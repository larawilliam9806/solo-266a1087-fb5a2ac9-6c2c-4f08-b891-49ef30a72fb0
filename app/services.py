# -*- coding: utf-8 -*-
"""业务服务：孔与统计段维护、按样号换交的原子版本化重算。

换交语义：一次上传的 CSV 代表该孔当前全套化验。以样号为主键与上一生效
版本比对，得到新增/消失/变更样号；化验行、逐段核查结果、底表结果在同一
事务内写入新版本，全部成功后才把 hole.effective_version_id 指向新版本，
旧版本保持只读快照，任何一步失败则整体回滚，旧底表照常可下载。
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .csvio import CsvError
from .db import get_db
from .tiling import Assay, SegmentVerdict, dumps_samples, evaluate_all


class ServiceError(ValueError):
    pass


# ---------------------------------------------------------------- 孔与统计段

def create_hole(code: str, name: str = "") -> int:
    conn = get_db()
    code = (code or "").strip()
    if not code:
        raise ServiceError("孔号不能为空")
    try:
        cur = conn.execute(
            "INSERT INTO hole(code, name) VALUES (?, ?)", (code, name.strip())
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ServiceError(f"孔号 {code} 已存在")


def list_holes() -> List[sqlite3.Row]:
    return get_db().execute(
        """
        SELECT h.*, (
            SELECT COUNT(*) FROM segment_def s WHERE s.hole_id = h.id
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
    return get_db().execute(
        "SELECT * FROM segment_def WHERE hole_id = ? ORDER BY seq", (hole_id,)
    ).fetchall()


def replace_segments(hole_id: int, segments: Sequence[Tuple[int, int]]) -> None:
    """整体替换统计段定义：(start_cm, end_cm) 列表，自动编号并校验。"""
    hole = get_hole(hole_id)
    if hole is None:
        raise ServiceError("钻孔不存在")
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

    conn = get_db()
    try:
        with conn:  # 事务
            conn.execute("DELETE FROM segment_def WHERE hole_id = ?", (hole_id,))
            conn.executemany(
                "INSERT INTO segment_def(hole_id, seq, start_cm, end_cm) VALUES (?,?,?,?)",
                [(hole_id, i, s, e) for i, (s, e) in enumerate(ordered, start=1)],
            )
    except sqlite3.DatabaseError as exc:  # pragma: no cover - 兜底
        raise ServiceError(f"统计段保存失败：{exc}")

    # 段定义变了，基于当前生效化验重算一版（若无化验则跳过）。
    rows = latest_assays(hole_id)
    if rows:
        recompute(hole_id, change_note="统计段调整后重算")


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
    rows = get_db().execute(
        "SELECT * FROM assay_row WHERE version_id = ? ORDER BY start_cm, sample_no",
        (hole["effective_version_id"],),
    ).fetchall()
    return [
        Assay(r["sample_no"], r["start_cm"], r["end_cm"], r["grade_ppm"], bool(r["recheck"]))
        for r in rows
    ]


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


def _persist_version(
    conn: sqlite3.Connection,
    hole_id: int,
    assays: Sequence[Assay],
    verdicts: Sequence[SegmentVerdict],
    change_note: str,
    diff: Tuple[int, int, int],
) -> int:
    """在调用方事务内写入版本快照并翻转生效指针，返回新版本 id。"""
    seq = _next_version_seq(conn, hole_id)
    cur = conn.execute(
        """
        INSERT INTO assay_version(hole_id, seq, status, change_note,
                                  added_count, removed_count, changed_count)
        VALUES (?, ?, 'effective', ?, ?, ?, ?)
        """,
        (hole_id, seq, change_note, diff[0], diff[1], diff[2]),
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
        "SELECT id, seq, start_cm, end_cm FROM segment_def WHERE hole_id = ? ORDER BY seq",
        (hole_id,),
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
    """基于当前生效化验 + 当前统计段定义，生成新版本。无化验时拒绝。"""
    conn = get_db()
    hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
    if hole is None:
        raise ServiceError("钻孔不存在")
    assays = latest_assays(hole_id)
    if not assays:
        raise ServiceError("尚无化验数据，无法重算；请先上传化验 CSV")
    segments = [
        (r["seq"], r["start_cm"], r["end_cm"]) for r in list_segments(hole_id)
    ]
    if not segments:
        raise ServiceError("尚未定义统计段")

    verdicts = evaluate_all(segments, assays)
    diff = (0, 0, 0)
    with conn:  # 单事务：失败整体回滚，旧版本不受影响
        return _persist_version(conn, hole_id, assays, verdicts, change_note, diff)


def upload_assays(hole_id: int, assays: Sequence[Assay], change_note: str = "按样号换交化验") -> dict:
    """换交入口：校验 -> 比对样号 -> 核算 -> 单事务落库并翻转版本。"""
    conn = get_db()
    hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
    if hole is None:
        raise ServiceError("钻孔不存在")
    segments = list_segments(hole_id)
    if not segments:
        raise ServiceError("请先为该孔定义统计段，再上传化验 CSV")

    # 样号唯一性已在 CSV 解析阶段保证；这里再防一层同孔内的硬约束。
    sample_nos = [a.sample_no for a in assays]
    if len(set(sample_nos)) != len(sample_nos):
        raise CsvError("样号存在重复")

    old_assays = latest_assays(hole_id)
    diff = _diff_by_sample(old_assays, assays)
    seg_tuples = [(r["seq"], r["start_cm"], r["end_cm"]) for r in segments]
    verdicts = evaluate_all(seg_tuples, assays)

    with conn:  # 单事务：化验、核查结果、底表一同生效
        version_id = _persist_version(conn, hole_id, assays, verdicts, change_note, diff)

    counts = {
        "version_id": version_id,
        "added": diff[0],
        "removed": diff[1],
        "changed": diff[2],
        "ok": sum(1 for v in verdicts if v.status == "ok"),
        "diverged": sum(1 for v in verdicts if v.status == "diverged"),
        "incomplete": sum(1 for v in verdicts if v.status == "incomplete"),
    }
    return counts
