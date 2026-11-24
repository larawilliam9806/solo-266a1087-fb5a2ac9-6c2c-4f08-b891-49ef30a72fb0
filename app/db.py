# -*- coding: utf-8 -*-
"""SQLite 连接、建表与事务工具。

表结构围绕「不可变版本快照」设计：

- ``segment_set`` / ``segment_def``：统计段整体版本化。每次保存统计段都插入
  一套新的段定义，旧段集原样保留——历史化验版本的逐段核查结果（segment_result）
  始终通过 ``assay_version.segment_set_id`` 指向其核算时所用的段集，绝不因改段
  而被删除或覆盖。
- ``assay_version`` / ``assay_row`` / ``segment_result``：一次换交或重算在同一
  事务内写入整套快照，并原子翻转 ``hole.effective_version_id``。

所有写操作都通过 :func:`immediate_transaction` 以 ``BEGIN IMMEDIATE`` 开启，
先取库级写锁再读数据，从而把「改段重算」与「换交」串行化：交错提交时后到的
操作等待先到者完成后再基于最新状态核算，不存在旧快照覆盖新化验的窗口。
SQLite 启用 WAL：写事务进行期间，下载/浏览等读请求不被阻塞，读到的始终是
上一个已提交的生效版本。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from flask import g, current_app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        # WAL：写事务（重算/换交/改段）进行时，读请求仍可访问上一已提交版本。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # 由我们显式管理 BEGIN/COMMIT，关闭 Python sqlite3 的隐式事务。
        conn.isolation_level = None
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """以 ``BEGIN IMMEDIATE`` 开启写事务的上下文管理器。

    进入即获取写锁（拿不到则在 busy_timeout 内等待，而不是先持旧读快照），
    保证事务内读到的都是最新已提交状态；正常退出提交，异常则整体回滚。
    锁竞争超时统一转成 ServiceError 语义的 :class:`sqlite3.OperationalError`
    提示，由路由层向用户呈现“稍后重试”。
    """
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS hole (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    effective_version_id INTEGER,
    effective_segment_set_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segment_set (
    id INTEGER PRIMARY KEY,
    hole_id INTEGER NOT NULL REFERENCES hole(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segment_def (
    id INTEGER PRIMARY KEY,
    segment_set_id INTEGER NOT NULL REFERENCES segment_set(id) ON DELETE CASCADE,
    hole_id INTEGER NOT NULL REFERENCES hole(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    start_cm INTEGER NOT NULL,
    end_cm INTEGER NOT NULL,
    UNIQUE(segment_set_id, seq),
    CHECK (end_cm > start_cm)
);

CREATE TABLE IF NOT EXISTS assay_version (
    id INTEGER PRIMARY KEY,
    hole_id INTEGER NOT NULL REFERENCES hole(id) ON DELETE CASCADE,
    segment_set_id INTEGER NOT NULL REFERENCES segment_set(id),
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
    segment_def_id INTEGER NOT NULL REFERENCES segment_def(id),
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

CREATE INDEX IF NOT EXISTS idx_segment_def_set ON segment_def(segment_set_id);
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
