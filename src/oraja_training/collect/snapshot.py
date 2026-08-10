"""Idempotent ingestion of a static daily score/scoredatalog pair."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Callable
from zoneinfo import ZoneInfo

from oraja_training.collect.normalize import (
    EMPTY_POOR_COLUMNS,
    JUDGEMENT_COLUMNS,
    derive_play,
    ex_score,
    payload_hash,
)
from oraja_training.collect.source import source_signature
from oraja_training.db import readers, store


DAILY_GENERATION = -2
JST = ZoneInfo("Asia/Tokyo")


class SnapshotPairMismatch(RuntimeError):
    """Raised when the two submitted databases cannot be one coherent copy."""


@dataclass(frozen=True, slots=True)
class DailyImportResult:
    import_id: int
    effective_date: str
    baseline_judged: int
    cumulative_playcount: int
    new_play_rows: int
    lost_events: int
    lamp_updates: int
    idempotent: bool = False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cumulative(row: readers.PlayerRow) -> tuple[int, int, int, int]:
    judged = sum(int(row.get(column) or 0) for column in JUDGEMENT_COLUMNS)
    empty = sum(int(row.get(column) or 0) for column in EMPTY_POOR_COLUMNS)
    return (
        int(row["playcount"]),
        judged,
        empty,
        int(row["playtime"]),
    )


def _validate_static(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise SnapshotPairMismatch(
                f"{path.name} has a live SQLite sidecar; exit beatoraja and copy again"
            )


def _existing_result(
    conn: sqlite3.Connection, score_hash: str, log_hash: str
) -> DailyImportResult | None:
    row = conn.execute(
        """
        SELECT id, effective_date, baseline_judged, cumulative_playcount,
               new_play_rows, lost_events
        FROM daily_imports
        WHERE score_sha256 = ? AND scoredatalog_sha256 = ?
        """,
        (score_hash, log_hash),
    ).fetchone()
    if row is None:
        return None
    lamp_updates = conn.execute(
        "SELECT count(*) FROM score_changes WHERE import_id = ? AND new_clear > old_clear",
        (row["id"],),
    ).fetchone()[0]
    return DailyImportResult(
        int(row["id"]),
        str(row["effective_date"]),
        int(row["baseline_judged"]),
        int(row["cumulative_playcount"]),
        int(row["new_play_rows"]),
        int(row["lost_events"]),
        int(lamp_updates),
        True,
    )


def run(
    score_db: str | Path,
    scoredatalog_db: str | Path,
    assistant_db: str | Path = Path("assistant.db"),
    *,
    clock: Callable[[], float] = time.time,
) -> DailyImportResult:
    """Import one post-session pair without ever writing to either source."""

    score_path = Path(score_db).expanduser().resolve(strict=True)
    log_path = Path(scoredatalog_db).expanduser().resolve(strict=True)
    destination = Path(assistant_db).expanduser().resolve()
    if destination in {score_path, log_path}:
        raise ValueError("assistant_db must not be either submitted source DB")
    _validate_static(score_path)
    _validate_static(log_path)

    signatures_before = {
        score_path: source_signature(score_path),
        log_path: source_signature(log_path),
    }
    score_hash = _file_sha256(score_path)
    log_hash = _file_sha256(log_path)

    conn = store.init(destination)
    try:
        existing = _existing_result(conn, score_hash, log_hash)
        if existing is not None:
            return existing

        with closing(readers.open_snapshot(score_path)) as source:
            score_rows = readers.read_score(source)
            player_rows = readers.read_player(source)
        with closing(readers.open_snapshot(log_path)) as source:
            log_rows = readers.read_scoredatalog(source)
        signatures_after = {
            score_path: source_signature(score_path),
            log_path: source_signature(log_path),
        }
        if signatures_after != signatures_before:
            raise SnapshotPairMismatch("submitted DB changed during immutable read")
        if not player_rows:
            raise SnapshotPairMismatch("score.db.player contains no cumulative rows")

        aggregate = {
            (str(row["sha256"]), int(row["mode"])): row for row in score_rows
        }
        incoherent = [
            (str(row["sha256"]), int(row["mode"]))
            for row in log_rows
            if (current := aggregate.get((str(row["sha256"]), int(row["mode"]))))
            is not None
            and int(row["playcount"]) > int(current["playcount"])
        ]
        if incoherent:
            raise SnapshotPairMismatch(
                "scoredatalog.db is newer than score.db; copy both again after exit"
            )

        latest = player_rows[-1]
        total_playcount, total_judged, _, _ = _cumulative(latest)
        effective_date = datetime.fromtimestamp(int(latest["date"]), JST).date().isoformat()
        now = int(clock())
        baseline = conn.execute("SELECT count(*) FROM daily_imports").fetchone()[0] == 0
        cursors = store.load_cursors(conn, DAILY_GENERATION)
        previous_score = {
            (str(row["sha256"]), int(row["mode"])): row
            for row in conn.execute("SELECT * FROM score_state")
        }
        new_play_rows = 0
        lost_total = 0
        lamp_updates = 0

        with conn:
            cursor = conn.execute(
                """
                INSERT INTO daily_imports(
                  imported_at, effective_date, score_sha256, scoredatalog_sha256,
                  baseline_judged, cumulative_playcount
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    effective_date,
                    score_hash,
                    log_hash,
                    total_judged,
                    total_playcount,
                ),
            )
            import_id = int(cursor.lastrowid)

            previous_values: tuple[int, int, int, int] | None = None
            for row in player_rows:
                current_values = _cumulative(row)
                deltas = (
                    tuple(current_values[i] - previous_values[i] for i in range(4))
                    if previous_values is not None
                    else (None, None, None, None)
                )
                if any(value is not None and value < 0 for value in deltas):
                    raise SnapshotPairMismatch("player cumulative counters moved backwards")
                conn.execute(
                    """
                    INSERT INTO player_daily(
                      date, cumulative_playcount, cumulative_judged,
                      cumulative_empty_poor, cumulative_playtime,
                      delta_playcount, delta_judged, delta_empty_poor,
                      delta_playtime, import_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                      cumulative_playcount=excluded.cumulative_playcount,
                      cumulative_judged=excluded.cumulative_judged,
                      cumulative_empty_poor=excluded.cumulative_empty_poor,
                      cumulative_playtime=excluded.cumulative_playtime,
                      delta_playcount=excluded.delta_playcount,
                      delta_judged=excluded.delta_judged,
                      delta_empty_poor=excluded.delta_empty_poor,
                      delta_playtime=excluded.delta_playtime,
                      import_id=excluded.import_id
                    """,
                    (int(row["date"]), *current_values, *deltas, import_id),
                )
                previous_values = current_values

            for row in score_rows:
                key = (str(row["sha256"]), int(row["mode"]))
                old = previous_score.get(key)
                current_ex = ex_score(row)
                combo = None if row.get("combo") is None else int(row["combo"])
                minbp = None if row.get("minbp") is None else int(row["minbp"])
                if not baseline and old is not None:
                    play_delta = int(row["playcount"]) - int(old["playcount"])
                    if play_delta < 0:
                        raise SnapshotPairMismatch("score playcount moved backwards")
                    changed = play_delta > 0 or any(
                        (
                            ("clear", int(row["clear"])),
                            ("ex", current_ex),
                            ("minbp", minbp),
                            ("combo", combo),
                        )[index][1]
                        != old[("clear", "ex", "minbp", "combo")[index]]
                        for index in range(4)
                    )
                    if changed:
                        conn.execute(
                            """
                            INSERT INTO score_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                import_id,
                                key[0],
                                key[1],
                                play_delta,
                                old["clear"],
                                int(row["clear"]),
                                old["ex"],
                                current_ex,
                                old["minbp"],
                                minbp,
                                old["combo"],
                                combo,
                            ),
                        )
                        lamp_updates += int(int(row["clear"]) > int(old["clear"]))
                conn.execute(
                    """
                    INSERT INTO score_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha256, mode) DO UPDATE SET
                      clear=excluded.clear, ex=excluded.ex, minbp=excluded.minbp,
                      combo=excluded.combo, notes=excluded.notes,
                      playcount=excluded.playcount, played_at=excluded.played_at,
                      import_id=excluded.import_id, payload_hash=excluded.payload_hash
                    """,
                    (
                        key[0],
                        key[1],
                        int(row["clear"]),
                        current_ex,
                        minbp,
                        combo,
                        None if row.get("notes") is None else int(row["notes"]),
                        int(row["playcount"]),
                        int(row["date"]),
                        import_id,
                        payload_hash(row),
                    ),
                )

            for row in log_rows:
                key = (str(row["sha256"]), int(row["mode"]))
                current_count = int(row["playcount"])
                current_hash = payload_hash(row)
                old_cursor = cursors.get(key)
                if old_cursor is not None and current_count < old_cursor[0]:
                    raise SnapshotPairMismatch("scoredatalog playcount moved backwards")
                should_insert = not baseline and (
                    old_cursor is None or current_count > old_cursor[0]
                )
                if should_insert:
                    lost = max(0, current_count - (old_cursor[0] if old_cursor else 0) - 1)
                    play = derive_play(
                        row,
                        source="daily_snapshot",
                        source_generation=DAILY_GENERATION,
                        aggregate_row=aggregate.get(key),
                        lost_events=lost,
                        ingested_at=now,
                    )
                    if store.insert_play(conn, play):
                        new_play_rows += 1
                        lost_total += lost
                if old_cursor is None or current_count >= old_cursor[0]:
                    store.upsert_cursor(
                        conn,
                        source_generation=DAILY_GENERATION,
                        sha256=key[0],
                        mode=key[1],
                        playcount=current_count,
                        payload_hash=current_hash,
                    )

            conn.execute(
                """
                UPDATE daily_imports
                SET new_play_rows = ?, lost_events = ? WHERE id = ?
                """,
                (new_play_rows, lost_total, import_id),
            )

        return DailyImportResult(
            import_id,
            effective_date,
            total_judged,
            total_playcount,
            new_play_rows,
            lost_total,
            lamp_updates,
        )
    finally:
        conn.close()
