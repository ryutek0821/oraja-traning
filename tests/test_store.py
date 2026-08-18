from __future__ import annotations

import sqlite3

import pytest

from oraja_training.db import store
from oraja_training.filesystem import sqlite_file_identity


def test_init_creates_complete_versioned_schema(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
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
            "replay_metadata",
            "replay_scan_state",
            "experiments",
            "experiment_sessions",
            "experiment_targets",
            "chart_pattern_features",
            "classification_sources",
            "chart_classifications",
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
        experiment_session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(experiment_sessions)")
        }
        assert "input_candidate_hash" in experiment_session_columns
        table_source_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(table_sources)")
        }
        assert {"entry_count", "matched_count"}.issubset(table_source_columns)
        pattern_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(chart_pattern_features)")
        }
        assert {"grid_bpm", "stream_sec", "last_kill"}.issubset(pattern_columns)
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
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_imports'"
        ).fetchone()
    finally:
        conn.close()


def test_init_migrates_v3_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(store.SCHEMA_V2 + store.SCHEMA_V3)
        conn.execute("INSERT INTO schema_version VALUES (3)")
        conn.execute(
            "INSERT INTO sessions(created_at, arm, slots_json) VALUES (1, 'A', '[]')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_metadata'"
        ).fetchone()
    finally:
        conn.close()


def test_init_migrates_v4_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4)
        conn.execute("INSERT INTO schema_version VALUES (4)")
        conn.execute(
            "INSERT INTO sessions(created_at, arm, slots_json) VALUES (1, 'A', '[]')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='experiment_targets'"
        ).fetchone()
    finally:
        conn.close()


def test_init_migrates_v5_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4 + store.SCHEMA_V5
        )
        conn.execute("INSERT INTO schema_version VALUES (5)")
        conn.execute(
            "INSERT INTO sessions(created_at, arm, slots_json) VALUES (1, 'A', '[]')"
        )
        conn.execute(
            """
            INSERT INTO experiments(
              id, name, seed, starts_at, ends_at, min_samples_per_arm, created_at
            ) VALUES (1, 'legacy', 'seed', 1, 100, 1, 1)
            """
        )
        sha256 = "1" * 64
        conn.execute(
            """
            INSERT INTO experiment_sessions(
              id, experiment_id, session_key, session_at, arm, arm_probability,
              selection_probability, candidate_hash, candidates_json,
              selected_sha256, selected_mode, selected_p_pred,
              transfer_sha256, transfer_mode, transfer_p_pred, assigned_at
            ) VALUES (1, 1, 'legacy', 2, 'coach', 0.5, 0.5, 'hash', '{}',
                      ?, 1, 0.5, ?, 1, 0.5, 2)
            """,
            (sha256, sha256),
        )
        conn.execute(
            """
            INSERT INTO experiment_targets(
              session_id, target_kind, interval_days, sha256, mode,
              due_at, window_closes_at, p_pred
            ) VALUES (1, 'transfer', 3, ?, 1, 10, 20, 0.5)
            """,
            (sha256,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='chart_pattern_features'"
        ).fetchone()
        assert tuple(
            conn.execute(
                "SELECT sha256, mode, interval_days FROM experiment_targets"
            ).fetchone()
        ) == ("1" * 64, 1, 3)
        assert conn.execute(
            "SELECT input_candidate_hash FROM experiment_sessions"
        ).fetchone()[0] == "hash"
    finally:
        conn.close()


def test_init_migrates_v6_table_coverage_columns(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4
            + store.SCHEMA_V5 + store.SCHEMA_V6
        )
        conn.execute("INSERT INTO schema_version VALUES (6)")
        conn.execute(
            "INSERT INTO table_sources(table_id, page_url) VALUES ('satellite', 'url')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        columns = {row[1] for row in conn.execute("PRAGMA table_info(table_sources)")}
        assert {"entry_count", "matched_count"}.issubset(columns)
        assert conn.execute(
            "SELECT page_url FROM table_sources WHERE table_id='satellite'"
        ).fetchone()[0] == "url"
    finally:
        conn.close()


def test_init_migrates_v7_pattern_safety_columns_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4
            + store.SCHEMA_V5 + store.SCHEMA_V6 + store.SCHEMA_V7
        )
        conn.execute("INSERT INTO schema_version VALUES (7)")
        conn.execute(
            "INSERT INTO chart_pattern_features(sha256) VALUES (?)",
            ("a" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(chart_pattern_features)")
        }
        assert {"grid_bpm", "stream_sec", "last_kill"}.issubset(columns)
        assert conn.execute(
            "SELECT sha256 FROM chart_pattern_features"
        ).fetchone()[0] == "a" * 64
    finally:
        conn.close()


def test_replay_scan_state_normalizes_unsigned_windows_file_ids(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        store.upsert_replay_scan_states(
            conn,
            [
                {
                    "path": "slot.brd",
                    "device": 16_012_189_180_544_750_605,
                    "inode": (1 << 127) + 17,
                    "mtime_ns": 3,
                    "compressed_size": 4,
                    "outcome": "valid",
                    "checked_at": 5,
                }
            ],
        )
        conn.commit()

        expected = sqlite_file_identity(
            16_012_189_180_544_750_605,
            (1 << 127) + 17,
        )
        assert store.load_replay_scan_states(conn)["slot.brd"] == (
            expected[0],
            expected[1],
            3,
            4,
            "valid",
        )
    finally:
        conn.close()


def test_init_migrates_v8_replay_scan_state_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4
            + store.SCHEMA_V5 + store.SCHEMA_V6 + store.SCHEMA_V7
            + store.SCHEMA_V8
        )
        conn.execute("INSERT INTO schema_version VALUES (8)")
        conn.execute(
            "INSERT INTO replay_metadata("
            "path, content_hash, observed_at, mtime_ns, compressed_size, sha256, "
            "mode, played_at, gauge, selected_gauge_kind, match_status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("slot.brd", "hash", 1, 2, 3, "a" * 64, 0, 4, 3, "HARD", "unmatched"),
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute(
            "SELECT path FROM replay_metadata"
        ).fetchone()[0] == "slot.brd"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='replay_scan_state'"
        ).fetchone()
        assert "input_candidate_hash" in {
            row[1] for row in conn.execute("PRAGMA table_info(experiment_sessions)")
        }
    finally:
        conn.close()


def test_init_migrates_v9_experiment_input_hash_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4
            + store.SCHEMA_V5 + store.SCHEMA_V6 + store.SCHEMA_V7
            + store.SCHEMA_V8 + store.SCHEMA_V9
        )
        conn.execute("INSERT INTO schema_version VALUES (9)")
        conn.execute(
            """
            INSERT INTO experiments(
              id, name, seed, starts_at, ends_at, min_samples_per_arm, created_at
            ) VALUES (1, 'legacy-v9', 'seed', 1, 100, 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO experiment_sessions(
              id, experiment_id, session_key, session_at, arm, arm_probability,
              selection_probability, candidate_hash, candidates_json,
              selected_sha256, selected_mode, selected_p_pred,
              transfer_sha256, transfer_mode, transfer_p_pred, assigned_at
            ) VALUES (1, 1, 'legacy-v9', 2, 'coach', 0.5, 0.5,
                      'effective-hash', '{}', ?, 1, 0.5, ?, 1, 0.5, 2)
            """,
            ("1" * 64, "2" * 64),
        )
        conn.execute(
            """
            INSERT INTO replay_scan_state(
              path, device, inode, mtime_ns, compressed_size, outcome, checked_at
            ) VALUES ('slot.brd', 1, 2, 3, 4, 'valid', 5)
            """
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute(
            "SELECT input_candidate_hash FROM experiment_sessions"
        ).fetchone()[0] == "effective-hash"
        assert conn.execute(
            "SELECT outcome FROM replay_scan_state WHERE path='slot.brd'"
        ).fetchone()[0] == "valid"
    finally:
        conn.close()


def test_init_migrates_v10_classification_tables_additively(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            store.SCHEMA_V2 + store.SCHEMA_V3 + store.SCHEMA_V4
            + store.SCHEMA_V5 + store.SCHEMA_V6 + store.SCHEMA_V7
            + store.SCHEMA_V8 + store.SCHEMA_V9 + store.SCHEMA_V10
        )
        conn.execute("INSERT INTO schema_version VALUES (10)")
        conn.execute(
            "INSERT INTO charts VALUES (?, NULL, 'Chart', NULL, 1, 7, NULL, 1)",
            ("a" * 64,),
        )
        conn.commit()
    finally:
        conn.close()

    conn = store.init(path)
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 11
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='chart_classifications'"
        ).fetchone()
        assert conn.execute("SELECT title FROM charts").fetchone()[0] == "Chart"
    finally:
        conn.close()
