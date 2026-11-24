# -*- coding: utf-8 -*-
"""CSV 解析与导出测试。"""
import pytest

from app.csvio import CsvError, export_supply_csv, parse_assay_csv
from app.tiling import SegmentVerdict


class TestParse:
    def test_chinese_header_and_markers(self):
        text = "样号,起厘米,止厘米,品位ppm,复验标记\nS1,0,100,320,是\nS2,100,250,410,否\n"
        assays = parse_assay_csv(text)
        assert len(assays) == 2
        assert assays[0].sample_no == "S1"
        assert assays[0].recheck is True
        assert assays[1].recheck is False
        assert assays[1].grade_ppm == 410

    def test_english_header_bool_markers(self):
        text = "sample_no,from_cm,to_cm,grade_ppm,recheck\nA,0,10,5,Y\nB,10,20,7,false\n"
        assays = parse_assay_csv(text)
        assert assays[0].recheck is True
        assert assays[1].recheck is False

    def test_duplicate_sample_rejected(self):
        text = "样号,起厘米,止厘米,品位ppm,复验\nS1,0,10,1,否\nS1,10,20,2,否\n"
        with pytest.raises(CsvError, match="重复"):
            parse_assay_csv(text)

    def test_non_integer_grade_rejected(self):
        text = "样号,起厘米,止厘米,品位ppm,复验\nS1,0,10,1.5,否\n"
        with pytest.raises(CsvError, match="整数"):
            parse_assay_csv(text)

    def test_bad_interval_rejected(self):
        text = "样号,起厘米,止厘米,品位ppm,复验\nS1,100,100,1,否\n"
        with pytest.raises(CsvError, match="止厘米"):
            parse_assay_csv(text)

    def test_missing_column_rejected(self):
        with pytest.raises(CsvError, match="缺少必需列"):
            parse_assay_csv("样号,起厘米,止厘米\nS1,0,10\n")

    def test_bad_recheck_marker(self):
        text = "样号,起厘米,止厘米,品位ppm,复验\nS1,0,10,1,也许\n"
        with pytest.raises(CsvError, match="复验标记"):
            parse_assay_csv(text)

    def test_bom_tolerated(self):
        text = "﻿样号,起厘米,止厘米,品位ppm,复验\nS1,0,10,1,1\n"
        assays = parse_assay_csv(text)
        assert assays[0].sample_no == "S1"
        assert assays[0].recheck is True


class TestExport:
    def test_export_contains_all_statuses(self):
        verdicts = [
            SegmentVerdict(seq=1, start_cm=0, end_cm=100, status="ok",
                           grade_num=28000, grade_den=100,
                           samples_min=["S1", "S2"], recheck_cover_cm=60, covered_cm=100),
            SegmentVerdict(seq=2, start_cm=100, end_cm=200, status="diverged",
                           grade_num_min=10000, grade_num_max=40000, grade_den=100,
                           samples_min=["A"], samples_max=["B"],
                           recheck_cover_cm=100, covered_cm=100),
            SegmentVerdict(seq=3, start_cm=200, end_cm=300, status="incomplete",
                           covered_cm=40),
        ]
        csv_text = export_supply_csv("ZK1", verdicts)
        assert "ZK1" in csv_text
        assert "供数" in csv_text and "品位分歧" in csv_text and "待补样" in csv_text
        assert "S1 S2" in csv_text
        # 分歧两套品位 10000/100=100、40000/100=400，整除时品位列输出整数
        assert ",100,400" in csv_text
        # 除不尽时保留分数精确形式
        v_frac = SegmentVerdict(seq=9, start_cm=0, end_cm=3, status="diverged",
                                grade_num_min=1, grade_num_max=2, grade_den=3,
                                samples_min=["X"], samples_max=["Y"],
                                recheck_cover_cm=3, covered_cm=3)
        frac_csv = export_supply_csv("ZK2", [v_frac])
        assert "1/3" in frac_csv and "2/3" in frac_csv
        # ok 段精确品位 28000/100 = 280，应输出有限小数形式
        assert "280" in csv_text
