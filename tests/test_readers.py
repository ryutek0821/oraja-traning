from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from oraja_training.db import readers


def test_snapshot_readers_detect_runtime_columns(player_db_dir) -> None:
    with closing(readers.open_snapshot(player_db_dir / "scorelog.db")) as conn:
        columns = readers.table_columns(conn, "scorelog")
        assert "avgjudge" not in columns
        assert "oldavgjudge" not in columns
        rows = readers.read_scorelog(conn)
        assert len(rows) == 465
        assert set(rows[0]) == columns


def test_snapshot_connection_rejects_writes(player_db_dir) -> None:
    with closing(readers.open_snapshot(player_db_dir / "score.db")) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE score SET clear = clear")


def test_score_and_song_readers(player_db_dir) -> None:
    with closing(readers.open_snapshot(player_db_dir / "score.db")) as conn:
        player = readers.read_player(conn)
        assert len(player) == 26
        latest = player[-1]
        judged = sum(
            int(latest[column])
            for column in (
                "epg", "lpg", "egr", "lgr", "egd", "lgd",
                "ebd", "lbd", "epr", "lpr",
            )
        )
        assert judged == 1_080_762

    with closing(readers.open_snapshot(player_db_dir / "scoredatalog.db")) as conn:
        rows = readers.read_scoredatalog(conn)
        assert len(rows) == 384
        assert all(row["date"] > 0 for row in rows)

    with closing(readers.open_snapshot(player_db_dir / "songdata.db")) as conn:
        sample_hash = conn.execute("SELECT sha256 FROM song LIMIT 1").fetchone()[0]
        matching = readers.read_songs(conn, [sample_hash])
        assert matching
        assert {row["sha256"] for row in matching} == {sample_hash}
        assert readers.read_songs(conn, []) == []
