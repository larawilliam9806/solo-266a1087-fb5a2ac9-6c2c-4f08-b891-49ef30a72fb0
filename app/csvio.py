# -*- coding: utf-8 -*-
"""化验 CSV 解析与校验、供数 CSV 导出。

上传 CSV 要求列：唯一样号、起厘米、止厘米、整数 ppm 品位、复验标记。
表头兼容中英文常见写法；复验标记兼容 是/否、Y/N、true/false、1/0。
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, List, Tuple

from .tiling import Assay, SegmentVerdict

HEADER_ALIASES = {
    "sample_no": "样号",
    "sample": "样号",
    "sampleid": "样号",
    "sample_id": "样号",
    "id": "样号",
    "样品号": "样号",
    "样号": "样号",
    "start_cm": "起厘米",
    "start": "起厘米",
    "from_cm": "起厘米",
    "from": "起厘米",
    "起点": "起厘米",
    "起": "起厘米",
    "起厘米": "起厘米",
    "end_cm": "止厘米",
    "end": "止厘米",
    "to_cm": "止厘米",
    "to": "止厘米",
    "终点": "止厘米",
    "止": "止厘米",
    "止厘米": "止厘米",
    "grade_ppm": "品位ppm",
    "grade": "品位ppm",
    "ppm": "品位ppm",
    "品位": "品位ppm",
    "品位ppm": "品位ppm",
    "品位(ppm)": "品位ppm",
    "recheck": "复验",
    "is_recheck": "复验",
    "复验": "复验",
    "复验标记": "复验",
    "复验标志": "复验",
}

CANONICAL = ["样号", "起厘米", "止厘米", "品位ppm", "复验"]

TRUE_TOKENS = {"1", "y", "yes", "true", "t", "是", "复验", "复"}
FALSE_TOKENS = {"0", "n", "no", "false", "f", "否", "非", "", "初验", "初"}


class CsvError(ValueError):
    pass


def _parse_int(raw: str, field_name: str, row_no: int) -> int:
    raw = (raw or "").strip()
    if raw == "":
        raise CsvError(f"第 {row_no} 行：{field_name} 不能为空")
    try:
        # 拒绝 12.5 这类非整数；允许 "12"、"12.0"？需求要求整数 ppm，严格只接受整数。
        return int(raw)
    except ValueError:
        raise CsvError(f"第 {row_no} 行：{field_name} 必须是整数厘米/ppm 值，收到 {raw!r}")


def _parse_recheck(raw: str, row_no: int) -> bool:
    token = (raw or "").strip().lower()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    raise CsvError(f"第 {row_no} 行：复验标记无法识别 {raw!r}（可用 是/否、Y/N、1/0、true/false）")


def parse_assay_csv(text: str) -> List[Assay]:
    """解析上传的化验 CSV，返回 Assay 列表；任何问题抛 CsvError。"""
    if not text.strip():
        raise CsvError("CSV 内容为空")
    # utf-8-sig 兼容带 BOM 的 Excel 导出。
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError("CSV 缺少表头行")

    header = [h.strip() for h in header]
    col_index = {}
    for idx, name in enumerate(header):
        key = name.lower().replace(" ", "")
        canon = HEADER_ALIASES.get(key) or HEADER_ALIASES.get(name)
        if canon and canon not in col_index:
            col_index[canon] = idx
    missing = [c for c in CANONICAL if c not in col_index]
    if missing:
        raise CsvError(f"CSV 缺少必需列：{'、'.join(missing)}；实际表头：{'、'.join(header)}")

    assays: List[Assay] = []
    seen = set()
    for row_no, row in enumerate(reader, start=2):
        if not any((c or "").strip() for c in row):
            continue  # 跳过空行
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        sample = row[col_index["样号"]].strip()
        if not sample:
            raise CsvError(f"第 {row_no} 行：样号不能为空")
        if sample in seen:
            raise CsvError(f"第 {row_no} 行：样号 {sample} 重复，样号在一份 CSV 中必须唯一")
        seen.add(sample)

        start = _parse_int(row[col_index["起厘米"]], "起厘米", row_no)
        end = _parse_int(row[col_index["止厘米"]], "止厘米", row_no)
        if end <= start:
            raise CsvError(f"第 {row_no} 行：止厘米必须大于起厘米（{start} >= {end}）")
        grade = _parse_int(row[col_index["品位ppm"]], "品位ppm", row_no)
        if grade < 0:
            raise CsvError(f"第 {row_no} 行：品位不能为负数")
        recheck = _parse_recheck(row[col_index["复验"]], row_no)
        assays.append(Assay(sample, start, end, grade, recheck))

    if not assays:
        raise CsvError("CSV 中没有任何化验数据行")
    return assays


def assay_template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CANONICAL)
    w.writerow(["S001", 0, 100, 320, "否"])
    w.writerow(["S002", 100, 250, 410, "是"])
    return buf.getvalue()


def _fmt_grade(num: int, den: int) -> str:
    """精确品位展示：整除输出整数；分母只含 2/5 因子时做整数长除法得有限
    小数；否则保留 num/den 分数形式。全程不使用浮点。"""
    whole, rem = divmod(num, den)
    if rem == 0:
        return f"{whole}"
    d = den
    for p in (2, 5):
        while d % p == 0:
            d //= p
    if d != 1:
        return f"{num}/{den}"
    # 整数长除法求有限小数（最多 10 位，剩余补零即可，有限小数必然终止）。
    digits = []
    r = rem
    while r != 0 and len(digits) < 10:
        r *= 10
        digits.append(str(r // den))
        r %= den
    return f"{whole}.{''.join(digits)}"


def export_supply_csv(hole_code: str, verdicts: Iterable[SegmentVerdict]) -> str:
    """导出供数底表 CSV。

    只有 ok 段列入供数品位；diverged/incomplete 保留行并标注状态、品位留空，
    样号列在分歧时给两套、待补样时为空。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "孔号", "段序号", "起厘米", "止厘米", "状态",
        "精确品位(ppm)", "品位分子", "品位分母(段长cm)",
        "复验覆盖(cm)", "样号集A", "样号集B", "品位A(ppm)", "品位B(ppm)",
    ])
    for v in verdicts:
        if v.status == "ok":
            w.writerow([
                hole_code, v.seq, v.start_cm, v.end_cm, "供数",
                _fmt_grade(v.grade_num, v.grade_den),
                v.grade_num, v.grade_den, v.recheck_cover_cm,
                " ".join(v.samples_min), "", "", "",
            ])
        elif v.status == "diverged":
            w.writerow([
                hole_code, v.seq, v.start_cm, v.end_cm, "品位分歧",
                "", "", v.grade_den, v.recheck_cover_cm,
                " ".join(v.samples_min), " ".join(v.samples_max),
                _fmt_grade(v.grade_num_min, v.grade_den),
                _fmt_grade(v.grade_num_max, v.grade_den),
            ])
        else:
            w.writerow([
                hole_code, v.seq, v.start_cm, v.end_cm, "待补样",
                "", "", v.length, 0, "", "", "", "",
            ])
    return buf.getvalue()
