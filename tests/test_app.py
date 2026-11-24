# -*- coding: utf-8 -*-
"""端到端测试：建孔 -> 定义统计段 -> 换交上传 -> 核查 -> 下载 -> 再换交 -> 失败回滚。"""
import csv
import io
import threading
import time

import pytest

from app import create_app
from app import services


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def _make_hole(app, client, code="ZK001"):
    resp = client.post("/holes", data={"code": code, "name": "一号孔"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        from app.db import get_db
        row = get_db().execute("SELECT id FROM hole WHERE code = ?", (code,)).fetchone()
    return row["id"]


def _set_segments(client, hole_id, text):
    return client.post(
        f"/holes/{hole_id}/segments",
        data={"segments_text": text},
        follow_redirects=True,
    )


def _upload(client, hole_id, content):
    data = {"assay_csv": (io.BytesIO(content.encode("utf-8")), "assay.csv")}
    return client.post(f"/holes/{hole_id}/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


CSV_HEADER = "样号,起厘米,止厘米,品位ppm,复验标记\n"


class TestEndToEnd:
    def test_full_workflow_ok_segment(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100\n100 200\n")

        csv_text = CSV_HEADER + (
            "S1,0,40,100,否\n"
            "S2,40,100,400,是\n"   # 段1：(100*40+400*60)/100 = 280
            "S3,100,200,350,是\n"  # 段2：350
        )
        resp = _upload(client, hole_id, csv_text)
        assert "换交成功" in resp.get_data(as_text=True)
        assert "供数 2 段" in resp.get_data(as_text=True)

        # 下载底表
        dl = client.get(f"/holes/{hole_id}/supply.csv")
        assert dl.status_code == 200
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert rows[0][:5] == ["孔号", "段序号", "起厘米", "止厘米", "状态"]
        data_rows = rows[1:]
        assert all(r[4] == "供数" for r in data_rows)
        seg1 = next(r for r in data_rows if r[1] == "1")
        assert seg1[5] == "280" and seg1[6] == "28000" and seg1[7] == "100"
        assert set(seg1[9].split()) == {"S1", "S2"}

    def test_incomplete_marks_pending(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        resp = _upload(client, hole_id, CSV_HEADER + "S1,0,60,100,否\n")
        body = resp.get_data(as_text=True)
        assert "待补样 1 段" in body
        assert "待补样" in body

        dl = client.get(f"/holes/{hole_id}/supply.csv")
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert rows[1][4] == "待补样"
        assert rows[1][5] == ""  # 不列入供数品位

    def test_diverged_lists_two_sample_sets(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        csv_text = CSV_HEADER + (
            "A1,0,60,100,是\nA2,60,100,100,是\n"
            "B1,0,40,400,是\nB2,40,100,400,是\n"
        )
        resp = _upload(client, hole_id, csv_text)
        assert "分歧 1 段" in resp.get_data(as_text=True)

        dl = client.get(f"/holes/{hole_id}/supply.csv")
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        r = rows[1]
        assert r[4] == "品位分歧"
        assert r[5] == ""  # 分歧段不供数
        assert set(r[9].split()) == {"A1", "A2"}
        assert set(r[10].split()) == {"B1", "B2"}
        assert r[11] == "100" and r[12] == "400"

    def test_swap_by_sample_atomic_and_diff_counts(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,50,100,否\nS2,50,100,200,否\n")

        # 第二次换交：S1 品位变更，S2 消失，新增 S3
        resp = _upload(
            client, hole_id,
            CSV_HEADER + "S1,0,50,150,否\nS3,50,100,300,是\n",
        )
        body = resp.get_data(as_text=True)
        assert "新增 1 个样号、消失 1 个、变更 1 个" in body

        with client.application.app_context():
            from app.db import get_db
            versions = get_db().execute(
                "SELECT seq, status FROM assay_version WHERE hole_id = ? ORDER BY seq",
                (hole_id,),
            ).fetchall()
            assert [v["status"] for v in versions] == ["superseded", "effective"]

        # 底表已是新品位 (150*50+300*50)/100 = 225
        dl = client.get(f"/holes/{hole_id}/supply.csv")
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert rows[1][5] == "225"

    def test_failed_swap_keeps_previous_table_downloadable(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,是\n")

        # 坏 CSV：重复样号 -> 解析失败，事务不产生新版本
        bad = CSV_HEADER + "X1,0,50,1,否\nX1,50,100,2,否\n"
        resp = _upload(client, hole_id, bad)
        assert "换交失败" in resp.get_data(as_text=True)

        with client.application.app_context():
            from app.db import get_db
            n = get_db().execute(
                "SELECT COUNT(*) c FROM assay_version WHERE hole_id = ?", (hole_id,)
            ).fetchone()["c"]
            assert n == 1  # 没有失败版本残留

        # 旧底表照常下载
        dl = client.get(f"/holes/{hole_id}/supply.csv")
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert rows[1][4] == "供数"
        assert rows[1][5] == "300"

    def test_recompute_creates_new_version(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")

        # 调整统计段后自动重算；再手动重算一次
        _set_segments(client, hole_id, "0 50\n50 100")
        resp = client.post(f"/holes/{hole_id}/recompute", follow_redirects=True)
        assert "新版本" in resp.get_data(as_text=True)

        with client.application.app_context():
            from app.db import get_db
            n = get_db().execute(
                "SELECT COUNT(*) c FROM assay_version WHERE hole_id = ?", (hole_id,)
            ).fetchone()["c"]
            assert n == 3

    def test_overlapping_segments_rejected(self, app, client):
        hole_id = _make_hole(app, client)
        resp = _set_segments(client, hole_id, "0 100\n50 150")
        assert "重叠" in resp.get_data(as_text=True)

    def test_download_without_data_flashes(self, app, client):
        hole_id = _make_hole(app, client)
        resp = client.get(f"/holes/{hole_id}/supply.csv", follow_redirects=True)
        assert "尚无生效底表" in resp.get_data(as_text=True)

    def test_healthz(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


def _download_grades(client, hole_id):
    dl = client.get(f"/holes/{hole_id}/supply.csv")
    assert dl.status_code == 200
    rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
    return rows


class TestSegmentVersioning:
    """改段语义：段集不可变、历史核查保留、失败回滚、与换交串行化。"""

    def test_resegment_keeps_old_version_details_and_table(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        with app.app_context():
            from app.db import get_db
            old = get_db().execute(
                "SELECT id, segment_set_id FROM assay_version "
                "WHERE hole_id=? ORDER BY seq LIMIT 1", (hole_id,)
            ).fetchone()
            old_version_id, old_set_id = old["id"], old["segment_set_id"]

        # 改段：新段集 + 重算同一事务生效
        _set_segments(client, hole_id, "0 50\n50 100")
        with app.app_context():
            from app.db import get_db
            # 旧版本逐段核查结果必须原样保留
            n_old_results = get_db().execute(
                "SELECT COUNT(*) c FROM segment_result WHERE version_id=?",
                (old_version_id,),
            ).fetchone()["c"]
            assert n_old_results == 1
            # 旧段集及其段定义仍在
            n_old_defs = get_db().execute(
                "SELECT COUNT(*) c FROM segment_def WHERE segment_set_id=?",
                (old_set_id,),
            ).fetchone()["c"]
            assert n_old_defs == 1
            n_sets = get_db().execute(
                "SELECT COUNT(*) c FROM segment_set WHERE hole_id=?", (hole_id,)
            ).fetchone()["c"]
            assert n_sets == 2
            # 旧版本明细仍可通过服务层取出
            old_results = services.list_results(old_version_id)
            assert len(old_results) == 1 and old_results[0]["grade_num"] == 30000

        # 当前生效底表是新两段
        rows = _download_grades(client, hole_id)
        assert len(rows) == 3

    def test_resegment_compute_failure_keeps_everything(self, app, client, monkeypatch):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")

        def boom(*args, **kwargs):
            raise services.ServiceError("simulated compute failure")

        monkeypatch.setattr(services, "evaluate_all", boom)
        resp = _set_segments(client, hole_id, "0 50\n50 100")
        assert "统计段保存失败" in resp.get_data(as_text=True)

        with app.app_context():
            from app.db import get_db
            conn = get_db()
            # 无失败版本/段集残留
            assert conn.execute(
                "SELECT COUNT(*) c FROM assay_version WHERE hole_id=?", (hole_id,)
            ).fetchone()["c"] == 1
            assert conn.execute(
                "SELECT COUNT(*) c FROM segment_set WHERE hole_id=?", (hole_id,)
            ).fetchone()["c"] == 1
            # 生效分段仍是旧分段
            hole = conn.execute(
                "SELECT * FROM hole WHERE id=?", (hole_id,)
            ).fetchone()
            defs = conn.execute(
                "SELECT start_cm, end_cm FROM segment_def "
                "WHERE segment_set_id=? ORDER BY seq",
                (hole["effective_segment_set_id"],),
            ).fetchall()
            assert [tuple(r) for r in defs] == [(0, 100)]

        # 旧底表照常完整下载
        rows = _download_grades(client, hole_id)
        assert len(rows) == 2 and rows[1][5] == "300"

    def test_resegment_invalid_input_changes_nothing(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        # 重叠段在写入前即被拒绝
        resp = _set_segments(client, hole_id, "0 60\n50 100")
        assert "重叠" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            assert get_db().execute(
                "SELECT COUNT(*) c FROM segment_set WHERE hole_id=?", (hole_id,)
            ).fetchone()["c"] == 1
        rows = _download_grades(client, hole_id)
        assert rows[1][5] == "300"

    def test_resegment_without_assays_only_stores_segments(self, app, client):
        hole_id = _make_hole(app, client)
        resp = _set_segments(client, hole_id, "0 100\n100 200")
        assert "未生成底表" in resp.get_data(as_text=True)
        with app.app_context():
            from app.db import get_db
            hole = get_db().execute(
                "SELECT * FROM hole WHERE id=?", (hole_id,)
            ).fetchone()
            assert hole["effective_version_id"] is None
            n = get_db().execute(
                "SELECT COUNT(*) c FROM segment_def WHERE segment_set_id=?",
                (hole["effective_segment_set_id"],),
            ).fetchone()["c"]
            assert n == 2
        # 之后再上传化验，按该段集直接核算
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,100,否\nS2,100,200,200,否\n")
        rows = _download_grades(client, hole_id)
        assert [r[5] for r in rows[1:]] == ["100", "200"]


class TestInterleaving:
    """改段重算与换交交错：写锁串行化，互不覆盖。"""

    @pytest.fixture()
    def seeded(self, app, client):
        hole_id = _make_hole(app, client)
        _set_segments(client, hole_id, "0 100")
        _upload(client, hole_id, CSV_HEADER + "S1,0,100,300,否\n")
        return app, client, hole_id

    def _hold_inside_evaluate(self, monkeypatch, app, hold_seconds=0.8):
        """让 evaluate_all 进入后挂起，制造写事务持锁窗口。"""
        real = services.evaluate_all
        gate = threading.Event()
        started = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            gate.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(services, "evaluate_all", slow)

        def release():
            started.wait(10)
            time.sleep(hold_seconds)  # 等竞争请求进入锁等待
            gate.set()

        threading.Thread(target=release, daemon=True).start()
        return gate

    def test_recompute_then_upload_final_uses_new_segments_and_assays(
        self, seeded, monkeypatch
    ):
        app, client, hole_id = seeded
        self._hold_inside_evaluate(monkeypatch, app)
        errors = []

        def segment_worker():
            try:
                with app.app_context():  # 独立连接，模拟另一 worker 线程
                    services.replace_segments(hole_id, [(0, 50), (50, 100)])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        t = threading.Thread(target=segment_worker)
        t.start()
        time.sleep(0.3)  # 改段事务持锁并卡在核算中
        # 换交在锁上排队；新化验按新两段拆分，保证铺满
        resp = _upload(
            client, hole_id,
            CSV_HEADER + "S1,0,50,999,是\nS2,50,100,999,是\n",
        )
        t.join()
        assert not errors
        assert "换交成功" in resp.get_data(as_text=True)

        with app.app_context():
            from app.db import get_db
            eff = get_db().execute(
                "SELECT v.seq, v.segment_set_id, h.effective_segment_set_id, "
                "r.grade_ppm, r.recheck FROM hole h "
                "JOIN assay_version v ON v.id = h.effective_version_id "
                "JOIN assay_row r ON r.version_id = v.id WHERE h.id=?",
                (hole_id,),
            ).fetchone()
            # 生效的是换交版本，化验未被旧重算覆盖
            assert eff["grade_ppm"] == 999 and eff["recheck"] == 1
            assert eff["segment_set_id"] == eff["effective_segment_set_id"]
            # 历史三个版本（初版/改段重算/换交）核查行分别为 1/2/2，全部保留
            hist = get_db().execute(
                "SELECT v.seq, COUNT(sr.id) n FROM assay_version v "
                "LEFT JOIN segment_result sr ON sr.version_id = v.id "
                "WHERE v.hole_id=? GROUP BY v.seq ORDER BY v.seq", (hole_id,),
            ).fetchall()
            assert [(r["seq"], r["n"]) for r in hist] == [(1, 1), (2, 2), (3, 2)]

        rows = _download_grades(client, hole_id)
        assert len(rows) == 3 and all(r[5] == "999" for r in rows[1:])

    def test_upload_then_recompute_final_uses_new_segments_and_assays(
        self, seeded, monkeypatch
    ):
        app, client, hole_id = seeded
        self._hold_inside_evaluate(monkeypatch, app)
        errors = []

        def upload_worker():
            try:
                with app.app_context():
                    from app.csvio import parse_assay_csv
                    services.upload_assays(
                        hole_id,
                        parse_assay_csv(
                            CSV_HEADER + "S1,0,25,777,是\nS2,25,100,777,是\n"
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        t = threading.Thread(target=upload_worker)
        t.start()
        time.sleep(0.3)  # 换交事务持锁并卡在核算中
        # 改段重算在锁上排队，换交提交后基于最新化验核算
        with app.app_context():
            outcome = services.replace_segments(hole_id, [(0, 25), (25, 100)])
        t.join()
        assert not errors
        assert outcome["version_id"] is not None

        with app.app_context():
            from app.db import get_db
            grades = [
                r["grade_ppm"]
                for r in get_db().execute(
                    "SELECT r.grade_ppm FROM hole h "
                    "JOIN assay_row r ON r.version_id = h.effective_version_id "
                    "WHERE h.id=? ORDER BY r.sample_no", (hole_id,)
                ).fetchall()
            ]
            assert grades == [777, 777]
        rows = _download_grades(client, hole_id)
        assert len(rows) == 3 and all(r[5] == "777" for r in rows[1:])

    def test_download_still_served_while_write_in_flight(self, seeded, monkeypatch):
        app, client, hole_id = seeded
        real = services.evaluate_all
        gate = threading.Event()
        started = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            gate.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(services, "evaluate_all", slow)

        def worker():
            with app.app_context():
                services.recompute(hole_id)

        t = threading.Thread(target=worker)
        t.start()
        started.wait(10)
        time.sleep(0.3)
        try:
            t0 = time.time()
            dl = client.get(f"/holes/{hole_id}/supply.csv")
            page = client.get(f"/holes/{hole_id}")
            elapsed = time.time() - t0
        finally:
            gate.set()
            t.join()
        # 写事务进行中读请求不被阻塞，读到的是上一版完整底表
        assert elapsed < 1.0
        assert dl.status_code == 200 and page.status_code == 200
        rows = list(csv.reader(io.StringIO(dl.data.decode("utf-8-sig"))))
        assert len(rows) == 2 and rows[1][5] == "300"
