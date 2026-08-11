from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from oraja_training.collect.backfill import run


@pytest.fixture(scope="module")
def backfilled(synthetic_db_dir, tmp_path_factory):
    assistant_db = tmp_path_factory.mktemp("backfill") / "assistant.db"
    result = run(synthetic_db_dir, assistant_db, clock=lambda: 1_700_000_000)
    return assistant_db, result


def _golden() -> dict:
    return json.loads(
        (Path(__file__).parent / "golden/synthetic_backfill.json").read_text()
    )


def test_backfill_legacy_acceptance_values(backfilled) -> None:
    assistant_db, result = backfilled
    expected = _golden()["backfill"]
    assert {
        "charts": result.charts,
        "legacy_plays": result.legacy_plays,
        "ir_imports": result.ir_imports,
        "song_rows_seen": result.song_rows_seen,
        "skipped_ir_noplay": result.skipped_ir_noplay,
    } == expected["result"]

    conn = sqlite3.connect(assistant_db)
    try:
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot'"
        ).fetchone()[0] == expected["legacy"]["rows"]
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND is_course = 1"
        ).fetchone()[0] == expected["legacy"]["course_rows"]
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND survival > 1.0"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT max(survival) FROM plays "
            "WHERE source = 'legacy_last_snapshot'"
        ).fetchone()[0] == expected["legacy"]["max_survival"]
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND abs(survival - 1.0) < 1e-12"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND completed = 1"
        ).fetchone()[0] == expected["legacy"]["completed_rows"]
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND clear >= 2 AND judged = notes"
        ).fetchone()[0] == expected["legacy"]["full_judgement_rows"]
        empty_poor = conn.execute(
            "SELECT sum(empty_poor), max(empty_poor), "
            "sum(empty_poor = 0) FROM plays "
            "WHERE source = 'legacy_last_snapshot'"
        ).fetchone()
        assert empty_poor == (
            expected["legacy"]["empty_poor_sum"],
            expected["legacy"]["empty_poor_max"],
            expected["legacy"]["zero_empty_poor_rows"],
        )

        gauge_counts = dict(
            conn.execute(
                "SELECT credited_gauge_kind, count(*) FROM plays "
                "WHERE source = 'legacy_last_snapshot' "
                "GROUP BY credited_gauge_kind"
            )
        )
        assert gauge_counts == {
            None if key == "null" else key: value
            for key, value in expected["legacy"]["credited_gauge_counts"].items()
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
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot' "
            "AND exceeded_aggregate_score = 1"
        ).fetchone()[0] == expected["legacy"]["exceeded_aggregate_rows"]
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import' "
            "AND judged IS NULL AND empty_poor IS NULL AND survival IS NULL "
            "AND completed IS NULL"
        ).fetchone()[0] == expected["legacy"]["ir_null_derived_rows"]
    finally:
        conn.close()


def test_backfill_follows_primary_key_and_noplay_rules(backfilled) -> None:
    assistant_db, result = backfilled

    # The fixture contains five song paths but only four content hashes.
    # charts.sha256 is the canonical primary key in SPEC A-4.
    assert result.charts == 4

    # Two of the four date=0 score rows are clear=0/playcount=0 NoPlay rows.
    # SPEC 3.1 says these are not plays and must not enter plays.
    assert result.ir_imports == 2
    assert result.skipped_ir_noplay == 2

    conn = sqlite3.connect(assistant_db)
    try:
        assert conn.execute("SELECT count(*) FROM charts").fetchone()[0] == 4
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM plays WHERE source = 'ir_import' AND clear = 0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_backfill_is_idempotent(backfilled, synthetic_db_dir) -> None:
    assistant_db, _ = backfilled
    second = run(synthetic_db_dir, assistant_db, clock=lambda: 1_700_000_001)
    assert second.charts == 4
    assert second.legacy_plays == 4
    assert second.ir_imports == 2


def test_backfill_refuses_source_as_destination(synthetic_db_dir) -> None:
    with pytest.raises(ValueError):
        run(synthetic_db_dir, synthetic_db_dir / "score.db")
