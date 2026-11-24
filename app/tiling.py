# -*- coding: utf-8 -*-
"""统计段铺排核算引擎。

规则（对应需求）：
1. 仅使用完全落在统计段 [seg_start, seg_end) 之内的化验段；
   合格铺法要求化验段首尾相接、恰好铺满统计段。
2. 先比较「复验覆盖长度」（recheck=1 的化验段长度之和），只在复验覆盖
   最长的铺法之间比较品位。
3. 品位按各化验段长度加权，全程整数精确运算：加权和 num = Σ grade_i * len_i，
   段长 den 固定，故比较 num 即可判断精确品位是否一致。
4. 最长复验覆盖下：
   - 铺满且所有铺法 num 相同 -> ok，给一套样号；
   - 铺满但 num 存在分歧 -> diverged，给最小/最大加权和两套样号及各自品位；
   - 铺不满 -> incomplete（待补样）。

复杂度：每个统计段对化验段按端点排序后做一次 DAG 动态规划，
O(m log m) 时间、O(m) 空间，与铺法总数无关（指数级铺法也不膨胀）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Assay:
    sample_no: str
    start_cm: int
    end_cm: int
    grade_ppm: int
    recheck: bool

    @property
    def length(self) -> int:
        return self.end_cm - self.start_cm


@dataclass
class SegmentVerdict:
    seq: int
    start_cm: int
    end_cm: int
    status: str  # ok | diverged | incomplete
    grade_num: Optional[int] = None
    grade_den: Optional[int] = None
    grade_num_min: Optional[int] = None
    grade_num_max: Optional[int] = None
    samples_min: List[str] = field(default_factory=list)
    samples_max: List[str] = field(default_factory=list)
    recheck_cover_cm: int = 0
    covered_cm: int = 0

    @property
    def length(self) -> int:
        return self.end_cm - self.start_cm


def _candidate_assays(assays: Sequence[Assay], seg_start: int, seg_end: int) -> List[Assay]:
    """取完全包含在统计段内的化验段，按 (起点, 终点) 排序。"""
    inside = [a for a in assays if a.start_cm >= seg_start and a.end_cm <= seg_end]
    inside.sort(key=lambda a: (a.start_cm, a.end_cm))
    return inside


def evaluate_segment(
    seq: int,
    seg_start: int,
    seg_end: int,
    assays: Sequence[Assay],
) -> SegmentVerdict:
    """对单个统计段求值。"""
    inside = _candidate_assays(assays, seg_start, seg_end)
    seg_len = seg_end - seg_start

    # 坐标点：统计段起点 + 各化验段端点（落在段内的）。
    points = {seg_start, seg_end}
    for a in inside:
        points.add(a.start_cm)
        points.add(a.end_cm)
    coords = sorted(points)
    index = {c: i for i, c in enumerate(coords)}
    n = len(coords)

    INF = None  # 用 None 表示不可达

    # 每个可达坐标维护：最长复验覆盖 rc；该 rc 下加权和的最小值/最大值及对应前驱。
    # 元组: (rc, num, prev_index, assay)
    best_rc: List[Optional[int]] = [INF] * n
    min_state: List[Optional[Tuple[int, int, Optional[int], Optional[Assay]]]] = [None] * n
    max_state: List[Optional[Tuple[int, int, Optional[int], Optional[Assay]]]] = [None] * n
    best_rc[0] = 0
    min_state[0] = (0, 0, None, None)
    max_state[0] = (0, 0, None, None)

    # 按起点分组，保证每个坐标的入边在该坐标出边之前全部处理完。
    by_start: dict = {}
    for a in inside:
        by_start.setdefault(a.start_cm, []).append(a)

    for i, c in enumerate(coords):
        if best_rc[i] is None:
            continue
        for a in by_start.get(c, ()):
            j = index[a.end_cm]
            add_len = a.length
            rc_add = add_len if a.recheck else 0
            num_add = a.grade_ppm * add_len

            cur_rc = best_rc[i] + rc_add
            cur_min_num = min_state[i][1] + num_add
            cur_max_num = max_state[i][1] + num_add

            if best_rc[j] is None or cur_rc > best_rc[j]:
                # 更长的复验覆盖：旧路径整体淘汰。
                best_rc[j] = cur_rc
                min_state[j] = (cur_rc, cur_min_num, i, a)
                max_state[j] = (cur_rc, cur_max_num, i, a)
            elif cur_rc == best_rc[j]:
                if cur_min_num < min_state[j][1]:
                    min_state[j] = (cur_rc, cur_min_num, i, a)
                if cur_max_num > max_state[j][1]:
                    max_state[j] = (cur_rc, cur_max_num, i, a)

    end_idx = n - 1
    if best_rc[end_idx] is None:
        # 铺不满：求最长可覆盖长度，便于提示待补样缺口。
        covered = 0
        for i, c in enumerate(coords):
            if best_rc[i] is not None:
                covered = max(covered, c - seg_start)
        return SegmentVerdict(
            seq=seq,
            start_cm=seg_start,
            end_cm=seg_end,
            status="incomplete",
            recheck_cover_cm=0,
            covered_cm=covered,
        )

    rc = best_rc[end_idx]
    mn = min_state[end_idx]
    mx = max_state[end_idx]

    def rebuild(states) -> List[str]:
        """沿前驱链回溯样号（states 为 min_state 或 max_state）。"""
        path: List[str] = []
        i = end_idx
        while states[i][2] is not None:
            path.append(states[i][3].sample_no)
            i = states[i][2]
        path.reverse()
        return path

    path_min = rebuild(min_state)
    num_min = mn[1]

    if mx[1] == mn[1]:
        # 最长复验覆盖下所有铺法精确品位一致。
        return SegmentVerdict(
            seq=seq,
            start_cm=seg_start,
            end_cm=seg_end,
            status="ok",
            grade_num=num_min,
            grade_den=seg_len,
            samples_min=path_min,
            samples_max=[],
            recheck_cover_cm=rc,
            covered_cm=seg_len,
        )

    # 品位分歧：两套样号 + 各自精确品位（分母同为段长）。
    path_max = rebuild(max_state)
    return SegmentVerdict(
        seq=seq,
        start_cm=seg_start,
        end_cm=seg_end,
        status="diverged",
        grade_num_min=num_min,
        grade_num_max=mx[1],
        grade_den=seg_len,
        samples_min=path_min,
        samples_max=path_max,
        recheck_cover_cm=rc,
        covered_cm=seg_len,
    )


def evaluate_all(
    segments: Sequence[Tuple[int, int, int]],
    assays: Sequence[Assay],
) -> List[SegmentVerdict]:
    """segments: (seq, start_cm, end_cm) 列表。"""
    return [evaluate_segment(seq, s, e, assays) for seq, s, e in segments]


def dumps_samples(samples: Sequence[str]) -> str:
    return json.dumps(list(samples), ensure_ascii=False)


def loads_samples(blob: Optional[str]) -> List[str]:
    if not blob:
        return []
    return list(json.loads(blob))
