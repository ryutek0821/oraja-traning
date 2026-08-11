"""Local SQLite adapter for the database-independent model core."""

from __future__ import annotations

import json
import math
import sqlite3

from oraja_training.domain.types import ModelSnapshot, Observation
from oraja_training.model.core import (
    FEATURE_NAMES,
    TARGET,
    FitResult,
    fit_repository,
    numeric_level,
    predict_snapshot,
)


def _observations(conn: sqlite3.Connection) -> list[Observation]:
    rows = conn.execute(
        """
        SELECT p.played_at, p.completed, te.level,
               f.density_p99, f.scratch_rate
          FROM plays AS p
          JOIN chart_features AS f ON f.sha256 = p.sha256
          JOIN table_entries AS te ON te.rowid = (
               SELECT candidate.rowid
                 FROM table_entries AS candidate
                WHERE candidate.sha256 = p.sha256
                ORDER BY candidate.table_id, candidate.level, candidate.rowid
                LIMIT 1
          )
         WHERE p.source IN ('collector', 'daily_snapshot')
           AND p.completed IS NOT NULL
           AND p.is_course = 0
           AND f.density_p99 IS NOT NULL
           AND f.scratch_rate IS NOT NULL
         ORDER BY p.played_at, p.id
        """
    ).fetchall()
    result: list[Observation] = []
    for played_at, completed, raw_level, density, scratch in rows:
        level = numeric_level(raw_level)
        values = (level, float(density), float(scratch)) if level is not None else None
        if values is None or not all(math.isfinite(value) for value in values):
            continue
        timestamp = int(played_at)
        result.append(
            Observation(
                played_at=timestamp,
                day=timestamp // 86_400,
                features=values,
                label=float(bool(completed)),
            )
        )
    return result


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
            str(value) for value in params.get("feature_names", FEATURE_NAMES)
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
    conn: sqlite3.Connection, level: float, density: float, scratch: float
) -> float | None:
    return predict_snapshot(latest_model(conn), level, density, scratch)
