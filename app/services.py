# -*- coding: utf-8 -*-
"""业务服务：孔与统计段维护、按样号换交的原子版本化重算。

并发与一致性语义（修复要点）
----------------------------
1. **统计段整体版本化、不可变**：每次保存统计段都新增一个 ``segment_set``，
   历史 ``segment_def`` 不删除、不修改。每个 ``assay_version`` 外键固定到其
   核算时使用的段集，因此改段不会破坏任何历史版本的逐段核查明细，旧底表永远
   可以完整下载。
2. **单事务原子生效**：保存统计段与随之而来的重算在同一个 ``BEGIN IMMEDIATE``
   事务内完成——新段集、化验快照、核查结果、底表以及生效指针翻转要么全部
   提交，要么全部回滚。核算或保存失败时，已生效的分段和底表原样保留。
3. **写操作串行化**：``BEGIN IMMEDIATE`` 进入即取库级写锁，事务内读到的必然
   是最新已提交状态。改段重算与换交交错时，后到者在锁上等待，先到者提交后
   再基于最新化验/分段核算，已成功换交的化验不会被旧快照重算覆盖；两项操作
   都成功时，最终底表同时采用最新分段与最新化验。
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .csvio import CsvError
from .db import get_db, immediate_transaction
from .tiling import Assay, SegmentVerdict, dumps_samples, evaluate_all


class ServiceError(ValueError):
    pass


# ---------------------------------------------------------------- 小工具

def _as_service_error(exc: sqlite3.Error) -> ServiceError:
    """把 SQLite 锁竞争等底层错误翻译成用户可理解的业务错误。"""
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return ServiceError("系统正忙：另一操作正在写入，本次未生效，请稍后重试")
    return ServiceError(f"数据库操作失败：{exc}")


def _load_assays_for_version(conn: sqlite3.Connection, version_id: int) -> List[Assay]:
    rows = conn.execute(
        "SELECT * FROM assay_row WHERE version_id = ? ORDER BY start_cm, sample_no",
        (version_id,),
    ).fetchall()
    return [
        Assay(r["sample_no"], r["start_cm"], r["end_cm"], r["grade_ppm"], bool(r["recheck"]))
        for r in rows
    ]


def _current_assays(conn: sqlite3.Connection, hole: sqlite3.Row) -> List[Assay]:
    """事务内读取当前生效版本的化验（无生效版本时为空）。"""
    if hole["effective_version_id"] is None:
        return []
    return _load_assays_for_version(conn, hole["effective_version_id"])


def _segments_of_set(
    conn: sqlite3.Connection, segment_set_id: Optional[int]
) -> List[sqlite3.Row]:
    if segment_set_id is None:
        return []
    return conn.execute(
        "SELECT * FROM segment_def WHERE segment_set_id = ? ORDER BY seq",
        (segment_set_id,),
    ).fetchall()


# ---------------------------------------------------------------- 孔与统计段

def create_hole(code: str, name: str = "") -> int:
    code = (code or "").strip()
    if not code:
        raise ServiceError("孔号不能为空")
    conn = get_db()
    try:
        with immediate_transaction():
            cur = conn.execute(
                "INSERT INTO hole(code, name) VALUES (?, ?)", (code, name.strip())
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ServiceError(f"孔号 {code} 已存在")
    except sqlite3.OperationalError as exc:
        raise _as_service_error(exc)


def list_holes() -> List[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT h.*, (
            SELECT COUNT(*) FROM segment_def d
            WHERE d.segment_set_id = h.effective_segment_set_id
        ) AS segment_count, (
            SELECT COUNT(*) FROM assay_row r
            JOIN assay_version v ON v.id = r.version_id
            WHERE v.id = h.effective_version_id
        ) AS assay_count
        FROM hole h ORDER BY h.code
        """
    ).fetchall()


def get_hole(hole_id: int) -> Optional[sqlite3.Row]:
    return get_db().execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()


def list_segments(hole_id: int) -> List[sqlite3.Row]:
    """返回该孔当前生效的统计段（随段集版本化，历史段集不在此展示）。"""
    hole = get_hole(hole_id)
    if hole is None:
        return []
    return _segments_of_set(get_db(), hole["effective_segment_set_id"])


def _validate_segments(segments: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    cleaned: List[Tuple[int, int]] = []
    for i, (s, e) in enumerate(segments, start=1):
        if not isinstance(s, int) or not isinstance(e, int):
            raise ServiceError(f"第 {i} 段：厘米值必须是整数")
        if e <= s:
            raise ServiceError(f"第 {i} 段：止厘米必须大于起厘米")
        cleaned.append((s, e))
    # 段间不允许重叠（底表深度段应互斥；相邻相接允许）。
    ordered = sorted(cleaned)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if s2 < e1:
            raise ServiceError(f"统计段重叠：[{s1},{e1}) 与 [{s2},{e2})")
    return ordered


def replace_segments(
    hole_id: int, segments: Sequence[Tuple[int, int]]
) -> dict:
    """整体替换统计段定义，并在同一事务内基于当前生效化验重算底表。

    返回 ``{"segment_set_id": int, "version_id": Optional[int]}``。
    无生效化验时只落段集（version_id 为 None）；任一步失败整体回滚，
    已生效的分段与底表不受影响。
    """
    hole = get_hole(hole_id)
    if hole is None:
        raise ServiceError("钻孔不存在")
    ordered = _validate_segments(segments)

    conn = get_db()
    try:
        with immediate_transaction():
            # 锁内重读孔行，保证基于最新生效状态操作。
            hole = conn.execute(
                "SELECT * FROM hole WHERE id = ?", (hole_id,)
            ).fetchone()

            # 1) 写入一套全新的、不可变的段集（旧段集与历史核查结果原样保留）。
            set_cur = conn.execute(
                "INSERT INTO segment_set(hole_id) VALUES (?)", (hole_id,)
            )
            segment_set_id = set_cur.lastrowid
            conn.executemany(
                """
                INSERT INTO segment_def(segment_set_id, hole_id, seq, start_cm, end_cm)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (segment_set_id, hole_id, i, s, e)
                    for i, (s, e) in enumerate(ordered, start=1)
                ],
            )
            conn.execute(
                "UPDATE hole SET effective_segment_set_id = ? WHERE id = ?",
                (segment_set_id, hole_id),
            )

            # 2) 已有生效化验：同事务内用新段集重算并翻转生效版本指针。
            version_id: Optional[int] = None
            if hole["effective_version_id"] is not None:
                assays = _load_assays_for_version(conn, hole["effective_version_id"])
                seg_tuples = [
                    (i, s, e) for i, (s, e) in enumerate(ordered, start=1)
                ]
                verdicts = evaluate_all(seg_tuples, assays)
                version_id = _persist_computed_version(
                    conn,
                    hole_id=hole_id,
                    segment_set_id=segment_set_id,
                    assays=assays,
                    verdicts=verdicts,
                    change_note="统计段调整后重算",
                    diff=(0, 0, 0),
                )
            return {"segment_set_id": segment_set_id, "version_id": version_id}
    except sqlite3.OperationalError as exc:
        raise _as_service_error(exc)


# ---------------------------------------------------------------- 版本与化验

def latest_version(hole_id: int) -> Optional[sqlite3.Row]:
    hole = get_hole(hole_id)
    if hole is None or hole["effective_version_id"] is None:
        return None
    return get_db().execute(
        "SELECT * FROM assay_version WHERE id = ?", (hole["effective_version_id"],)
    ).fetchone()


def list_versions(hole_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM assay_version WHERE hole_id = ? ORDER BY seq DESC", (hole_id,)
    ).fetchall()


def latest_assays(hole_id: int) -> List[Assay]:
    hole = get_hole(hole_id)
    if hole is None or hole["effective_version_id"] is None:
        return []
    return _load_assays_for_version(get_db(), hole["effective_version_id"])


def list_results(version_id: int) -> List[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM segment_result WHERE version_id = ? ORDER BY seq", (version_id,)
    ).fetchall()


def _next_version_seq(conn: sqlite3.Connection, hole_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM assay_version WHERE hole_id = ?",
        (hole_id,),
    ).fetchone()
    return row["next_seq"]


def _diff_by_sample(old: Sequence[Assay], new: Sequence[Assay]) -> Tuple[int, int, int]:
    """按样号比对两套化验：返回 (新增, 消失, 变更) 计数。"""
    old_map = {a.sample_no: a for a in old}
    new_map = {a.sample_no: a for a in new}
    added = [k for k in new_map if k not in old_map]
    removed = [k for k in old_map if k not in new_map]
    changed = [
        k for k in (old_map.keys() & new_map.keys())
        if (
            old_map[k].start_cm != new_map[k].start_cm
            or old_map[k].end_cm != new_map[k].end_cm
            or old_map[k].grade_ppm != new_map[k].grade_ppm
            or old_map[k].recheck != new_map[k].recheck
        )
    ]
    return len(added), len(removed), len(changed)


def _persist_computed_version(
    conn: sqlite3.Connection,
    hole_id: int,
    segment_set_id: int,
    assays: Sequence[Assay],
    verdicts: Sequence[SegmentVerdict],
    change_note: str,
    diff: Tuple[int, int, int],
) -> int:
    """在调用方已开启的事务内写入版本快照并翻转生效指针，返回新版本 id。

    调用方负责保证 ``verdicts`` 就是用 ``segment_set_id`` 这套段定义核算的。
    """
    seq = _next_version_seq(conn, hole_id)
    cur = conn.execute(
        """
        INSERT INTO assay_version(hole_id, segment_set_id, seq, status, change_note,
                                  added_count, removed_count, changed_count)
        VALUES (?, ?, ?, 'effective', ?, ?, ?, ?)
        """,
        (hole_id, segment_set_id, seq, change_note, diff[0], diff[1], diff[2]),
    )
    version_id = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO assay_row(version_id, sample_no, start_cm, end_cm, grade_ppm, recheck)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(version_id, a.sample_no, a.start_cm, a.end_cm, a.grade_ppm, 1 if a.recheck else 0)
         for a in assays],
    )
    seg_rows = conn.execute(
        "SELECT id, seq, start_cm, end_cm FROM segment_def "
        "WHERE segment_set_id = ? ORDER BY seq",
        (segment_set_id,),
    ).fetchall()
    seg_by_seq = {r["seq"]: r for r in seg_rows}
    for v in verdicts:
        sd = seg_by_seq.get(v.seq)
        if sd is None:
            continue
        conn.execute(
            """
            INSERT INTO segment_result(
                version_id, segment_def_id, seq, start_cm, end_cm, status,
                grade_num, grade_den, recheck_cover_cm, covered_cm,
                sample_path_min, sample_path_max, grade_num_min, grade_num_max)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version_id, sd["id"], v.seq, v.start_cm, v.end_cm, v.status,
                v.grade_num, v.grade_den, v.recheck_cover_cm, v.covered_cm,
                dumps_samples(v.samples_min), dumps_samples(v.samples_max),
                v.grade_num_min, v.grade_num_max,
            ),
        )
    # 旧生效版本降级；翻转指针——同一事务内原子完成。
    conn.execute(
        "UPDATE assay_version SET status = 'superseded' "
        "WHERE hole_id = ? AND id <> ? AND status = 'effective'",
        (hole_id, version_id),
    )
    conn.execute(
        "UPDATE hole SET effective_version_id = ? WHERE id = ?",
        (version_id, hole_id),
    )
    return version_id


def recompute(hole_id: int, change_note: str = "手动重算") -> int:
    """基于当前生效化验 + 当前生效段集生成新版本；单事务，失败整体回滚。"""
    conn = get_db()
    try:
        with immediate_transaction():
            hole = conn.execute(
                "SELECT * FROM hole WHERE id = ?", (hole_id,)
            ).fetchone()
            if hole is None:
                raise ServiceError("钻孔不存在")
            if hole["effective_version_id"] is None:
                raise ServiceError("尚无化验数据，无法重算；请先上传化验 CSV")
            segment_set_id = hole["effective_segment_set_id"]
            seg_rows = _segments_of_set(conn, segment_set_id)
            if not seg_rows:
                raise ServiceError("尚未定义统计段")

            assays = _load_assays_for_version(conn, hole["effective_version_id"])
            seg_tuples = [(r["seq"], r["start_cm"], r["end_cm"]) for r in seg_rows]
            verdicts = evaluate_all(seg_tuples, assays)
            return _persist_computed_version(
                conn,
                hole_id=hole_id,
                segment_set_id=segment_set_id,
                assays=assays,
                verdicts=verdicts,
                change_note=change_note,
                diff=(0, 0, 0),
            )
    except sqlite3.OperationalError as exc:
        raise _as_service_error(exc)


def upload_assays(
    hole_id: int, assays: Sequence[Assay], change_note: str = "按样号换交化验"
) -> dict:
    """换交入口：加写锁 -> 比对样号 -> 核算 -> 单事务落库并翻转版本。

    写锁保证与改段重算互斥：本事务提交前，任何重算都只能在锁外等待，因此
    换交成功后的新生效化验不可能被持旧快照的重算覆盖。
    """
    # 样号唯一性已在 CSV 解析阶段保证；这里再防一层同孔内的硬约束。
    sample_nos = [a.sample_no for a in assays]
    if len(set(sample_nos)) != len(sample_nos):
        raise CsvError("样号存在重复")

    conn = get_db()
    try:
        with immediate_transaction():
            hole = conn.execute(
                "SELECT * FROM hole WHERE id = ?", (hole_id,)
            ).fetchone()
            if hole is None:
                raise ServiceError("钻孔不存在")
            segment_set_id = hole["effective_segment_set_id"]
            seg_rows = _segments_of_set(conn, segment_set_id)
            if not seg_rows:
                raise ServiceError("请先为该孔定义统计段，再上传化验 CSV")

            old_assays = _current_assays(conn, hole)
            diff = _diff_by_sample(old_assays, assays)
            seg_tuples = [(r["seq"], r["start_cm"], r["end_cm"]) for r in seg_rows]
            verdicts = evaluate_all(seg_tuples, assays)

            version_id = _persist_computed_version(
                conn,
                hole_id=hole_id,
                segment_set_id=segment_set_id,
                assays=assays,
                verdicts=verdicts,
                change_note=change_note,
                diff=diff,
            )
    except sqlite3.OperationalError as exc:
        raise _as_service_error(exc)

    return {
        "version_id": version_id,
        "added": diff[0],
        "removed": diff[1],
        "changed": diff[2],
        "ok": sum(1 for v in verdicts if v.status == "ok"),
        "diverged": sum(1 for v in verdicts if v.status == "diverged"),
        "incomplete": sum(1 for v in verdicts if v.status == "incomplete"),
    }
