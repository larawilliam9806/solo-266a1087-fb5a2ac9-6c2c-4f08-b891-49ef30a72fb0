# -*- coding: utf-8 -*-
"""Flask 路由：钻孔管理、统计段维护、化验上传、版本核查、底表下载。"""
from __future__ import annotations

import io
import os

from flask import (
    Flask, abort, flash, redirect, render_template, request, send_file, url_for,
)

from .csvio import CsvError, assay_template_csv, export_supply_csv, parse_assay_csv
from .db import get_db, init_app
from .services import ServiceError, create_hole, list_holes, list_segments
from .services import list_results, list_versions, latest_assays, latest_version
from .services import recompute, replace_segments, upload_assays
from .tiling import loads_samples


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get("DATABASE_PATH", "/data/geology.db"),
    )
    if config:
        app.config.update(config)
    init_app(app)

    @app.template_filter("approx6")
    def _approx6(pair) -> str:
        """整数长除法给出 6 位小数近似值（页面标注 ≈，精确值以分子/分母为准）。"""
        num, den = pair
        whole, rem = divmod(num, den)
        digits = []
        for _ in range(6):
            rem *= 10
            digits.append(str(rem // den))
            rem %= den
        return f"{whole}.{''.join(digits)}"

    # ------------------------------------------------------------ 首页 / 钻孔
    @app.route("/")
    def index():
        holes = list_holes()
        return render_template("index.html", holes=holes)

    @app.route("/holes", methods=["POST"])
    def add_hole():
        code = request.form.get("code", "")
        name = request.form.get("name", "")
        try:
            create_hole(code, name)
            flash(f"钻孔 {code} 已创建", "ok")
        except ServiceError as exc:
            flash(str(exc), "err")
        return redirect(url_for("index"))

    @app.route("/holes/<int:hole_id>")
    def hole_detail(hole_id: int):
        conn = get_db()
        hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
        if hole is None:
            abort(404)
        segments = list_segments(hole_id)
        version = latest_version(hole_id)
        results = list_results(version["id"]) if version else []
        assays = latest_assays(hole_id)
        versions = list_versions(hole_id)
        # 把样号 JSON 解析成列表供模板展示。
        enriched = []
        for r in results:
            d = dict(r)
            d["samples_min"] = loads_samples(r["sample_path_min"])
            d["samples_max"] = loads_samples(r["sample_path_max"])
            enriched.append(d)
        return render_template(
            "hole.html",
            hole=hole,
            segments=segments,
            version=version,
            results=enriched,
            assays=assays,
            versions=versions,
        )

    # ------------------------------------------------------------ 统计段
    @app.route("/holes/<int:hole_id>/segments", methods=["POST"])
    def save_segments(hole_id: int):
        raw = request.form.get("segments_text", "")
        segments = []
        try:
            for lineno, line in enumerate(raw.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                parts = line.replace(",", " ").replace("-", " ").split()
                if len(parts) < 2:
                    raise ServiceError(
                        f"第 {lineno} 行格式不对：每行写“起厘米 止厘米”，收到 {line!r}"
                    )
                s, e = int(parts[0]), int(parts[1])
                segments.append((s, e))
            if not segments:
                raise ServiceError("至少需要一个统计段")
            replace_segments(hole_id, segments)
            flash(f"已保存 {len(segments)} 个统计段", "ok")
        except (ServiceError, ValueError) as exc:
            flash(f"统计段保存失败：{exc}", "err")
        return redirect(url_for("hole_detail", hole_id=hole_id))

    # ------------------------------------------------------------ 化验上传 / 重算
    @app.route("/holes/<int:hole_id>/upload", methods=["POST"])
    def upload(hole_id: int):
        f = request.files.get("assay_csv")
        if f is None or not f.filename:
            flash("请选择化验 CSV 文件", "err")
            return redirect(url_for("hole_detail", hole_id=hole_id))
        try:
            # 上传文件属于外部数据：只读字节、utf-8 解码，不执行其中任何内容。
            text = f.read().decode("utf-8-sig")
            assays = parse_assay_csv(text)
            counts = upload_assays(hole_id, assays)
            flash(
                "换交成功（新版本 #{version_id}）：新增 {added} 个样号、消失 {removed} 个、"
                "变更 {changed} 个；核查结果 供数 {ok} 段 / 分歧 {diverged} 段 / "
                "待补样 {incomplete} 段".format(**counts),
                "ok",
            )
        except (CsvError, ServiceError, UnicodeDecodeError) as exc:
            # 失败时事务已回滚，旧生效版本保持可下载。
            flash(f"换交失败，旧底表仍有效：{exc}", "err")
        return redirect(url_for("hole_detail", hole_id=hole_id))

    @app.route("/holes/<int:hole_id>/recompute", methods=["POST"])
    def do_recompute(hole_id: int):
        try:
            vid = recompute(hole_id)
            flash(f"已基于当前化验重算并生成新版本 #{vid}", "ok")
        except ServiceError as exc:
            flash(f"重算失败：{exc}", "err")
        return redirect(url_for("hole_detail", hole_id=hole_id))

    # ------------------------------------------------------------ 下载
    @app.route("/holes/<int:hole_id>/supply.csv")
    def supply_csv(hole_id: int):
        conn = get_db()
        hole = conn.execute("SELECT * FROM hole WHERE id = ?", (hole_id,)).fetchone()
        if hole is None:
            abort(404)
        if hole["effective_version_id"] is None:
            flash("尚无生效底表：请先定义统计段并上传化验 CSV", "err")
            return redirect(url_for("hole_detail", hole_id=hole_id))
        rows = list_results(hole["effective_version_id"])

        class _V:  # 轻量适配 export_supply_csv 所需字段
            def __init__(self, r):
                self.seq = r["seq"]
                self.start_cm = r["start_cm"]
                self.end_cm = r["end_cm"]
                self.status = r["status"]
                self.grade_num = r["grade_num"]
                self.grade_den = r["grade_den"]
                self.grade_num_min = r["grade_num_min"]
                self.grade_num_max = r["grade_num_max"]
                self.recheck_cover_cm = r["recheck_cover_cm"]
                self.samples_min = loads_samples(r["sample_path_min"])
                self.samples_max = loads_samples(r["sample_path_max"])

            @property
            def length(self):
                return self.end_cm - self.start_cm

        data = export_supply_csv(hole["code"], [_V(r) for r in rows])
        buf = io.BytesIO(data.encode("utf-8-sig"))
        return send_file(
            buf,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=f"{hole['code']}_supply.csv",
        )

    @app.route("/template.csv")
    def template_csv():
        buf = io.BytesIO(assay_template_csv().encode("utf-8-sig"))
        return send_file(
            buf,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name="assay_template.csv",
        )

    @app.route("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return {"status": "ok"}

    return app
