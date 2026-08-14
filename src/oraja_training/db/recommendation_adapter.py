"""Local SQLite adapter for profile-scoped recommendation input."""

from __future__ import annotations

from dataclasses import asdict
from collections.abc import Mapping
import json
import sqlite3
import time

from oraja_training.domain.types import (
    ProfileContext,
    RecommendationInput,
    RecommendationOutput,
)

from .model_adapter import latest_model


_CANDIDATE_QUERY = """
WITH ranked_state AS (
  SELECT score_state.*,
         row_number() OVER (
           PARTITION BY sha256 ORDER BY played_at DESC, mode ASC
         ) AS rn
  FROM score_state WHERE mode IN (0, 1)
), recent_ranked AS (
  SELECT p.*,
         row_number() OVER (
           PARTITION BY p.sha256 ORDER BY p.played_at DESC, p.id DESC
         ) AS recent_rank
  FROM plays AS p
  WHERE p.mode IN (0, 1) AND p.is_course = 0
    AND p.source IN ('collector', 'daily_snapshot')
), recent_summary AS (
  SELECT sha256,
         SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END) recent_successes,
         SUM(CASE WHEN completed = 0 THEN 1 ELSE 0 END) recent_failures,
         MAX(played_at) recent_played_at,
         MAX(bp_rate) recent_bp_rate
  FROM recent_ranked WHERE recent_rank <= 3
  GROUP BY sha256
)
SELECT c.sha256, c.md5, c.title, c.artist, c.notes,
       te.table_id, te.level,
       COALESCE(s.clear, 0) clear,
       COALESCE(s.playcount, 0) playcount,
       COALESCE(s.played_at, 0) last_played,
       s.minbp,
       COALESCE(r.recent_successes, 0) recent_successes,
       COALESCE(r.recent_failures, 0) recent_failures,
       COALESCE(r.recent_played_at, 0) recent_played_at,
       r.recent_bp_rate,
       COALESCE(f.density_p99, 0) density,
       COALESCE(f.scratch_p90, 0) scratch,
       COALESCE(f.scratch_rate, 0) model_scratch,
       COALESCE(f.ln_rate, 0) ln,
       COALESCE(f.soflan_changes, 0) soflan,
       f.density_p90, f.end_density, f.burst_max,
       f.scratch_rate, f.scratch_combo_rate, f.ln_rate,
       f.soflan_var, f.soflan_changes, f.stop_count, f.chart_seconds,
       pf.rhythm_family, pf.avg_chord, pf.chord_ge3,
       pf.micro_rate, pf.long_jack_rate, pf.practice_low
FROM charts c
JOIN table_entries te ON te.sha256 = c.sha256
LEFT JOIN ranked_state s ON s.sha256 = c.sha256 AND s.rn = 1
LEFT JOIN recent_summary r ON r.sha256 = c.sha256
LEFT JOIN chart_features f ON f.sha256 = c.sha256
LEFT JOIN chart_pattern_features pf ON pf.sha256 = c.sha256
WHERE c.song_mode = 7 AND c.notes > 0
ORDER BY c.sha256,
  CASE te.table_id
    WHEN 'genocide' THEN 0 WHEN 'overjoy' THEN 1
    WHEN 'satellite' THEN 2 WHEN 'stella' THEN 3 ELSE 4
  END,
  te.table_id
"""


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    columns = [str(column[0]) for column in cursor.description or ()]
    return dict(zip(columns, row if isinstance(row, tuple) else tuple(row)))


def _warmup_adjustment(conn: sqlite3.Connection) -> int:
    """Return a bounded shift from the latest observed WARMUP results."""

    session = conn.execute(
        "SELECT created_at, slots_json FROM sessions ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if session is None:
        return 0
    try:
        payload = json.loads(str(session[1]))
    except (TypeError, ValueError):
        return 0
    queue = payload.get("queue", ()) if isinstance(payload, dict) else payload
    if not isinstance(queue, list):
        return 0
    warmup = [
        item for item in queue
        if isinstance(item, dict) and item.get("category") == "01 WARMUP"
    ]
    observations: list[tuple[bool, float | None]] = []
    for item in warmup:
        sha256 = item.get("sha256")
        if not isinstance(sha256, str):
            continue
        play = conn.execute(
            """
            SELECT completed, bp_rate
            FROM plays
            WHERE sha256 = ? AND played_at >= ? AND played_at > 0
              AND is_course = 0
              AND source IN ('collector', 'daily_snapshot')
              AND completed IS NOT NULL
            ORDER BY played_at, id LIMIT 1
            """,
            (sha256, int(session[0])),
        ).fetchone()
        if play is not None:
            observations.append(
                (bool(play[0]), None if play[1] is None else float(play[1]))
            )
    if len(observations) < 2:
        return 0
    poor = [not completed or (bp_rate is not None and bp_rate >= 0.10)
            for completed, bp_rate in observations]
    rates = [rate for _, rate in observations if rate is not None]
    bp_worsened = (
        len(rates) >= 2 and rates[-1] >= 0.06 and rates[-1] - rates[0] >= 0.03
    )
    if sum(poor) >= 2 or poor[-1] or bp_worsened:
        return -1
    comfortable = [
        completed and bp_rate is not None and bp_rate <= 0.05
        for completed, bp_rate in observations
    ]
    return 1 if len(comfortable) >= 3 and all(comfortable) else 0


class SQLiteRecommendationRepository:
    """Local adapter for profile-scoped candidate reads and output writes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def load_input(self, profile: ProfileContext) -> RecommendationInput:
        """Read all recommendation input in one adapter-owned query boundary."""

        latest = self.conn.execute(
            "SELECT id, baseline_judged FROM daily_imports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        source_cursor = self.conn.execute(
            """
            SELECT table_id, page_url, header_url, data_url, fetched_at, last_error
            FROM table_sources ORDER BY table_id
            """
        )
        table_sources = tuple(
            _row_mapping(source_cursor, row) for row in source_cursor.fetchall()
        )
        if latest is None:
            return RecommendationInput(
                profile, 0, 0, (), table_sources=table_sources,
                warmup_adjustment=_warmup_adjustment(self.conn),
            )
        cursor = self.conn.execute(_CANDIDATE_QUERY)
        rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
        return RecommendationInput(
            profile=profile,
            import_id=int(latest[0]),
            baseline_judged=int(latest[1]),
            candidates=tuple(rows),
            model=latest_model(self.conn),
            table_sources=table_sources,
            warmup_adjustment=_warmup_adjustment(self.conn),
        )

    def save_output(self, output: RecommendationOutput) -> None:
        """Persist the storage-neutral result using the legacy SQLite schema."""

        generated_at = int(output.generated_at or time.time())
        payload = {
            "profile": asdict(output.profile),
            "menu_date": output.menu_date,
            "seed": output.seed,
            "import_id": output.import_id,
            "baseline_judged": output.baseline_judged,
            "model_version": output.model_version,
            "readiness": output.readiness,
            "generated_at": generated_at,
            "target_judged": output.target_judged,
            "reserve_target": output.reserve_target,
            "core_expected_judged": output.core_expected_judged,
            "reserve_expected_judged": output.reserve_expected_judged,
            "weakness_axes": list(output.weakness_axes),
            "model_status": output.model_status,
            "table_frontiers": list(output.table_frontiers),
            "table_warnings": list(output.table_warnings),
            "warmup_adjustment": output.warmup_adjustment,
            "queue": list(output.queue),
            "personal": list(output.personal),
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions(created_at, arm, slots_json) VALUES (?, ?, ?)",
                (
                    generated_at,
                    "model" if output.model_version else "heuristic",
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO recommendation_versions(
                  generated_at, menu_date, import_id, model_version,
                  seed, readiness, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    output.menu_date,
                    output.import_id,
                    output.model_version,
                    output.seed,
                    output.readiness,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )


def load_recommendation_input(
    conn: sqlite3.Connection,
    *,
    profile: ProfileContext | None = None,
) -> RecommendationInput:
    """Compatibility shim for the local SQLite adapter."""

    return SQLiteRecommendationRepository(conn).load_input(profile or ProfileContext())
