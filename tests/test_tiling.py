# -*- coding: utf-8 -*-
"""铺排核算引擎单元测试。

运行：python -m pytest tests/ -q
"""
import time

from app.tiling import Assay, evaluate_all, evaluate_segment


def A(no, s, e, grade, rc=False):
    return Assay(no, s, e, grade, rc)


class TestBasicTiling:
    def test_perfect_single_tile_ok(self):
        v = evaluate_segment(1, 0, 100, [A("S1", 0, 100, 300)])
        assert v.status == "ok"
        assert v.grade_num == 300 * 100
        assert v.grade_den == 100
        assert v.samples_min == ["S1"]

    def test_chain_tiles_weighted_grade_exact(self):
        assays = [A("S1", 0, 40, 100), A("S2", 40, 100, 400)]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "ok"
        # (100*40 + 400*60) / 100 = 280
        assert v.grade_num == 100 * 40 + 400 * 60
        assert v.grade_num / v.grade_den == 280
        assert v.samples_min == ["S1", "S2"]

    def test_incomplete_when_gap(self):
        assays = [A("S1", 0, 40, 100), A("S2", 60, 100, 400)]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "incomplete"
        assert v.covered_cm == 40  # 最长可达 40cm

    def test_partial_overhang_ignored(self):
        # 化验段伸出统计段边界，不能参与铺满。
        assays = [A("S1", 0, 120, 100)]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "incomplete"

    def test_outside_assays_ignored_but_inside_works(self):
        assays = [A("X", 200, 300, 9), A("S1", 0, 50, 10), A("S2", 50, 100, 20)]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "ok"
        assert v.samples_min == ["S1", "S2"]


class TestRecheckPriority:
    def test_recheck_cover_wins_then_compare_grades(self):
        # 铺法1：单段全覆盖、初验，品位 500
        # 铺法2：两段拼接、第二段复验（覆盖 60cm），品位 100
        assays = [
            A("BIG", 0, 100, 500, rc=False),
            A("P1", 0, 40, 100, rc=False),
            A("P2", 40, 100, 100, rc=True),
        ]
        v = evaluate_segment(1, 0, 100, assays)
        # 复验覆盖 60 > 0，只在铺法2 体系内比较；铺法2唯一 => ok，品位 100
        assert v.status == "ok"
        assert v.recheck_cover_cm == 60
        assert v.samples_min == ["P1", "P2"]
        assert v.grade_num // v.grade_den == 100

    def test_lower_recheck_divergence_ignored(self):
        # 低复验铺法之间有分歧，但高复验铺法品位唯一 => 仍应 ok。
        assays = [
            # 高复验（覆盖100）：唯一铺法
            A("R1", 0, 50, 200, rc=True),
            A("R2", 50, 100, 200, rc=True),
            # 初验替代段，两种铺法品位不同，但复验覆盖为 0，应被整体淘汰
            A("Q1", 0, 100, 999, rc=False),
        ]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "ok"
        assert v.recheck_cover_cm == 100
        assert v.samples_min == ["R1", "R2"]

    def test_divergence_among_max_recheck_tilings(self):
        assays = [
            # 复验覆盖都为 100（所有段均复验），两种铺满方式品位不同
            A("A1", 0, 60, 100, rc=True),
            A("A2", 60, 100, 100, rc=True),       # 铺法A：品位 100
            A("B1", 0, 40, 400, rc=True),
            A("B2", 40, 100, 400, rc=True),       # 铺法B：品位 400
        ]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "diverged"
        assert v.grade_num_min == 100 * 100
        assert v.grade_num_max == 400 * 100
        assert v.samples_min == ["A1", "A2"]
        assert v.samples_max == ["B1", "B2"]

    def test_different_tilings_same_weighted_grade_ok(self):
        # 两套铺法加权和恰好相同 => 精确品位一致 => ok，只给一套样号。
        assays = [
            A("A1", 0, 50, 200, rc=True),
            A("A2", 50, 100, 200, rc=True),
            A("B1", 0, 30, 200, rc=True),
            A("B2", 30, 100, 200, rc=True),
        ]
        v = evaluate_segment(1, 0, 100, assays)
        assert v.status == "ok"
        assert v.grade_num == 200 * 100
        assert len(v.samples_max) == 0


class TestComplexity:
    def test_exponential_tilings_linear_time(self):
        """构造指数级铺法数量，验证运行时间/内存不随铺法数增长。

        段 [0, 2N)：对每个偶数点 2k 同时放长度 2 与长度 4（可行时）的段，
        铺法数随 N 指数增长；DP 状态数只与坐标点数量成线性关系。
        """
        n = 400  # 铺法数远超 2^(N/2)，无法枚举
        assays = []
        idx = 0
        for k in range(0, n, 2):
            assays.append(A(f"L2_{k}", k, min(k + 2, n), 10, rc=True))
            idx += 1
            if k + 4 <= n:
                assays.append(A(f"L4_{k}", k, k + 4, 10, rc=True))
                idx += 1
        start = time.perf_counter()
        v = evaluate_segment(1, 0, n, assays)
        elapsed = time.perf_counter() - start
        assert v.status == "ok"
        assert elapsed < 3.0  # 指数枚举绝无可能在该时间内完成
        # 所有品位相同 => 加权和一致
        assert v.grade_num == 10 * n

    def test_many_segments_total_linear(self):
        n = 300
        assays = [A(f"S{k}", k, k + 1, 50, rc=(k % 3 == 0)) for k in range(n)]
        segs = [(i, i * 3, min(i * 3 + 3, n)) for i in range(n // 3)]
        start = time.perf_counter()
        verdicts = evaluate_all(segs, assays)
        elapsed = time.perf_counter() - start
        assert all(v.status == "ok" for v in verdicts)
        assert elapsed < 3.0


class TestMultiSegment:
    def test_evaluate_all_independent(self):
        assays = [
            A("S1", 0, 100, 100),
            A("S2", 100, 200, 200),
            A("S3", 200, 250, 300),  # 第二段 [200,300) 铺不满
        ]
        verdicts = evaluate_all([(1, 0, 200), (2, 200, 300)], assays)
        assert verdicts[0].status == "ok"
        assert verdicts[1].status == "incomplete"
        assert verdicts[1].covered_cm == 50
