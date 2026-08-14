from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from oraja_training.db import readers


def test_snapshot_readers_detect_runtime_columns(synthetic_db_dir) -> None:
    with closing(readers.open_snapshot(synthetic_db_dir / "scorelog.db")) as conn:
        columns = readers.table_columns(conn, "scorelog")
        assert "avgjudge" not in columns
        assert "oldavgjudge" not in columns
        rows = readers.read_scorelog(conn)
        assert len(rows) == 1
        assert set(rows[0]) == columns


def test_snapshot_connection_rejects_writes(synthetic_db_dir) -> None:
    with closing(readers.open_snapshot(synthetic_db_dir / "score.db")) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE score SET clear = clear")


def test_score_and_song_readers(synthetic_db_dir) -> None:
    with closing(readers.open_snapshot(synthetic_db_dir / "score.db")) as conn:
        player = readers.read_player(conn)
        assert len(player) == 2
        latest = player[-1]
        assert latest["playcount"] == 12
        judged = sum(
            int(latest[column])
            for column in (
                "epg", "lpg", "egr", "lgr", "egd", "lgd",
                "ebd", "lbd", "epr", "lpr",
            )
        )
        assert judged == 140

    with closing(readers.open_snapshot(synthetic_db_dir / "scoredatalog.db")) as conn:
        rows = readers.read_scoredatalog(conn)
        assert len(rows) == 4
        assert sum(row["date"] > 0 for row in rows) == 3

    with closing(readers.open_snapshot(synthetic_db_dir / "songdata.db")) as conn:
        sample_hash = "1" * 64
        matching = readers.read_songs(conn, [sample_hash])
        assert len(matching) == 2
        assert {row["sha256"] for row in matching} == {sample_hash}
        assert readers.read_songs(conn, []) == []
        patterns = readers.read_chart_patterns(conn)
        assert len(patterns) == 2
        assert patterns[0]["analysis_version"] == 3

    with closing(readers.open_snapshot(synthetic_db_dir / "songinfo.db")) as conn:
        assert len(readers.read_songinfo(conn)) == 4
