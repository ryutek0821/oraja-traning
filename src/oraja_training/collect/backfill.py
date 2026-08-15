"""Initial import from a static beatoraja player database directory."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from collections.abc import Callable

from oraja_training.collect.normalize import derive_play, payload_hash
from oraja_training.collect.source import source_signature
from oraja_training.db import readers, store


LEGACY_GENERATION = 0
# IR aggregates are not events from the live scoredatalog generation.  A
# separate namespace also prevents their event keys from suppressing legacy
# rows because the schema's UNIQUE key intentionally does not include source.
IR_IMPORT_GENERATION = -1
SOURCE_DATABASES = {
    "score.db",
    "scoredatalog.db",
    "scorelog.db",
    "songdata.db",
    "songinfo.db",
}


class SourceChangedDuringBackfill(RuntimeError):
    """Raised when an allegedly static source changes during import."""


@dataclass(frozen=True, slots=True)
class BackfillResult:
    charts: int
    pattern_features: int
    legacy_plays: int
    ir_imports: int
    song_rows_seen: int
    skipped_ir_noplay: int

    @property
    def plays(self) -> int:
        return self.legacy_plays + self.ir_imports


def _validate_destination(db_dir: Path, assistant_db: str | Path) -> Path:
    destination = Path(assistant_db).expanduser().resolve()
    sources = {(db_dir / name).resolve() for name in SOURCE_DATABASES}
    if destination in sources:
        raise ValueError("assistant_db must not be a beatoraja source database")
    return destination


def run(
    db_dir: str | Path,
    assistant_db: str | Path = Path("assistant.db"),
    *,
    clock: Callable[[], float] = time.time,
) -> BackfillResult:
    """Backfill charts, the latest local snapshot, and usable IR aggregates.

    Source databases are always opened with ``immutable=1``.  The caller must
    only use this entry point for a static snapshot, never a running player
    directory.
    """

    source_dir = Path(db_dir).expanduser().resolve(strict=True)
    destination = _validate_destination(source_dir, assistant_db)
    now = int(clock())
    read_paths = (
        source_dir / "songdata.db",
        source_dir / "scoredatalog.db",
        source_dir / "score.db",
    )
    signatures_before = {path: source_signature(path) for path in read_paths}

    with closing(readers.open_snapshot(source_dir / "songdata.db")) as song_conn:
        songs = readers.read_songs(song_conn)
        pattern_rows = readers.read_chart_patterns(song_conn)
    with closing(
        readers.open_snapshot(source_dir / "scoredatalog.db")
    ) as scoredatalog_conn:
        latest_rows = readers.read_scoredatalog(scoredatalog_conn)
    with closing(readers.open_snapshot(source_dir / "score.db")) as score_conn:
        score_rows = readers.read_score(score_conn)

    signatures_after = {path: source_signature(path) for path in read_paths}
    if signatures_after != signatures_before:
        raise SourceChangedDuringBackfill(
            "source DB changed during immutable backfill; retry from a static snapshot"
        )

    aggregate = {
        (str(row["sha256"]), int(row["mode"])): row for row in score_rows
    }
    signature = signatures_after[source_dir / "scoredatalog.db"]
    conn = store.init(destination)
    try:
        with conn:
            store.upsert_charts(
                conn,
                (
                    {
                        "sha256": str(song["sha256"]),
                        "md5": song.get("md5"),
                        "title": song.get("title"),
                        "artist": song.get("artist"),
                        "notes": song.get("notes"),
                        "song_mode": song.get("mode"),
                        "path": song.get("path"),
                        "updated_at": now,
                    }
                    for song in songs
                ),
            )
            store.upsert_chart_pattern_features(
                conn,
                (
                    {
                        "sha256": str(row["sha256"]),
                        "rhythm_family": row.get("rhythm_family"),
                        "avg_chord": row.get("avg_chord"),
                        "chord_ge3": row.get("chord_ge3"),
                        "micro_rate": row.get("micro_rate"),
                        "long_jack_rate": row.get("long_jack_rate"),
                        "practice_low": row.get("practice_low"),
                        "analysis_version": int(row.get("analysis_version") or 0),
                        "grid_bpm": row.get("grid_bpm"),
                        "stream_sec": row.get("stream_sec"),
                        "last_kill": row.get("last_kill"),
                    }
                    for row in pattern_rows
                    if not row.get("error")
                ),
            )

            for row in latest_rows:
                key = (str(row["sha256"]), int(row["mode"]))
                play = derive_play(
                    row,
                    source="legacy_last_snapshot",
                    source_generation=LEGACY_GENERATION,
                    aggregate_row=aggregate.get(key),
                    ingested_at=now,
                )
                store.insert_play(conn, play)
                store.upsert_cursor(
                    conn,
                    source_generation=LEGACY_GENERATION,
                    sha256=key[0],
                    mode=key[1],
                    playcount=int(row["playcount"]),
                    payload_hash=payload_hash(row),
                )

            for row in score_rows:
                if int(row["date"]) != 0 or int(row["clear"]) == 0:
                    continue
                play = derive_play(
                    row,
                    source="ir_import",
                    source_generation=IR_IMPORT_GENERATION,
                    aggregate_row=row,
                    ingested_at=now,
                )
                store.insert_play(conn, play)

            conn.execute(
                """
                INSERT INTO collector_state(
                  key, source_generation, last_mtime, last_size,
                  last_run_at, last_error
                ) VALUES ('scoredatalog', ?, ?, ?, ?, NULL)
                ON CONFLICT(key) DO NOTHING
                """,
                (
                    LEGACY_GENERATION,
                    signature.last_mtime,
                    signature.total_size,
                    now,
                ),
            )

        chart_count = int(conn.execute("SELECT count(*) FROM charts").fetchone()[0])
        legacy_count = int(
            conn.execute(
                "SELECT count(*) FROM plays WHERE source = 'legacy_last_snapshot'"
            ).fetchone()[0]
        )
        ir_count = int(
            conn.execute(
                "SELECT count(*) FROM plays WHERE source = 'ir_import'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    skipped_ir_noplay = sum(
        int(row["date"]) == 0 and int(row["clear"]) == 0 for row in score_rows
    )
    valid_pattern_rows = sum(not row.get("error") for row in pattern_rows)
    return BackfillResult(
        charts=chart_count,
        pattern_features=valid_pattern_rows,
        legacy_plays=legacy_count,
        ir_imports=ir_count,
        song_rows_seen=len(songs),
        skipped_ir_noplay=skipped_ir_noplay,
    )
