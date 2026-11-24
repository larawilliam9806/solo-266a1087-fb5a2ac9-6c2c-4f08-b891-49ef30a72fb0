# -*- coding: utf-8 -*-
"""SQLite 连接与建表。

表结构围绕「版本快照」设计：一次换交（按样号替换化验集）在同一事务中
写入 assay_version / assay_row / segment_result，并原子翻转 hole.effective_version_id。
"""
from __future__ import annotations

import os
import sqlite3

from flask import g, current_app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        # WAL：写事务（重算）进行时，读请求仍可访问上一已提交版本。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS hole (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    effective_version_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segment_def (
    id INTEGER PRIMARY KEY,
    hole_id INTEGER NOT NULL REFERENCES hole(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    start_cm INTEGER NOT NULL,
    end_cm INTEGER NOT NULL,
    UNIQUE(hole_id, seq),
    CHECK (end_cm > start_cm)
);

CREATE TABLE IF NOT EXISTS assay_version (
    id INTEGER PRIMARY KEY,
    hole_id INTEGER NOT NULL REFERENCES hole(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('effective', 'superseded', 'failed')),
    change_note TEXT NOT NULL DEFAULT '',
    added_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(hole_id, seq)
);

CREATE TABLE IF NOT EXISTS assay_row (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES assay_version(id) ON DELETE CASCADE,
    sample_no TEXT NOT NULL,
    start_cm INTEGER NOT NULL,
    end_cm INTEGER NOT NULL,
    grade_ppm INTEGER NOT NULL,
    recheck INTEGER NOT NULL CHECK (recheck IN (0, 1)),
    UNIQUE(version_id, sample_no),
    CHECK (end_cm > start_cm),
    CHECK (grade_ppm >= 0)
);

CREATE TABLE IF NOT EXISTS segment_result (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES assay_version(id) ON DELETE CASCADE,
    segment_def_id INTEGER NOT NULL REFERENCES segment_def(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    start_cm INTEGER NOT NULL,
    end_cm INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'diverged', 'incomplete')),
    -- 精确品位 = grade_num / grade_den（分母即段长，铺满时等于 end-start）
    grade_num INTEGER,
    grade_den INTEGER,
    recheck_cover_cm INTEGER NOT NULL DEFAULT 0,
    covered_cm INTEGER NOT NULL DEFAULT 0,
    -- ok: 一套样号；diverged: 最小/最大加权和各一套
    sample_path_min TEXT,
    sample_path_max TEXT,
    grade_num_min INTEGER,
    grade_num_max INTEGER,
    UNIQUE(version_id, segment_def_id)
);

CREATE INDEX IF NOT EXISTS idx_assay_row_version ON assay_row(version_id);
CREATE INDEX IF NOT EXISTS idx_segment_result_version ON segment_result(version_id);
"""


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
