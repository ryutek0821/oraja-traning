from __future__ import annotations

import sqlite3

import pytest

from oraja_training.collect.backfill import run


@pytest.fixture(scope="module")
def backfilled(player_db_dir, tmp_path_factory):
    assistant_db = tmp_path_factory.mktemp("backfill") / "assistant.db"
    result = run(player_db_dir, assistant_db, clock=lambda: 1_700_000_000)
    return assistant_db, result


def test_backfill_legacy_acceptance_values(backfilled) -> None:
    assistant_db, result = backfilled
    assert result.charts == 65_998
    assert result.ir_imports == 542
    assert result.legacy_plays == 384
    assert result.skipped_ir_noplay == 3
    assert result.song_rows_seen == 66_156

    conn = sqlite3.connect(assistant_db)
    try:
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot'"
        ).fetchone()[0] == 384
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND is_course = 1"
        ).fetchone()[0] == 12
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND survival > 1.0"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT max(survival) FROM plays "
            "WHERE source = 'legacy_last_snapshot'"
        ).fetchone()[0] == 1.0
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND abs(survival - 1.0) < 1e-12"
        ).fetchone()[0] == 365
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND completed = 1"
        ).fetchone()[0] == 368
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND clear = 1 AND completed = 1"
        ).fetchone()[0] == 84
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND clear >= 2 AND judged = notes"
        ).fetchone()[0] == 272
        empty_poor = conn.execute(
            "SELECT sum(empty_poor), max(empty_poor), "
            "sum(empty_poor = 0) FROM plays "
            "WHERE source = 'legacy_last_snapshot'"
        ).fetchone()
        assert empty_poor == (11_255, 168, 2)

        gauge_counts = dict(
            conn.execute(
                "SELECT credited_gauge_kind, count(*) FROM plays "
                "WHERE source = 'legacy_last_snapshot' "
                "GROUP BY credited_gauge_kind"
            )
        )
        assert gauge_counts == {
            None: 148,
            "EASY": 78,
            "NORMAL": 37,
            "HARD": 99,
            "EXHARD": 22,
        }
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND selected_gauge_kind IS NOT NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND credited_gauge_kind IN ('ASSIST_EASY', 'HAZARD')"
        ).fetchone()[0] == 0

        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import' AND "
            "(judged IS NOT NULL OR empty_poor IS NOT NULL OR "
            "survival IS NOT NULL OR completed IS NOT NULL)"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_backfill_follows_primary_key_and_noplay_rules(backfilled) -> None:
    assistant_db, result = backfilled

    # The fixture contains 66,156 song paths but only 65,998 content hashes.
    # charts.sha256 is the canonical primary key in SPEC A-4.
    assert result.charts == 65_998

    # Three of the 545 date=0 score rows are clear=0/playcount=0 NoPlay rows.
    # SPEC 3.1 says these are not plays and must not enter plays.
    assert result.ir_imports == 542
    assert result.skipped_ir_noplay == 3

    conn = sqlite3.connect(assistant_db)
    try:
        assert conn.execute("SELECT count(*) FROM charts").fetchone()[0] == 65_998
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import'"
        ).fetchone()[0] == 542
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import' AND clear = 0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_backfill_is_idempotent(backfilled, player_db_dir) -> None:
    assistant_db, _ = backfilled
    second = run(player_db_dir, assistant_db, clock=lambda: 1_700_000_001)
    assert second.charts == 65_998
    assert second.legacy_plays == 384
    assert second.ir_imports == 542


def test_backfill_refuses_source_as_destination(player_db_dir) -> None:
    with pytest.raises(ValueError):
        run(player_db_dir, player_db_dir / "score.db")
