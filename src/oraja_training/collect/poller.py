"""Content-diff collector for the overwrite-only ``scoredatalog`` table."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import logging
import shutil
import sqlite3
import tempfile
import time

from oraja_training.collect.normalize import derive_play, payload_hash
from oraja_training.collect import replay
from oraja_training.collect.source import SourceSignature, source_signature
from oraja_training.db import readers, store


LOGGER = logging.getLogger(__name__)


class SourceChangedDuringRead(RuntimeError):
    """Raised when the two source databases could not be read consistently."""


@dataclass(frozen=True, slots=True)
class TickResult:
    new_plays: int
    lost_events: int
    generation_changed: bool
    scanned: bool = True
    replay_scanned: int = 0
    replay_matched: int = 0
    replay_unmatched: int = 0
    replay_ambiguous: int = 0
    replay_invalid: int = 0
    replay_unstable: int = 0
    replay_overwritten: int = 0


def _same_persisted_signal(
    signature: SourceSignature, last_mtime: float | None, last_size: int | None
) -> bool:
    if last_mtime is None or last_size is None:
        return False
    current_mtime = signature.last_mtime
    if current_mtime is None:
        return False
    return abs(current_mtime - float(last_mtime)) < 0.000_000_5 and (
        signature.total_size == int(last_size)
    )


class Poller:
    """Poll and persist the latest row for every changed chart.

    File mtimes merely decide whether to scan.  Once scanning begins, every
    ``scoredatalog`` row is compared by ``playcount`` and canonical payload
    hash with the assistant-owned cursor.
    """

    def __init__(
        self,
        db_dir: str | Path,
        assistant_db: str | Path | sqlite3.Connection = Path("assistant.db"),
        *,
        poll_interval: float = 5.0,
        busy_timeout_ms: int = 1_000,
        max_retries: int = 3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_dir = Path(db_dir).expanduser().resolve()
        self.scoredatalog_path = self.db_dir / "scoredatalog.db"
        self.score_path = self.db_dir / "score.db"
        self.replay_dir = self.db_dir / "replay"
        self.poll_interval = float(poll_interval)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.max_retries = max(1, int(max_retries))
        self.clock = clock
        self._last_signature: SourceSignature | None = None
        self._owns_connection = not isinstance(assistant_db, sqlite3.Connection)

        if isinstance(assistant_db, sqlite3.Connection):
            self.conn = assistant_db
            self.conn.row_factory = sqlite3.Row
            self._assert_connection_is_not_source()
            store.migrate(self.conn)
        else:
            assistant_path = Path(assistant_db).expanduser().resolve()
            source_paths = {
                self.scoredatalog_path.resolve(),
                self.score_path.resolve(),
                (self.db_dir / "scorelog.db").resolve(),
                (self.db_dir / "songdata.db").resolve(),
                (self.db_dir / "songinfo.db").resolve(),
            }
            if assistant_path in source_paths:
                raise ValueError("assistant_db must not be a beatoraja source database")
            self.conn = store.init(assistant_path)

    def _assert_connection_is_not_source(self) -> None:
        row = self.conn.execute("PRAGMA database_list").fetchone()
        if row is None or not row[2]:
            return
        connected = Path(str(row[2])).resolve()
        source_paths = {
            self.db_dir / name
            for name in (
                "score.db",
                "scoredatalog.db",
                "scorelog.db",
                "songdata.db",
                "songinfo.db",
            )
        }
        if connected in {path.resolve() for path in source_paths}:
            raise ValueError("assistant connection points at a beatoraja source database")

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def __enter__(self) -> "Poller":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _state(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM collector_state WHERE key = 'scoredatalog'"
        ).fetchone()

    def _read_consistent_snapshot(
        self,
    ) -> tuple[list[readers.ScoreRow], dict[tuple[str, int], readers.ScoreRow], SourceSignature]:
        for _attempt in range(self.max_retries):
            scoredatalog_before = source_signature(self.scoredatalog_path)
            score_before = source_signature(self.score_path)

            # Even a mode=ro SQLite connection writes reader marks into a live
            # WAL's shared-memory file.  Read short-lived private copies so the
            # beatoraja-owned DB, WAL, SHM and rollback journal remain byte-for-
            # byte untouched.  SHM is intentionally not copied: SQLite safely
            # rebuilds this non-durable WAL index beside the private copy.
            with tempfile.TemporaryDirectory(prefix="oraja-collector-") as temporary:
                temporary_dir = Path(temporary)
                scoredatalog_copy = self._copy_live_database(
                    self.scoredatalog_path, temporary_dir
                )
                score_copy = self._copy_live_database(self.score_path, temporary_dir)

                scoredatalog_conn = readers.open_private_copy(
                    scoredatalog_copy, busy_timeout_ms=self.busy_timeout_ms
                )
                try:
                    score_conn = readers.open_private_copy(
                        score_copy, busy_timeout_ms=self.busy_timeout_ms
                    )
                    try:
                        scoredatalog_version_before = int(
                            scoredatalog_conn.execute(
                                "PRAGMA data_version"
                            ).fetchone()[0]
                        )
                        score_version_before = int(
                            score_conn.execute("PRAGMA data_version").fetchone()[0]
                        )
                        rows = readers.read_scoredatalog(scoredatalog_conn)
                        aggregate_rows = readers.read_score(score_conn)
                        scoredatalog_version_after = int(
                            scoredatalog_conn.execute(
                                "PRAGMA data_version"
                            ).fetchone()[0]
                        )
                        score_version_after = int(
                            score_conn.execute("PRAGMA data_version").fetchone()[0]
                        )
                    finally:
                        score_conn.close()
                finally:
                    scoredatalog_conn.close()

            scoredatalog_after = source_signature(self.scoredatalog_path)
            score_after = source_signature(self.score_path)

            if (
                scoredatalog_before == scoredatalog_after
                and score_before == score_after
                and scoredatalog_version_before == scoredatalog_version_after
                and score_version_before == score_version_after
            ):
                aggregate = {
                    (str(row["sha256"]), int(row["mode"])): row
                    for row in aggregate_rows
                }
                return rows, aggregate, scoredatalog_after

        raise SourceChangedDuringRead(
            "scoredatalog.db or score.db changed during "
            f"{self.max_retries} consecutive reads"
        )

    @staticmethod
    def _copy_live_database(source: Path, destination_dir: Path) -> Path:
        destination = destination_dir / source.name
        shutil.copyfile(source, destination)
        for suffix in ("-wal", "-journal"):
            sidecar = Path(f"{source}{suffix}")
            try:
                shutil.copyfile(sidecar, Path(f"{destination}{suffix}"))
            except FileNotFoundError:
                continue
        return destination

    def _record_error(self, error: Exception, signature: SourceSignature | None) -> None:
        state = self._state()
        generation = int(state["source_generation"]) if state is not None else 0
        store.upsert_collector_state(
            self.conn,
            key="scoredatalog",
            source_generation=generation,
            last_mtime=signature.last_mtime if signature else None,
            last_size=signature.total_size if signature else None,
            last_run_at=int(self.clock()),
            last_error=f"{type(error).__name__}: {error}",
        )
        self.conn.commit()

    def _replay_signature(self) -> tuple[tuple[str, int, int, int, int], ...]:
        if not self.replay_dir.is_dir():
            return ()
        signals: list[tuple[str, int, int, int, int]] = []
        for path in sorted(self.replay_dir.glob("*.brd")):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            signals.append(
                (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
            )
        return tuple(signals)

    def _collect_replays(self, *, now: int, force: bool = False) -> TickResult:
        signature = self._replay_signature()
        signals = {item[0]: item[1:] for item in signature}
        states = store.load_replay_scan_states(self.conn)
        unseen = [item for item in signature if item[0] not in states]
        changed = [
            item
            for item in signature
            if item[0] in states and states[item[0]][:4] != item[1:]
        ]
        candidates = unseen + changed
        selected = candidates[:replay.MAX_REPLAY_FILES]
        if force and len(selected) < replay.MAX_REPLAY_FILES:
            selected_paths = {item[0] for item in selected}
            unchanged = [
                item for item in signature if item[0] not in selected_paths
            ]
            selected.extend(
                unchanged[: replay.MAX_REPLAY_FILES - len(selected)]
            )

        stale_paths = sorted(set(states).difference(signals))
        if not selected and not stale_paths:
            return TickResult(0, 0, False, scanned=False)

        report = replay.scan_report(
            self.replay_dir,
            candidates=(item[0] for item in selected),
        )
        matched = unmatched = ambiguous = overwritten = 0
        with self.conn:
            store.delete_replay_scan_states(self.conn, stale_paths)
            for metadata in report.metadata:
                status, was_overwritten, inserted = store.ingest_replay_metadata(
                    self.conn, metadata, observed_at=now
                )
                if not inserted:
                    continue
                if status == "matched":
                    matched += 1
                elif status == "ambiguous":
                    ambiguous += 1
                else:
                    unmatched += 1
                overwritten += int(was_overwritten)

            observations: list[dict[str, int | str]] = []
            for metadata in report.metadata:
                observations.append(
                    {
                        "path": str(metadata.path),
                        "device": metadata.device,
                        "inode": metadata.inode,
                        "mtime_ns": metadata.mtime_ns,
                        "compressed_size": metadata.compressed_size,
                        "outcome": "valid",
                        "checked_at": now,
                    }
                )
            for paths, outcome in (
                (report.invalid_paths, "invalid"),
                (report.unstable_paths, "unstable"),
            ):
                for path in paths:
                    signal = signals.get(str(path))
                    if signal is None:
                        continue
                    observations.append(
                        {
                            "path": str(path),
                            "device": signal[0],
                            "inode": signal[1],
                            "mtime_ns": signal[2],
                            "compressed_size": signal[3],
                            "outcome": outcome,
                            "checked_at": now,
                        }
                    )
            store.upsert_replay_scan_states(self.conn, observations)
            persisted_invalid, persisted_unstable = store.replay_scan_error_counts(
                self.conn
            )
            audit_invalid = max(persisted_invalid, report.invalid)
            audit_unstable = max(persisted_unstable, report.unstable)
            last_mtime = (
                max(item[3] for item in signature) / 1_000_000_000
                if signature else None
            )
            store.upsert_collector_state(
                self.conn,
                key="replay",
                source_generation=0,
                last_mtime=last_mtime,
                last_size=sum(item[4] for item in signature),
                last_run_at=now,
                last_error=(
                    None
                    if not audit_invalid and not audit_unstable
                    else f"invalid={audit_invalid}, unstable={audit_unstable}"
                ),
            )
        return TickResult(
            0, 0, False,
            scanned=bool(selected) or bool(report.invalid or report.unstable),
            replay_scanned=len(report.metadata) + report.invalid + report.unstable,
            replay_matched=matched,
            replay_unmatched=unmatched,
            replay_ambiguous=ambiguous,
            replay_invalid=report.invalid,
            replay_unstable=report.unstable,
            replay_overwritten=overwritten,
        )

    @staticmethod
    def _with_replays(score: TickResult, replays: TickResult) -> TickResult:
        return TickResult(
            score.new_plays,
            score.lost_events,
            score.generation_changed,
            scanned=score.scanned or replays.scanned,
            replay_scanned=replays.replay_scanned,
            replay_matched=replays.replay_matched,
            replay_unmatched=replays.replay_unmatched,
            replay_ambiguous=replays.replay_ambiguous,
            replay_invalid=replays.replay_invalid,
            replay_unstable=replays.replay_unstable,
            replay_overwritten=replays.replay_overwritten,
        )

    def tick(self, *, force: bool = False) -> TickResult:
        """Run at most one complete content scan."""

        try:
            trigger = source_signature(self.scoredatalog_path)
        except OSError as exc:
            self._record_error(exc, None)
            raise
        state = self._state()
        if not force:
            if self._last_signature is not None and trigger == self._last_signature:
                return self._collect_replays(now=int(self.clock()))
            if (
                self._last_signature is None
                and state is not None
                and state["last_error"] is None
                and _same_persisted_signal(
                    trigger, state["last_mtime"], state["last_size"]
                )
            ):
                self._last_signature = trigger
                return self._collect_replays(now=int(self.clock()))

        try:
            rows, aggregate, stable_signature = self._read_consistent_snapshot()
            state = self._state()
            generation = int(state["source_generation"]) if state is not None else 0
            cursors = store.load_cursors(self.conn, generation)
            current_keys = {
                (str(row["sha256"]), int(row["mode"])) for row in rows
            }

            replacement = (
                self._last_signature is not None
                and self._last_signature.main_identity
                != stable_signature.main_identity
            )
            # scoredatalog is append-by-chart and overwrite-by-key; wholesale
            # key disappearance is therefore another regeneration signal.
            replacement = replacement or bool(set(cursors).difference(current_keys))
            rollback = any(
                (key := (str(row["sha256"]), int(row["mode"]))) in cursors
                and int(row["playcount"]) < cursors[key][0]
                for row in rows
            )
            generation_changed = replacement or rollback
            if generation_changed:
                generation += 1
                cursors = {}

            now = int(self.clock())
            new_plays = 0
            lost_total = 0
            with self.conn:
                for row in rows:
                    key = (str(row["sha256"]), int(row["mode"]))
                    current_playcount = int(row["playcount"])
                    current_hash = payload_hash(row)
                    cursor = cursors.get(key)

                    should_insert = cursor is None or current_playcount > cursor[0]
                    payload_changed = (
                        cursor is not None
                        and current_playcount == cursor[0]
                        and current_hash != cursor[1]
                    )
                    if should_insert:
                        lost = (
                            max(0, current_playcount - cursor[0] - 1)
                            if cursor is not None
                            else 0
                        )
                        play = derive_play(
                            row,
                            source="collector",
                            source_generation=generation,
                            aggregate_row=aggregate.get(key),
                            lost_events=lost,
                            ingested_at=now,
                        )
                        if store.insert_play(self.conn, play):
                            new_plays += 1
                            lost_total += lost
                    elif payload_changed:
                        play = derive_play(
                            row,
                            source="collector",
                            source_generation=generation,
                            aggregate_row=aggregate.get(key),
                            ingested_at=now,
                        )
                        store.insert_play(self.conn, play, update_existing=True)

                    if should_insert or payload_changed:
                        store.upsert_cursor(
                            self.conn,
                            source_generation=generation,
                            sha256=key[0],
                            mode=key[1],
                            playcount=current_playcount,
                            payload_hash=current_hash,
                        )

                store.upsert_collector_state(
                    self.conn,
                    key="scoredatalog",
                    source_generation=generation,
                    last_mtime=stable_signature.last_mtime,
                    last_size=stable_signature.total_size,
                    last_run_at=now,
                    last_error=None,
                )

            self._last_signature = stable_signature
            score_result = TickResult(new_plays, lost_total, generation_changed)
            # A replay slot can become visible just before its score row. Scan
            # identical metadata again after a new play so an earlier
            # ``unmatched`` history row can be reconciled safely.
            replay_result = self._collect_replays(
                now=now, force=force or new_plays > 0
            )
            return self._with_replays(score_result, replay_result)
        except Exception as exc:
            self._record_error(exc, trigger)
            raise

    def run_daemon(
        self, on_tick: Callable[[TickResult], None] | None = None
    ) -> None:
        """Poll forever until interrupted by the caller."""

        while True:
            try:
                result = self.tick()
            except (OSError, sqlite3.Error, SourceChangedDuringRead) as exc:
                LOGGER.error("collector tick failed; retrying: %s", exc)
            else:
                if on_tick is not None:
                    on_tick(result)
            time.sleep(self.poll_interval)
