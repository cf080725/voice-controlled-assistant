# -*- coding: utf-8 -*-
"""
Tests for Canaan and OpenDots Bidirectional Memory Bridge
"""
import os
import sqlite3
import pytest
from unittest.mock import patch, MagicMock
from canaan_dots_memory_bridge import CanaanDotsMemoryBridge


@pytest.fixture
def temp_memory_dbs(tmp_path):
    canaan_db = str(tmp_path / "canaan_mem.db")
    dots_db = str(tmp_path / "dots_mem.sqlite")

    # Init Canaan schema
    conn_c = sqlite3.connect(canaan_db)
    conn_c.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT, category TEXT, pinned INTEGER, superseded_by INTEGER)"
    )
    conn_c.execute(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT, started_at INTEGER, ended_at INTEGER)"
    )
    conn_c.execute(
        "INSERT INTO facts (text, category, pinned, superseded_by) VALUES ('主人喜欢黑夜编程', 'habit', 1, NULL)"
    )
    conn_c.execute(
        "INSERT INTO facts (text, category, pinned, superseded_by) VALUES ('开发主力机是RTX 5060', 'fact', 0, NULL)"
    )
    conn_c.execute(
        "INSERT INTO facts (text, category, pinned, superseded_by) VALUES ('旧信息不应同步', 'fact', 0, 999)"
    )
    conn_c.commit()
    conn_c.close()

    # Init Dots schema
    conn_d = sqlite3.connect(dots_db)
    conn_d.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, text TEXT NOT NULL, createdAt INTEGER NOT NULL)"
    )
    conn_d.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL, error TEXT, updatedAt INTEGER)"
    )
    conn_d.execute(
        "INSERT INTO tasks (id, prompt, status, error, updatedAt) VALUES ('t1', '多智能体架构调研已完成', 'completed', NULL, 1790000000)"
    )
    conn_d.commit()
    conn_d.close()

    return canaan_db, dots_db


def test_bridge_default_is_disabled_without_reading_either_database(temp_memory_dbs, monkeypatch):
    canaan_db, dots_db = temp_memory_dbs
    bridge = CanaanDotsMemoryBridge(canaan_db_path=canaan_db, dots_db_path=dots_db)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("默认关闭时不应读私人数据库"))
    assert bridge.sync_canaan_to_dots() == bridge.sync_dots_to_canaan() == 0
    bridge.start_background_sync()
    assert bridge._thread is None


def test_enabled_bridge_stops_in_incognito(temp_memory_dbs):
    canaan_db, dots_db = temp_memory_dbs
    with sqlite3.connect(canaan_db) as db:
        db.execute("CREATE TABLE memory_settings(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO memory_settings VALUES('incognito','1')")
    bridge = CanaanDotsMemoryBridge(canaan_db_path=canaan_db, dots_db_path=dots_db, enabled=True)
    assert bridge.sync_canaan_to_dots() == bridge.sync_dots_to_canaan() == 0


def test_sync_canaan_to_dots(temp_memory_dbs):
    canaan_db, dots_db = temp_memory_dbs
    bridge = CanaanDotsMemoryBridge(canaan_db_path=canaan_db, dots_db_path=dots_db, enabled=True)

    # First sync: should sync 2 active facts (excluding superseded)
    count = bridge.sync_canaan_to_dots()
    assert count == 2

    # Second sync: deduplication check -> 0 new facts
    count_again = bridge.sync_canaan_to_dots()
    assert count_again == 0

    conn_d = sqlite3.connect(dots_db)
    cur = conn_d.cursor()
    cur.execute("SELECT text FROM memories")
    rows = [r[0] for r in cur.fetchall()]
    conn_d.close()

    assert any("主人喜欢黑夜编程" in r for r in rows)
    assert any("RTX 5060" in r for r in rows)
    assert not any("旧信息不应同步" in r for r in rows)


def test_sync_dots_to_canaan(temp_memory_dbs):
    canaan_db, dots_db = temp_memory_dbs
    bridge = CanaanDotsMemoryBridge(canaan_db_path=canaan_db, dots_db_path=dots_db, enabled=True)

    # Ingest completed tasks into Canaan episodes
    count = bridge.sync_dots_to_canaan()
    assert count == 1

    # Ingest again -> deduplication check -> 0
    count_again = bridge.sync_dots_to_canaan()
    assert count_again == 0

    conn_c = sqlite3.connect(canaan_db)
    cur = conn_c.cursor()
    cur.execute("SELECT summary FROM episodes")
    summaries = [r[0] for r in cur.fetchall()]
    conn_c.close()

    assert any("多智能体架构调研已完成" in s for s in summaries)


def test_query_unified_with_dots_knowledge(temp_memory_dbs):
    canaan_db, dots_db = temp_memory_dbs
    bridge = CanaanDotsMemoryBridge(canaan_db_path=canaan_db, dots_db_path=dots_db, enabled=True)
    bridge.sync_canaan_to_dots()

    results = bridge.query_unified("RTX")
    assert isinstance(results, list)
    assert any("RTX" in r.get("text", "") for r in results)
