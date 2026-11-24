# -*- coding: utf-8 -*-
"""端到端测试：建孔 -> 定义统计段 -> 换交上传 -> 核查 -> 下载 -> 再换交 -> 失败回滚。"""
import csv
import io

import pytest

from app import create_app


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
