from __future__ import annotations

import hashlib
from pathlib import Path
import gzip
import json
import sqlite3
from types import SimpleNamespace

import pytest

from oraja_training.collect import replay
from oraja_training.collect.poller import Poller
from oraja_training.db import readers
from oraja_training.db import store
from oraja_training.filesystem import sqlite_file_identity


WINDOWS_DEVICE_ID = 16_012_189_180_544_750_605


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


def _source_artifact_state(source: Path) -> dict[str, tuple[str, int]]:
    suffixes = (".db", ".db-wal", ".db-shm", ".db-journal")
    return {
        path.name: (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(source.iterdir())
        if path.name.endswith(suffixes)
    }


def _replay(
    path: Path,
    *,
    sha256: str = "a" * 64,
    mode: int = 0,
    date: int = 100,
    gauge: int = 3,
) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(
        gzip.compress(
            json.dumps(
                {
                    "sha256": sha256,
                    "mode": mode,
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


def test_restart_reuses_cursor_without_touching_source_databases(tmp_path) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"

    with Poller(source, assistant, clock=lambda: 2_500) as first:
        assert first.tick(force=True).new_plays == 1

    before = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in source.glob("*.db")
    }
    with Poller(source, assistant, clock=lambda: 2_501) as restarted:
        result = restarted.tick(force=True)
    after = {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in source.glob("*.db")
    }

    assert result.new_plays == 0
    assert result.lost_events == 0
    assert result.generation_changed is False
    assert after == before


def test_first_tick_does_not_touch_source_database_or_live_sidecars(tmp_path) -> None:
    source = _source_dir(tmp_path)
    live_connections: list[sqlite3.Connection] = []
    try:
        for name, table in (
            ("score.db", "score"),
            ("scoredatalog.db", "scoredatalog"),
        ):
            conn = sqlite3.connect(source / name)
            assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.execute(f"UPDATE {table} SET date = date + 1")
            conn.commit()
            live_connections.append(conn)

        # A stale rollback journal may coexist with a WAL database after an
        # interrupted process.  It must be treated as source data, not opened
        # or cleaned up in place by the collector.
        (source / "scoredatalog.db-journal").write_bytes(b"stale-journal-marker")
        before = _source_artifact_state(source)
        assert {
            "score.db-wal",
            "score.db-shm",
            "scoredatalog.db-wal",
            "scoredatalog.db-shm",
            "scoredatalog.db-journal",
        } <= set(before)

        with Poller(source, tmp_path / "assistant.db", clock=lambda: 2_750) as poller:
            result = poller.tick(force=True)

        assert result.new_plays == 1
        assert _source_artifact_state(source) == before
    finally:
        for conn in live_connections:
            conn.close()


def test_append_style_scoredatalog_schema_fails_closed(tmp_path) -> None:
    source = _source_dir(tmp_path)
    conn = sqlite3.connect(source / "scoredatalog.db")
    try:
        conn.execute("ALTER TABLE scoredatalog RENAME TO legacy_scoredatalog")
        conn.execute(
            "CREATE TABLE scoredatalog AS "
            "SELECT * FROM legacy_scoredatalog WHERE 0"
        )
        conn.execute("INSERT INTO scoredatalog SELECT * FROM legacy_scoredatalog")
        conn.execute("INSERT INTO scoredatalog SELECT * FROM legacy_scoredatalog")
        conn.commit()
    finally:
        conn.close()

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 2_875) as poller:
        with pytest.raises(
            readers.ReaderSchemaError,
            match="unsupported scoredatalog schema.*append-style",
        ):
            poller.tick(force=True)
        state = poller.conn.execute(
            "SELECT last_error FROM collector_state WHERE key = 'scoredatalog'"
        ).fetchone()
        assert poller.conn.execute("SELECT count(*) FROM plays").fetchone()[0] == 0

    assert state is not None
    assert "expected overwrite-only PRIMARY KEY (sha256, mode)" in state[0]


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


def test_replay_windows_unsigned_file_id_is_persisted_and_stable(
    tmp_path, monkeypatch
) -> None:
    source = _source_dir(tmp_path)
    slot = source / "replay" / "slot.brd"
    _replay(slot)
    original_stat = Path.stat
    windows_inode = [(1 << 127) + 17]

    def windows_stat(candidate: Path, *args, **kwargs):
        value = original_stat(candidate, *args, **kwargs)
        if candidate != slot:
            return value
        return SimpleNamespace(
            st_dev=WINDOWS_DEVICE_ID,
            st_ino=windows_inode[0],
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns,
        )

    monkeypatch.setattr(Path, "stat", windows_stat)

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_050) as poller:
        first = poller.tick(force=True)
        persisted = poller.conn.execute(
            "SELECT device, inode FROM replay_scan_state WHERE path = ?", (str(slot),)
        ).fetchone()

    assert first.replay_scanned == 1
    assert persisted is not None
    assert tuple(persisted) == sqlite_file_identity(
        WINDOWS_DEVICE_ID,
        windows_inode[0],
    )

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_051) as restarted:
        assert restarted.tick().replay_scanned == 0
        windows_inode[0] += 1
        changed = restarted.tick()
        updated = restarted.conn.execute(
            "SELECT inode FROM replay_scan_state WHERE path = ?", (str(slot),)
        ).fetchone()

    assert changed.replay_scanned == 1
    assert updated is not None
    assert updated[0] == sqlite_file_identity(
        WINDOWS_DEVICE_ID, windows_inode[0]
    )[1]


def test_invalid_replay_windows_wide_identity_is_persisted_once(
    tmp_path, monkeypatch
) -> None:
    source = _source_dir(tmp_path)
    slot = source / "replay" / "broken.brd"
    slot.parent.mkdir()
    slot.write_bytes(b"not-gzip")
    windows_inode = (1 << 127) + 19
    original_stat = Path.stat

    def windows_stat(candidate: Path, *args, **kwargs):
        value = original_stat(candidate, *args, **kwargs)
        if candidate != slot:
            return value
        return SimpleNamespace(
            st_dev=WINDOWS_DEVICE_ID,
            st_ino=windows_inode,
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns,
        )

    monkeypatch.setattr(Path, "stat", windows_stat)

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_075) as poller:
        first = poller.tick(force=True)
        persisted = poller.conn.execute(
            "SELECT device, inode, outcome FROM replay_scan_state WHERE path = ?",
            (str(slot),),
        ).fetchone()

    expected = sqlite_file_identity(WINDOWS_DEVICE_ID, windows_inode)
    assert first.replay_invalid == 1
    assert persisted is not None
    assert tuple(persisted) == (*expected, "invalid")

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_076) as restarted:
        second = restarted.tick()

    assert second.replay_scanned == 0
    assert second.replay_invalid == 0


def test_replay_batches_eventually_ingest_over_128_files_across_restart(
    tmp_path,
) -> None:
    source = _source_dir(tmp_path)
    replay_count = replay.MAX_REPLAY_FILES + 2
    for index in range(replay_count):
        _replay(
            source / "replay" / f"slot-{index:03d}.brd",
            sha256=f"{index:064x}",
        )
    assistant = tmp_path / "assistant.db"

    with Poller(source, assistant, clock=lambda: 4_100) as poller:
        first = poller.tick(force=True)
        assert first.replay_scanned == replay.MAX_REPLAY_FILES
        assert first.replay_invalid == 0
        assert poller.conn.execute(
            "SELECT count(*) FROM replay_metadata"
        ).fetchone()[0] == replay.MAX_REPLAY_FILES

    with Poller(source, assistant, clock=lambda: 4_101) as restarted:
        second = restarted.tick()
        assert second.replay_scanned == 2
        assert second.replay_invalid == 0
        assert restarted.conn.execute(
            "SELECT count(*) FROM replay_metadata"
        ).fetchone()[0] == replay_count
        assert restarted.tick().replay_scanned == 0


def test_replay_batch_progresses_past_persisted_invalid_files(
    tmp_path, monkeypatch
) -> None:
    source = _source_dir(tmp_path)
    replay_dir = source / "replay"
    replay_dir.mkdir()
    (replay_dir / "slot-0.brd").write_bytes(b"not-gzip")
    (replay_dir / "slot-1.brd").write_bytes(b"not-gzip")
    _replay(replay_dir / "slot-2.brd", sha256="b" * 64)
    assistant = tmp_path / "assistant.db"
    monkeypatch.setattr(replay, "MAX_REPLAY_FILES", 2)

    with Poller(source, assistant, clock=lambda: 4_200) as poller:
        first = poller.tick(force=True)
        assert first.replay_invalid == 2
        assert first.replay_scanned == 2

    with Poller(source, assistant, clock=lambda: 4_201) as restarted:
        second = restarted.tick()
        assert second.replay_invalid == 0
        assert second.replay_scanned == 1
        assert restarted.conn.execute(
            "SELECT count(*) FROM replay_metadata"
        ).fetchone()[0] == 1


def test_replay_matches_only_the_bounded_result_timestamp_skew(tmp_path) -> None:
    source = _source_dir(tmp_path)
    _replay(source / "replay" / "within.brd", date=70)
    _replay(source / "replay" / "outside.brd", date=69)

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_500) as poller:
        result = poller.tick(force=True)
        statuses = dict(
            poller.conn.execute(
                "SELECT path, match_status FROM replay_metadata ORDER BY path"
            ).fetchall()
        )

    assert result.replay_matched == 1
    assert result.replay_unmatched == 1
    assert statuses[str(source / "replay" / "within.brd")] == "matched"
    assert statuses[str(source / "replay" / "outside.brd")] == "unmatched"


def test_replay_ln_mode_falls_back_to_score_mode_zero_only_without_exact_match(
    tmp_path,
) -> None:
    source = _source_dir(tmp_path)
    _replay(source / "replay" / "ln-mode.brd", mode=1)

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_750) as poller:
        result = poller.tick(force=True)
        matched = poller.conn.execute(
            """
            SELECT metadata.mode, play.mode, play.selected_gauge_kind
            FROM replay_metadata metadata
            JOIN plays play ON play.id = metadata.matched_play_id
            """
        ).fetchone()

    assert result.replay_matched == 1
    assert tuple(matched) == (1, 0, "HARD")


def test_replay_ln_mode_prefers_an_exact_mode_candidate(tmp_path) -> None:
    source = _source_dir(tmp_path)
    with Poller(source, tmp_path / "assistant.db", clock=lambda: 4_875) as poller:
        poller.tick(force=True)
        original = dict(
            poller.conn.execute(
                f"SELECT {', '.join(store.PLAY_COLUMNS)} FROM plays"
            ).fetchone()
        )
        exact = dict(original)
        exact["mode"] = 1
        exact["source_generation"] = 9
        exact["selected_gauge_kind"] = None
        store.insert_play(poller.conn, exact)
        poller.conn.commit()
        _replay(source / "replay" / "ln-mode.brd", mode=1)

        result = poller.tick(force=True)
        matched_mode = poller.conn.execute(
            """
            SELECT play.mode FROM replay_metadata metadata
            JOIN plays play ON play.id = metadata.matched_play_id
            """
        ).fetchone()[0]

    assert result.replay_matched == 1
    assert matched_mode == 1


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
        duplicate["selected_gauge_kind"] = None
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


def test_replay_match_becomes_ambiguous_if_a_second_play_arrives(tmp_path) -> None:
    source = _source_dir(tmp_path)
    assistant = tmp_path / "assistant.db"
    _replay(source / "replay" / "slot.brd")
    with Poller(source, assistant, clock=lambda: 5_250) as poller:
        assert poller.tick(force=True).replay_matched == 1
        original = dict(
            poller.conn.execute(
                f"SELECT {', '.join(store.PLAY_COLUMNS)} FROM plays"
            ).fetchone()
        )
        duplicate = dict(original)
        duplicate["source_generation"] = 9
        duplicate["playcount"] = 99
        duplicate["selected_gauge_kind"] = None
        store.insert_play(poller.conn, duplicate)
        poller.conn.commit()

        result = poller.tick(force=True)

        assert result.replay_ambiguous == 1
        assert poller.conn.execute(
            "SELECT match_status FROM replay_metadata"
        ).fetchone()[0] == "ambiguous"
        assert poller.conn.execute(
            "SELECT count(*) FROM plays WHERE selected_gauge_kind IS NOT NULL"
        ).fetchone()[0] == 0


def test_replay_changed_during_read_is_counted_and_audited(
    tmp_path, monkeypatch
) -> None:
    source = _source_dir(tmp_path)
    _replay(source / "replay" / "unstable.brd")
    monkeypatch.setattr(
        replay,
        "scan_report",
        lambda _, **__: replay.ReplayScanResult((), unstable=1),
    )

    with Poller(source, tmp_path / "assistant.db", clock=lambda: 5_500) as poller:
        result = poller.tick(force=True)
        state = poller.conn.execute(
            "SELECT last_error FROM collector_state WHERE key = 'replay'"
        ).fetchone()

    assert result.replay_unstable == 1
    assert result.replay_scanned == 1
    assert state[0] == "invalid=0, unstable=1"


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
