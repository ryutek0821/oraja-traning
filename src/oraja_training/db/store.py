"""Schema and write helpers for the assistant-owned SQLite database."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import sqlite3
import json
from typing import Any


SCHEMA_VERSION = 9
BEATORAJA_DB_NAMES = {
    "score.db",
    "scoredatalog.db",
    "scorelog.db",
    "songdata.db",
    "songinfo.db",
}
REPLAY_MATCH_TOLERANCE_SECONDS = 30
REPLAY_LN_MODES = frozenset({1, 2})


SCHEMA_V2 = """
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE charts (
  sha256      TEXT PRIMARY KEY,
  md5         TEXT,
  title       TEXT,
  artist      TEXT,
  notes       INTEGER,
  song_mode   INTEGER,
  path        TEXT,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_charts_md5 ON charts(md5);

CREATE TABLE plays (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256                    TEXT NOT NULL,
  mode                      INTEGER NOT NULL,
  played_at                 INTEGER NOT NULL,
  playcount                 INTEGER NOT NULL,
  source_generation         INTEGER NOT NULL,
  source                    TEXT NOT NULL,
  clear                     INTEGER NOT NULL,
  ex                        INTEGER,
  minbp                     INTEGER,
  notes                     INTEGER,
  judged                    INTEGER,
  empty_poor                INTEGER,
  survival                  REAL,
  completed                 INTEGER,
  bp_rate                   REAL,
  credited_gauge_kind       TEXT,
  selected_gauge_kind       TEXT,
  option                    INTEGER,
  seed                      INTEGER,
  random                    INTEGER,
  trophy                    TEXT,
  is_course                 INTEGER NOT NULL DEFAULT 0,
  exceeded_aggregate_score  INTEGER NOT NULL DEFAULT 0,
  lost_events               INTEGER NOT NULL DEFAULT 0,
  payload_hash              TEXT NOT NULL,
  ingested_at               INTEGER NOT NULL,
  UNIQUE(source_generation, sha256, mode, playcount)
);
CREATE INDEX idx_plays_played_at ON plays(played_at);
CREATE INDEX idx_plays_sha256 ON plays(sha256, mode);

CREATE TABLE chart_cursor (
  source_generation INTEGER NOT NULL,
  sha256            TEXT NOT NULL,
  mode              INTEGER NOT NULL,
  last_playcount    INTEGER NOT NULL,
  last_payload_hash TEXT NOT NULL,
  PRIMARY KEY(source_generation, sha256, mode)
);

CREATE TABLE collector_state (
  key               TEXT PRIMARY KEY,
  source_generation INTEGER NOT NULL,
  last_mtime        REAL,
  last_size         INTEGER,
  last_run_at       INTEGER,
  last_error        TEXT
);

CREATE TABLE chart_features (
  sha256          TEXT PRIMARY KEY,
  feature_version INTEGER NOT NULL,
  density_mean REAL, density_p90 REAL, density_p99 REAL,
  end_density REAL, burst_max REAL,
  scratch_rate REAL, scratch_p90 REAL, scratch_combo_rate REAL,
  ln_rate REAL, soflan_var REAL, soflan_changes INTEGER, stop_count INTEGER,
  chart_seconds REAL, total_notes INTEGER
);

CREATE TABLE table_entries (
  table_id   TEXT NOT NULL,
  level      TEXT NOT NULL,
  sha256     TEXT,
  md5        TEXT,
  title      TEXT,
  fetched_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_table_entries
  ON table_entries(table_id, level, COALESCE(sha256, md5));

CREATE TABLE model_state (
  target           TEXT NOT NULL,
  version          INTEGER NOT NULL,
  trained_at       INTEGER NOT NULL,
  params_json      TEXT NOT NULL,
  n_train          INTEGER NOT NULL,
  logloss          REAL,
  brier            REAL,
  baseline_logloss REAL,
  PRIMARY KEY(target, version)
);

CREATE TABLE predictions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256             TEXT NOT NULL,
  mode               INTEGER NOT NULL,
  predicted_at       INTEGER NOT NULL,
  model_target       TEXT NOT NULL,
  model_version      INTEGER NOT NULL,
  p_pred             REAL NOT NULL,
  baseline_p         REAL,
  candidate_set_json TEXT,
  selection_prob     REAL,
  is_exploration     INTEGER NOT NULL DEFAULT 0,
  resolved_play_id   INTEGER
);

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  arm        TEXT,
  slots_json TEXT NOT NULL
);

CREATE TABLE revisits (
  sha256        TEXT NOT NULL,
  mode          INTEGER NOT NULL,
  due_at        INTEGER NOT NULL,
  interval_days INTEGER NOT NULL,
  last_result   TEXT,
  PRIMARY KEY(sha256, mode)
);
"""


SCHEMA_V3 = """
CREATE TABLE daily_imports (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  imported_at          INTEGER NOT NULL,
  effective_date       TEXT NOT NULL,
  score_sha256         TEXT NOT NULL,
  scoredatalog_sha256  TEXT NOT NULL,
  baseline_judged      INTEGER NOT NULL,
  cumulative_playcount INTEGER NOT NULL,
  new_play_rows        INTEGER NOT NULL DEFAULT 0,
  lost_events          INTEGER NOT NULL DEFAULT 0,
  UNIQUE(score_sha256, scoredatalog_sha256)
);

CREATE TABLE player_daily (
  date                 INTEGER PRIMARY KEY,
  cumulative_playcount INTEGER NOT NULL,
  cumulative_judged    INTEGER NOT NULL,
  cumulative_empty_poor INTEGER NOT NULL,
  cumulative_playtime  INTEGER NOT NULL,
  delta_playcount      INTEGER,
  delta_judged         INTEGER,
  delta_empty_poor     INTEGER,
  delta_playtime       INTEGER,
  import_id            INTEGER NOT NULL REFERENCES daily_imports(id)
);

CREATE TABLE score_state (
  sha256       TEXT NOT NULL,
  mode         INTEGER NOT NULL,
  clear        INTEGER NOT NULL,
  ex           INTEGER,
  minbp        INTEGER,
  combo        INTEGER,
  notes        INTEGER,
  playcount    INTEGER NOT NULL,
  played_at    INTEGER NOT NULL,
  import_id    INTEGER NOT NULL REFERENCES daily_imports(id),
  payload_hash TEXT NOT NULL,
  PRIMARY KEY(sha256, mode)
);

CREATE TABLE score_changes (
  import_id        INTEGER NOT NULL REFERENCES daily_imports(id),
  sha256           TEXT NOT NULL,
  mode             INTEGER NOT NULL,
  playcount_delta  INTEGER NOT NULL,
  old_clear        INTEGER,
  new_clear        INTEGER NOT NULL,
  old_ex           INTEGER,
  new_ex           INTEGER,
  old_minbp        INTEGER,
  new_minbp        INTEGER,
  old_combo        INTEGER,
  new_combo        INTEGER,
  PRIMARY KEY(import_id, sha256, mode)
);

CREATE TABLE table_sources (
  table_id       TEXT PRIMARY KEY,
  page_url       TEXT NOT NULL,
  header_url     TEXT,
  data_url       TEXT,
  etag           TEXT,
  last_modified  TEXT,
  fetched_at     INTEGER,
  cache_path     TEXT,
  last_error     TEXT
);

CREATE TABLE recommendation_versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_at  INTEGER NOT NULL,
  menu_date     TEXT NOT NULL,
  import_id     INTEGER NOT NULL REFERENCES daily_imports(id),
  model_version INTEGER NOT NULL DEFAULT 0,
  seed          TEXT NOT NULL,
  readiness     TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  UNIQUE(menu_date, import_id, model_version, readiness)
);
"""


SCHEMA_V4 = """
CREATE TABLE replay_metadata (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  path                   TEXT NOT NULL,
  content_hash           TEXT NOT NULL,
  previous_content_hash  TEXT,
  observed_at            INTEGER NOT NULL,
  mtime_ns               INTEGER NOT NULL,
  compressed_size        INTEGER NOT NULL,
  sha256                 TEXT NOT NULL,
  mode                   INTEGER NOT NULL,
  played_at              INTEGER NOT NULL,
  gauge                  INTEGER NOT NULL,
  selected_gauge_kind    TEXT NOT NULL,
  randomoption           INTEGER,
  randomoptionseed       INTEGER,
  randomoption2          INTEGER,
  randomoption2seed      INTEGER,
  doubleoption           INTEGER,
  seven_to_nine_pattern  INTEGER,
  lane_shuffle_json      TEXT,
  rand_json              TEXT,
  match_status           TEXT NOT NULL,
  matched_play_id        INTEGER REFERENCES plays(id),
  UNIQUE(path, content_hash),
  CHECK(match_status IN ('matched', 'unmatched', 'ambiguous'))
);
CREATE INDEX idx_replay_metadata_play
  ON replay_metadata(sha256, mode, played_at);
"""


SCHEMA_V5 = """
CREATE TABLE experiments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT NOT NULL,
  seed                TEXT NOT NULL,
  starts_at           INTEGER NOT NULL,
  ends_at             INTEGER NOT NULL,
  min_samples_per_arm INTEGER NOT NULL,
  created_at          INTEGER NOT NULL,
  CHECK(ends_at > starts_at),
  CHECK(min_samples_per_arm > 0)
);

CREATE TABLE experiment_sessions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id         INTEGER NOT NULL REFERENCES experiments(id),
  session_key           TEXT NOT NULL,
  session_at            INTEGER NOT NULL,
  arm                   TEXT NOT NULL,
  arm_probability       REAL NOT NULL,
  selection_probability REAL NOT NULL,
  candidate_hash        TEXT NOT NULL,
  candidates_json       TEXT NOT NULL,
  selected_sha256       TEXT NOT NULL,
  selected_mode         INTEGER NOT NULL,
  selected_p_pred       REAL NOT NULL,
  transfer_sha256       TEXT NOT NULL,
  transfer_mode         INTEGER NOT NULL,
  transfer_p_pred       REAL NOT NULL,
  assigned_at           INTEGER NOT NULL,
  UNIQUE(experiment_id, session_key),
  CHECK(arm IN ('coach', 'control')),
  CHECK(arm_probability > 0 AND arm_probability <= 1),
  CHECK(selection_probability > 0 AND selection_probability <= 1),
  CHECK(selected_p_pred >= 0 AND selected_p_pred <= 1),
  CHECK(transfer_p_pred >= 0 AND transfer_p_pred <= 1)
);
CREATE INDEX idx_experiment_sessions_experiment
  ON experiment_sessions(experiment_id, arm);

CREATE TABLE experiment_targets (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id       INTEGER NOT NULL REFERENCES experiment_sessions(id),
  target_kind      TEXT NOT NULL,
  interval_days    INTEGER NOT NULL,
  sha256           TEXT NOT NULL,
  mode             INTEGER NOT NULL,
  due_at           INTEGER NOT NULL,
  window_closes_at INTEGER NOT NULL,
  p_pred           REAL NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  outcome          INTEGER,
  resolved_play_id INTEGER REFERENCES plays(id),
  resolved_at      INTEGER,
  resolution_note  TEXT,
  UNIQUE(session_id, target_kind, interval_days),
  CHECK(target_kind IN ('retention', 'transfer')),
  CHECK(interval_days IN (1, 3, 7, 14)),
  CHECK(status IN ('pending', 'resolved', 'missing', 'duplicate')),
  CHECK(outcome IS NULL OR outcome IN (0, 1)),
  CHECK(p_pred >= 0 AND p_pred <= 1),
  CHECK(window_closes_at > due_at)
);
CREATE INDEX idx_experiment_targets_resolution
  ON experiment_targets(status, due_at, window_closes_at);
"""


SCHEMA_V6 = """
CREATE TABLE chart_pattern_features (
  sha256          TEXT PRIMARY KEY,
  rhythm_family   INTEGER,
  avg_chord       REAL,
  chord_ge3       REAL,
  micro_rate      REAL,
  long_jack_rate  REAL,
  practice_low    INTEGER,
  analysis_version INTEGER NOT NULL DEFAULT 0
);
"""


SCHEMA_V7 = """
ALTER TABLE table_sources ADD COLUMN entry_count INTEGER;
ALTER TABLE table_sources ADD COLUMN matched_count INTEGER;
"""


SCHEMA_V8 = """
ALTER TABLE chart_pattern_features ADD COLUMN grid_bpm REAL;
ALTER TABLE chart_pattern_features ADD COLUMN stream_sec REAL;
ALTER TABLE chart_pattern_features ADD COLUMN last_kill REAL;
"""


SCHEMA_V9 = """
CREATE TABLE replay_scan_state (
  path             TEXT PRIMARY KEY,
  device           INTEGER NOT NULL,
  inode            INTEGER NOT NULL,
  mtime_ns         INTEGER NOT NULL,
  compressed_size  INTEGER NOT NULL,
  outcome          TEXT NOT NULL,
  checked_at       INTEGER NOT NULL,
  CHECK(outcome IN ('valid', 'invalid', 'unstable'))
);
"""


PLAY_COLUMNS = (
    "sha256",
    "mode",
    "played_at",
    "playcount",
    "source_generation",
    "source",
    "clear",
    "ex",
    "minbp",
    "notes",
    "judged",
    "empty_poor",
    "survival",
    "completed",
    "bp_rate",
    "credited_gauge_kind",
    "selected_gauge_kind",
    "option",
    "seed",
    "random",
    "trophy",
    "is_course",
    "exceeded_aggregate_score",
    "lost_events",
    "payload_hash",
    "ingested_at",
)


def _schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("schema_version exists but contains no version")
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> None:
    """Migrate an assistant database to the current schema version."""

    current = _schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"assistant DB schema {current} is newer than supported {SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return

    if current == 1:
        raise RuntimeError(
            "assistant DB schema_version 1 cannot be migrated safely; "
            "delete assistant.db and rerun backfill"
        )

    if current == 0:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + SCHEMA_V2 + SCHEMA_V3 + SCHEMA_V4 + SCHEMA_V5 + SCHEMA_V6
                + SCHEMA_V7 + SCHEMA_V8 + SCHEMA_V9
            )
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 2:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V3 + SCHEMA_V4 + SCHEMA_V5
                + SCHEMA_V6 + SCHEMA_V7 + SCHEMA_V8 + SCHEMA_V9
            )
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 3:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V4 + SCHEMA_V5 + SCHEMA_V6
                + SCHEMA_V7 + SCHEMA_V8 + SCHEMA_V9
            )
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 4:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V5 + SCHEMA_V6 + SCHEMA_V7
                + SCHEMA_V8 + SCHEMA_V9
            )
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 5:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V6 + SCHEMA_V7 + SCHEMA_V8
                + SCHEMA_V9
            )
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 6:
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA_V7 + SCHEMA_V8 + SCHEMA_V9
            )
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 7:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V8 + SCHEMA_V9)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if current == 8:
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_V9)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    raise RuntimeError(f"unsupported assistant DB schema_version {current}")


def load_replay_scan_states(
    conn: sqlite3.Connection,
) -> dict[str, tuple[int, int, int, int, str]]:
    """Return the last durably checked filesystem identity for each replay path."""

    rows = conn.execute(
        """
        SELECT path, device, inode, mtime_ns, compressed_size, outcome
        FROM replay_scan_state
        """
    )
    return {
        str(row[0]): (
            int(row[1]),
            int(row[2]),
            int(row[3]),
            int(row[4]),
            str(row[5]),
        )
        for row in rows
    }


def upsert_replay_scan_states(
    conn: sqlite3.Connection, observations: Iterable[Mapping[str, Any]]
) -> None:
    """Persist checked replay identities, including fail-closed outcomes."""

    conn.executemany(
        """
        INSERT INTO replay_scan_state(
          path, device, inode, mtime_ns, compressed_size, outcome, checked_at
        ) VALUES (
          :path, :device, :inode, :mtime_ns, :compressed_size, :outcome,
          :checked_at
        )
        ON CONFLICT(path) DO UPDATE SET
          device = excluded.device,
          inode = excluded.inode,
          mtime_ns = excluded.mtime_ns,
          compressed_size = excluded.compressed_size,
          outcome = excluded.outcome,
          checked_at = excluded.checked_at
        """,
        observations,
    )


def delete_replay_scan_states(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    """Forget scan state for replay slots that no longer exist."""

    conn.executemany(
        "DELETE FROM replay_scan_state WHERE path = ?",
        ((path,) for path in paths),
    )


def replay_scan_error_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return current persisted invalid and unstable slot counts."""

    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """
            SELECT outcome, count(*) FROM replay_scan_state
            WHERE outcome != 'valid'
            GROUP BY outcome
            """
        )
    }
    return counts.get("invalid", 0), counts.get("unstable", 0)


def ingest_replay_metadata(
    conn: sqlite3.Connection, metadata: Any, *, observed_at: int
) -> tuple[str, bool, bool]:
    """Persist a new slot version and update only an exactly matched play.

    Returns ``(status, overwritten, changed)``. Re-observing identical slot
    content is idempotent, except that an unmatched row may be reconciled after
    its score event arrives.
    """

    def refresh_play_gauge(play_id: int) -> None:
        replacement = conn.execute(
            """
            SELECT selected_gauge_kind FROM replay_metadata
            WHERE match_status = 'matched' AND matched_play_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (play_id,),
        ).fetchone()
        conn.execute(
            "UPDATE plays SET selected_gauge_kind = ? WHERE id = ?",
            (None if replacement is None else str(replacement[0]), play_id),
        )

    path = str(metadata.path)
    existing = conn.execute(
        """
        SELECT id, match_status, matched_play_id FROM replay_metadata
        WHERE path = ? AND content_hash = ?
        """,
        (path, metadata.content_hash),
    ).fetchone()
    def play_matches(mode: int) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id FROM plays
            WHERE sha256 = ? AND mode = ?
              AND played_at >= ? AND played_at <= ? AND is_course = 0
            ORDER BY id
            LIMIT 2
            """,
            (
                metadata.sha256,
                mode,
                metadata.played_at,
                metadata.played_at + REPLAY_MATCH_TOLERANCE_SECONDS,
            ),
        ).fetchall()

    matches = play_matches(metadata.mode)
    if not matches and metadata.mode in REPLAY_LN_MODES:
        # ReplayData always stores the configured LN mode. ScoreData stores 0
        # for charts without undefined LN, so use that normalization only when
        # there is no exact-mode candidate.
        matches = play_matches(0)
    if existing is not None:
        previous_status = str(existing[1])
        previous_play_id = None if existing[2] is None else int(existing[2])
        if len(matches) == 1:
            matched_play_id = int(matches[0][0])
            if previous_status == "matched" and previous_play_id == matched_play_id:
                return previous_status, False, False
            conn.execute(
                "UPDATE replay_metadata SET match_status = 'matched', matched_play_id = ? WHERE id = ?",
                (matched_play_id, int(existing[0])),
            )
            if previous_play_id is not None and previous_play_id != matched_play_id:
                refresh_play_gauge(previous_play_id)
            refresh_play_gauge(matched_play_id)
            return "matched", False, True
        if len(matches) > 1 and previous_status != "ambiguous":
            conn.execute(
                """
                UPDATE replay_metadata
                SET match_status = 'ambiguous', matched_play_id = NULL
                WHERE id = ?
                """,
                (int(existing[0]),),
            )
            if previous_play_id is not None:
                refresh_play_gauge(previous_play_id)
            return "ambiguous", False, True
        return previous_status, False, False

    previous = conn.execute(
        "SELECT content_hash FROM replay_metadata WHERE path = ? ORDER BY id DESC LIMIT 1",
        (path,),
    ).fetchone()
    previous_hash = None if previous is None else str(previous[0])
    if len(matches) == 1:
        status = "matched"
        matched_play_id = int(matches[0][0])
    elif matches:
        status = "ambiguous"
        matched_play_id = None
    else:
        status = "unmatched"
        matched_play_id = None

    conn.execute(
        """
        INSERT INTO replay_metadata(
          path, content_hash, previous_content_hash, observed_at, mtime_ns,
          compressed_size, sha256, mode, played_at, gauge, selected_gauge_kind,
          randomoption, randomoptionseed, randomoption2, randomoption2seed,
          doubleoption, seven_to_nine_pattern, lane_shuffle_json, rand_json,
          match_status, matched_play_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            path, metadata.content_hash, previous_hash, int(observed_at),
            metadata.mtime_ns, metadata.compressed_size, metadata.sha256,
            metadata.mode, metadata.played_at, metadata.gauge,
            metadata.selected_gauge_kind, metadata.randomoption,
            metadata.randomoptionseed, metadata.randomoption2,
            metadata.randomoption2seed, metadata.doubleoption,
            metadata.seven_to_nine_pattern,
            None if metadata.lane_shuffle_pattern is None else json.dumps(metadata.lane_shuffle_pattern, separators=(",", ":")),
            None if metadata.rand is None else json.dumps(metadata.rand, separators=(",", ":")),
            status, matched_play_id,
        ),
    )
    if matched_play_id is not None:
        refresh_play_gauge(matched_play_id)
    return status, previous_hash is not None, True


def init(path: str | Path) -> sqlite3.Connection:
    """Open and migrate the only database this tool is allowed to write."""

    db_path = Path(path).expanduser()
    if db_path.name in BEATORAJA_DB_NAMES:
        raise ValueError(f"refusing to use beatoraja source name as store: {db_path.name}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def insert_play(
    conn: sqlite3.Connection,
    play: Mapping[str, Any],
    *,
    update_existing: bool = False,
) -> bool:
    """Insert one event and return whether a new row was created.

    A same-generation/same-playcount payload correction can update the existing
    event in place.  It is still the same event key and is therefore not counted
    as a new play.
    """

    values = tuple(play[column] for column in PLAY_COLUMNS)
    existed = False
    if update_existing:
        existed = (
            conn.execute(
                """
                SELECT 1 FROM plays
                WHERE source_generation = ? AND sha256 = ? AND mode = ?
                  AND playcount = ?
                """,
                (
                    play["source_generation"],
                    play["sha256"],
                    play["mode"],
                    play["playcount"],
                ),
            ).fetchone()
            is not None
        )
    placeholders = ", ".join("?" for _ in PLAY_COLUMNS)
    columns = ", ".join(PLAY_COLUMNS)
    if not update_existing:
        sql = f"INSERT OR IGNORE INTO plays ({columns}) VALUES ({placeholders})"
    else:
        mutable = [
            column
            for column in PLAY_COLUMNS
            if column
            not in {
                "source_generation",
                "sha256",
                "mode",
                "playcount",
                "source",
                "lost_events",
            }
        ]
        assignments = ", ".join(f"{column} = excluded.{column}" for column in mutable)
        sql = (
            f"INSERT INTO plays ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT(source_generation, sha256, mode, playcount) "
            f"DO UPDATE SET {assignments}"
        )
    before = conn.total_changes
    conn.execute(sql, values)
    return conn.total_changes > before and not existed


def upsert_cursor(
    conn: sqlite3.Connection,
    *,
    source_generation: int,
    sha256: str,
    mode: int,
    playcount: int,
    payload_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO chart_cursor(
          source_generation, sha256, mode, last_playcount, last_payload_hash
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_generation, sha256, mode) DO UPDATE SET
          last_playcount = excluded.last_playcount,
          last_payload_hash = excluded.last_payload_hash
        WHERE excluded.last_playcount >= chart_cursor.last_playcount
        """,
        (source_generation, sha256, mode, playcount, payload_hash),
    )


def load_cursors(
    conn: sqlite3.Connection, source_generation: int
) -> dict[tuple[str, int], tuple[int, str]]:
    rows = conn.execute(
        """
        SELECT sha256, mode, last_playcount, last_payload_hash
        FROM chart_cursor WHERE source_generation = ?
        """,
        (source_generation,),
    )
    return {
        (str(row["sha256"]), int(row["mode"])): (
            int(row["last_playcount"]),
            str(row["last_payload_hash"]),
        )
        for row in rows
    }


def upsert_charts(
    conn: sqlite3.Connection, charts: Iterable[Mapping[str, Any]]
) -> None:
    """Store one deterministic representative path for each content hash."""

    conn.executemany(
        """
        INSERT INTO charts(sha256, md5, title, artist, notes, song_mode, path, updated_at)
        VALUES (:sha256, :md5, :title, :artist, :notes, :song_mode, :path, :updated_at)
        ON CONFLICT(sha256) DO UPDATE SET
          md5 = excluded.md5,
          title = excluded.title,
          artist = excluded.artist,
          notes = excluded.notes,
          song_mode = excluded.song_mode,
          path = CASE
            WHEN charts.path IS NULL THEN excluded.path
            WHEN excluded.path IS NULL THEN charts.path
            WHEN excluded.path < charts.path THEN excluded.path
            ELSE charts.path
          END,
          updated_at = excluded.updated_at
        """,
        charts,
    )


def upsert_chart_pattern_features(
    conn: sqlite3.Connection, features: Iterable[Mapping[str, Any]]
) -> None:
    """Copy optional oraja-constellator analysis into the assistant store."""

    conn.executemany(
        """
        INSERT INTO chart_pattern_features(
          sha256, rhythm_family, avg_chord, chord_ge3, micro_rate,
          long_jack_rate, practice_low, analysis_version, grid_bpm,
          stream_sec, last_kill
        ) VALUES (
          :sha256, :rhythm_family, :avg_chord, :chord_ge3, :micro_rate,
          :long_jack_rate, :practice_low, :analysis_version, :grid_bpm,
          :stream_sec, :last_kill
        )
        ON CONFLICT(sha256) DO UPDATE SET
          rhythm_family=excluded.rhythm_family,
          avg_chord=excluded.avg_chord,
          chord_ge3=excluded.chord_ge3,
          micro_rate=excluded.micro_rate,
          long_jack_rate=excluded.long_jack_rate,
          practice_low=excluded.practice_low,
          analysis_version=excluded.analysis_version,
          grid_bpm=excluded.grid_bpm,
          stream_sec=excluded.stream_sec,
          last_kill=excluded.last_kill
        """,
        features,
    )


def upsert_collector_state(
    conn: sqlite3.Connection,
    *,
    key: str,
    source_generation: int,
    last_mtime: float | None,
    last_size: int | None,
    last_run_at: int | None,
    last_error: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO collector_state(
          key, source_generation, last_mtime, last_size, last_run_at, last_error
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          source_generation = excluded.source_generation,
          last_mtime = excluded.last_mtime,
          last_size = excluded.last_size,
          last_run_at = excluded.last_run_at,
          last_error = excluded.last_error
        """,
        (
            key,
            source_generation,
            last_mtime,
            last_size,
            last_run_at,
            last_error,
        ),
    )
