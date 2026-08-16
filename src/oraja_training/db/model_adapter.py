"""Local SQLite adapter for the database-independent model core."""

from __future__ import annotations

from collections import defaultdict
import json
import math
import sqlite3

from oraja_training.domain.types import ModelSnapshot, Observation
from oraja_training.model.core import (
    FEATURE_NAMES,
    TARGET,
    FitResult,
    fit_difficulty_frontier,
    fit_repository,
    numeric_level,
    predict_snapshot,
)


def _observations(conn: sqlite3.Connection) -> list[Observation]:
    lamp_rows = conn.execute(
        """
        WITH ranked_state AS (
          SELECT score_state.*,
                 row_number() OVER (
                   PARTITION BY sha256 ORDER BY played_at DESC, mode ASC
                 ) AS rn
          FROM score_state WHERE mode IN (0, 1)
        )
        SELECT te.table_id, te.sha256, te.level, s.clear
          FROM table_entries AS te
          JOIN ranked_state AS s ON s.sha256 = te.sha256 AND s.rn = 1
         WHERE s.playcount > 0 AND s.played_at > 0
         ORDER BY te.table_id, te.level, te.rowid
        """
    ).fetchall()
    table_outcomes: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    seen_lamps: set[tuple[str, str, str]] = set()
    for table_id, sha256, raw_level, clear in lamp_rows:
        level = numeric_level(raw_level)
        key = (str(table_id), str(sha256), str(raw_level))
        if level is None or key in seen_lamps:
            continue
        seen_lamps.add(key)
        table_outcomes[str(table_id)].append((level, int(clear) >= 4))

    rows = conn.execute(
        """
        WITH semantic_events AS (
          SELECT p.*,
                 row_number() OVER (
                   PARTITION BY p.sha256, p.mode, p.played_at, p.playcount
                   ORDER BY CASE p.source
                              WHEN 'collector' THEN 0
                              WHEN 'official_ir' THEN 1
                              WHEN 'daily_snapshot' THEN 2
                              ELSE 3
                            END,
                            p.ingested_at DESC, p.id DESC
                 ) AS semantic_rank
            FROM plays AS p
           WHERE p.source IN ('collector', 'official_ir', 'daily_snapshot')
        )
        SELECT p.id, p.played_at, p.completed, te.table_id, te.level,
               f.density_p99, f.scratch_rate
          FROM semantic_events AS p
          JOIN chart_features AS f ON f.sha256 = p.sha256
          JOIN table_entries AS te ON te.sha256 = p.sha256
         WHERE p.semantic_rank = 1 AND p.completed IS NOT NULL
           AND p.is_course = 0
           AND f.density_p99 IS NOT NULL
           AND f.scratch_rate IS NOT NULL
         ORDER BY p.played_at, p.id, te.table_id, te.level, te.rowid
        """
    ).fetchall()
    parsed: list[tuple[int, int, float, str, float, float, float]] = []
    for play_id, played_at, completed, table_id, raw_level, density, scratch in rows:
        level = numeric_level(raw_level)
        if level is None:
            continue
        values = (level, float(density), float(scratch))
        if not all(math.isfinite(value) for value in values):
            continue
        parsed.append(
            (
                int(play_id), int(played_at), float(bool(completed)),
                str(table_id), level, values[1], values[2],
            )
        )

    frontiers = {
        table_id: fit_difficulty_frontier(outcomes)
        for table_id, outcomes in table_outcomes.items()
    }
    support = {table_id: len(outcomes) for table_id, outcomes in table_outcomes.items()}
    by_play: dict[int, list[tuple[int, int, float, str, float, float, float]]] = defaultdict(list)
    for row in parsed:
        by_play[row[0]].append(row)

    result: list[Observation] = []
    for play_rows in by_play.values():
        supported_rows = [row for row in play_rows if row[3] in frontiers]
        if not supported_rows:
            continue
        selected = min(
            supported_rows,
            key=lambda row: (-support[row[3]], row[3], row[4]),
        )
        _, timestamp, completed, table_id, level, density, scratch = selected
        margin = (frontiers[table_id] - level) / 1.5
        result.append(
            Observation(
                played_at=timestamp,
                day=timestamp // 86_400,
                features=(margin, density, scratch),
                label=completed,
            )
        )
    return sorted(result, key=lambda row: row.played_at)


def _snapshot_from_row(row: sqlite3.Row | tuple[object, ...] | None) -> ModelSnapshot | None:
    if row is None:
        return None
    params = json.loads(row[3])
    metrics = {
        str(key): float(value)
        for key, value in params.get("metrics", {}).items()
    }
    return ModelSnapshot(
        target=str(row[0]),
        version=int(row[1]),
        trained_at=int(row[2]),
        n_train=int(row[4]),
        weights=tuple(float(value) for value in params["weights"]),
        means=tuple(float(value) for value in params["means"]),
        scales=tuple(float(value) for value in params["scales"]),
        feature_names=tuple(
            str(value)
            for value in params.get(
                "feature_names", ("level", "density_p99", "scratch_rate")
            )
        ),
        metrics=metrics,
        description=str(params.get("description", "")),
    )


def latest_model(conn: sqlite3.Connection) -> ModelSnapshot | None:
    return SQLiteModelRepository(conn).latest_model()


def _persist_model(conn: sqlite3.Connection, model: ModelSnapshot) -> None:
    params = {
        "description": model.description,
        "weights": list(model.weights),
        "means": list(model.means),
        "scales": list(model.scales),
        "feature_names": list(model.feature_names),
        "metrics": dict(model.metrics),
    }
    metrics = model.metrics
    conn.execute(
        """
        INSERT INTO model_state(
          target, version, trained_at, params_json, n_train,
          logloss, brier, baseline_logloss
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            model.target,
            model.version,
            model.trained_at,
            json.dumps(params, sort_keys=True, separators=(",", ":")),
            model.n_train,
            metrics.get("logloss"),
            metrics.get("brier"),
            metrics.get("baseline_logloss"),
        ),
    )


class SQLiteUnitOfWork:
    """Small transaction port used by database-independent model fitting."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> "SQLiteUnitOfWork":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


class SQLiteModelRepository:
    """Adapter implementing the model observation/snapshot repository port."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def iter_observations(self) -> list[Observation]:
        return _observations(self.conn)

    def latest_model(self) -> ModelSnapshot | None:
        row = self.conn.execute(
            """
            SELECT target, version, trained_at, params_json, n_train
              FROM model_state WHERE target = ? ORDER BY version DESC LIMIT 1
            """,
            (TARGET,),
        ).fetchone()
        return _snapshot_from_row(row)

    def save_model(self, model: ModelSnapshot) -> None:
        _persist_model(self.conn, model)


def fit_latest(conn: sqlite3.Connection, *, trained_at: int | None = None) -> FitResult:
    """Load observations, run the pure fitter, and persist only new models."""

    return fit_repository(
        SQLiteModelRepository(conn),
        unit_of_work=SQLiteUnitOfWork(conn),
        trained_at=trained_at,
    )


def predict_latest(
    conn: sqlite3.Connection,
    table_completion_margin: float,
    density: float,
    scratch: float,
) -> float | None:
    return predict_snapshot(
        latest_model(conn), table_completion_margin, density, scratch
    )
