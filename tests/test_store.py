from __future__ import annotations

import sqlite3

import pytest

from oraja_training.db import store


def test_init_creates_complete_versioned_schema(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "charts",
            "plays",
            "chart_cursor",
            "collector_state",
            "chart_features",
            "table_entries",
            "model_state",
            "predictions",
            "sessions",
            "revisits",
            "daily_imports",
            "player_daily",
            "score_state",
            "score_changes",
            "table_sources",
            "recommendation_versions",
        }.issubset(tables)
        play_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(plays)")
        }
        assert {
            "empty_poor",
            "credited_gauge_kind",
            "selected_gauge_kind",
        }.issubset(play_columns)
        assert {"gauge", "gauge_source"}.isdisjoint(play_columns)
        store.migrate(conn)
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_refuses_beatoraja_database_names(tmp_path) -> None:
    with pytest.raises(ValueError):
        store.init(tmp_path / "score.db")
    assert not (tmp_path / "score.db").exists()


def test_init_rejects_v1_and_requires_a_fresh_backfill(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match=r"schema_version 1.*delete.*backfill"):
        store.init(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_migrates_v2_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(store.SCHEMA_V2)
        conn.execute("INSERT INTO schema_version VALUES (2)")
        conn.execute(
            "INSERT INTO sessions(created_at, arm, slots_json) VALUES (1, 'A', '[]')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_imports'"
        ).fetchone()
    finally:
        conn.close()
