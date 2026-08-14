from __future__ import annotations

from pathlib import Path
import gzip
import json
import sqlite3

from oraja_training.collect.poller import Poller
from oraja_training.db import readers
from oraja_training.db import store


SCORE_DDL = """
CREATE TABLE {table} (
  sha256 TEXT NOT NULL,
  mode INTEGER NOT NULL,
  clear INTEGER,
  epg INTEGER, lpg INTEGER, egr INTEGER, lgr INTEGER,
  egd INTEGER, lgd INTEGER, ebd INTEGER, lbd INTEGER,
  epr INTEGER, lpr INTEGER, ems INTEGER, lms INTEGER,
  notes INTEGER, combo INTEGER, minbp INTEGER, avgjudge INTEGER,
  playcount INTEGER, clearcount INTEGER, trophy TEXT, ghost TEXT,
  option INTEGER, seed INTEGER, random INTEGER, date INTEGER,
  state INTEGER, scorehash TEXT,
  PRIMARY KEY(sha256, mode)
)
"""


def _row(playcount: int = 5, *, minbp: int = 3, date: int = 100) -> dict:
    return {
        "sha256": "a" * 64,
        "mode": 0,
        "clear": 4,
        "epg": 50,
        "lpg": 50,
        "egr": 0,
        "lgr": 0,
        "egd": 0,
        "lgd": 0,
        "ebd": 0,
        "lbd": 0,
        "epr": 0,
        "lpr": 0,
        "ems": 0,
        "lms": 0,
        "notes": 100,
        "combo": 100,
        "minbp": minbp,
        "avgjudge": 0,
        "playcount": playcount,
        "clearcount": playcount,
        "trophy": "r",
        "ghost": "",
        "option": 1,
        "seed": 2,
        "random": 3,
        "date": date,
        "state": 0,
        "scorehash": "",
    }


def _put(path: Path, table: str, row: dict, *, create: bool = False) -> None:
    conn = sqlite3.connect(path)
    try:
        if create:
            conn.execute(SCORE_DDL.format(table=table))
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in row
            if column not in {"sha256", "mode"}
        )
        conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(sha256, mode) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        conn.commit()
    finally:
        conn.close()


def _source_dir(tmp_path: Path) -> Path:
    source = tmp_path / "player"
    source.mkdir()
    initial = _row()
    _put(source / "scoredatalog.db", "scoredatalog", initial, create=True)
    _put(source / "score.db", "score", initial, create=True)
    return source


def _replay(path: Path, *, sha256: str = "a" * 64, date: int = 100, gauge: int = 3) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(
        gzip.compress(
            json.dumps(
                {
                    "sha256": sha256,
                    "mode": 0,
                    "date": date,
                    "gauge": gauge,
                    "randomoption": 4,
                    "randomoptionseed": 99,
                    "keyinput": "private-replay-body",
                }
            ).encode()
        )
    )


def test_playcount_gap_generation_change_and_idempotency(tmp_path) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"
    ticks = iter([1_000, 1_001, 1_002, 1_003, 1_004])

    with Poller(source, assistant, clock=lambda: next(ticks)) as poller:
        first = poller.tick(force=True)
        assert first.new_plays == 1

        _put(source / "scoredatalog.db", "scoredatalog", _row(7, date=101))
        gap = poller.tick(force=True)
        assert gap.new_plays == 1
        assert gap.lost_events == 1

        unchanged = poller.tick(force=True)
        assert unchanged.new_plays == 0
        assert unchanged.lost_events == 0

        _put(source / "scoredatalog.db", "scoredatalog", _row(2, date=102))
        rewound = poller.tick(force=True)
        assert rewound.generation_changed
        assert rewound.new_plays == 1

    conn = sqlite3.connect(assistant)
    try:
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 3
        assert conn.execute(
            "SELECT lost_events FROM plays WHERE playcount = 7"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT max(source_generation) FROM plays"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_payload_change_without_playcount_updates_same_event(tmp_path) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"
    with Poller(source, assistant, clock=lambda: 2_000) as poller:
        poller.tick(force=True)
        _put(source / "scoredatalog.db", "scoredatalog", _row(5, minbp=9))
        changed = poller.tick(force=True)
        assert changed.new_plays == 0
        assert poller.tick().scanned is False

    conn = sqlite3.connect(assistant)
    try:
        assert conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 1
        assert conn.execute("SELECT minbp FROM plays").fetchone()[0] == 9
    finally:
        conn.close()


def test_score_change_during_read_retries_snapshot(tmp_path, monkeypatch) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"
    score_path = source / "score.db"
    original_read_score = readers.read_score
    read_count = 0

    def read_score_and_mutate_once(conn):
        nonlocal read_count
        rows = original_read_score(conn)
        read_count += 1
        if read_count == 1:
            _put(score_path, "score", _row(minbp=9))
        return rows

    monkeypatch.setattr(readers, "read_score", read_score_and_mutate_once)
    with Poller(source, assistant, max_retries=2, clock=lambda: 3_000) as poller:
        result = poller.tick(force=True)

    assert result.new_plays == 1
    assert read_count == 2


def test_replay_exact_match_history_overwrite_and_invalid_counter(tmp_path) -> None:
    source = _source_dir(tmp_path)
    slot = source / "replay" / "slot-0.brd"
    _replay(slot)
    assistant = tmp_path / "assistant.db"

    with Poller(source, assistant, clock=lambda: 4_000) as poller:
        first = poller.tick(force=True)
        assert first.replay_scanned == 1
        assert first.replay_matched == 1
        row = poller.conn.execute(
            "SELECT selected_gauge_kind, seed, random FROM plays"
        ).fetchone()
        assert tuple(row) == ("HARD", 2, 3)

        _replay(slot, gauge=4)
        broken = source / "replay" / "slot-1.brd"
        broken.write_bytes(b"not-gzip")
        second = poller.tick(force=True)
        assert second.replay_matched == 1
        assert second.replay_overwritten == 1
        assert second.replay_invalid == 1
        assert poller.conn.execute(
            "SELECT selected_gauge_kind FROM plays"
        ).fetchone()[0] == "EXHARD"
        history = poller.conn.execute(
            "SELECT content_hash, previous_content_hash, match_status "
            "FROM replay_metadata ORDER BY id"
        ).fetchall()
        assert len(history) == 2
        assert history[0][1] is None
        assert history[1][1] == history[0][0]
        assert {row[2] for row in history} == {"matched"}
        columns = {row[1] for row in poller.conn.execute("PRAGMA table_info(replay_metadata)")}
        assert "keyinput" not in columns


def test_replay_ambiguous_and_unmatched_never_change_plays(tmp_path) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"
    with Poller(source, assistant, clock=lambda: 5_000) as poller:
        poller.tick(force=True)
        original = dict(
            poller.conn.execute(
                f"SELECT {', '.join(store.PLAY_COLUMNS)} FROM plays"
            ).fetchone()
        )
        duplicate = dict(original)
        duplicate["source_generation"] = 9
        duplicate["playcount"] = 99
        store.insert_play(poller.conn, duplicate)
        poller.conn.commit()

        _replay(source / "replay" / "ambiguous.brd")
        _replay(source / "replay" / "unmatched.brd", sha256="b" * 64)
        result = poller.tick(force=True)

        assert result.replay_ambiguous == 1
        assert result.replay_unmatched == 1
        assert poller.conn.execute(
            "SELECT count(*) FROM plays WHERE selected_gauge_kind IS NOT NULL"
        ).fetchone()[0] == 0


def test_replay_saved_before_score_is_reconciled_after_new_play(tmp_path) -> None:
    source = _source_dir(tmp_path)
    for name, table in (("score.db", "score"), ("scoredatalog.db", "scoredatalog")):
        conn = sqlite3.connect(source / name)
        conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
    _replay(source / "replay" / "early.brd")

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 6_000) as poller:
        early = poller.tick(force=True)
        assert early.replay_unmatched == 1

        _put(source / "scoredatalog.db", "scoredatalog", _row())
        _put(source / "score.db", "score", _row())
        later = poller.tick()

        assert later.new_plays == 1
        assert later.replay_matched == 1
        assert poller.conn.execute(
            "SELECT selected_gauge_kind FROM plays"
        ).fetchone()[0] == "HARD"
        assert tuple(
            poller.conn.execute(
                "SELECT count(*), max(match_status) FROM replay_metadata"
            ).fetchone()
        ) == (1, "matched")
