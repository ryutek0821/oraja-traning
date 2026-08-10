"""Fit and gate the observed-completion model.

The target is completion under the player's habitual settings.  It is not a
gauge-conditional clear probability: the source data does not reliably record
the gauge selected at the beginning of a play.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import random
import sqlite3
import time
from typing import Sequence


TARGET = "observed_completion"
FEATURE_NAMES = ("level", "density_p99", "scratch_rate")
MIN_RESULTS = 200
MIN_SESSIONS = 10
HOLDOUT_FRACTION = 0.20
BOOTSTRAP_SAMPLES = 100
GATE_FRACTION = 0.80


@dataclass(frozen=True)
class FitResult:
    """Outcome of a fit attempt; only ``passed`` outcomes are persisted."""

    status: str
    n_results: int
    n_sessions: int
    n_train: int = 0
    n_holdout: int = 0
    gate_fraction: float | None = None
    version: int | None = None
    metrics: dict[str, float] | None = None


@dataclass(frozen=True)
class _Observation:
    played_at: int
    day: int
    features: tuple[float, float, float]
    label: float


def _numeric_level(value: object) -> float | None:
    text = str(value).strip()
    try:
        result = float(text)
    except (TypeError, ValueError):
        # Common table labels include a scale prefix, e.g. "★12" or "sl4".
        number = ""
        started = False
        for char in text:
            if char.isdigit() or (char in ".-" and not started):
                number += char
                started = True
            elif started:
                break
        try:
            result = float(number)
        except ValueError:
            return None
    return result if math.isfinite(result) else None


def _observations(conn: sqlite3.Connection) -> list[_Observation]:
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
    result: list[_Observation] = []
    for played_at, completed, raw_level, density, scratch in rows:
        level = _numeric_level(raw_level)
        values = (level, float(density), float(scratch)) if level is not None else None
        if values is None or not all(math.isfinite(value) for value in values):
            continue
        timestamp = int(played_at)
        result.append(
            _Observation(
                played_at=timestamp,
                day=timestamp // 86_400,
                features=values,
                label=float(bool(completed)),
            )
        )
    return result


def _standardize(
    train: Sequence[_Observation], feature_indexes: Sequence[int]
) -> tuple[list[float], list[float]]:
    means: list[float] = []
    scales: list[float] = []
    for index in feature_indexes:
        values = [row.features[index] for row in train]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        means.append(mean)
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    return means, scales


def _matrix(
    rows: Sequence[_Observation],
    feature_indexes: Sequence[int],
    means: Sequence[float],
    scales: Sequence[float],
) -> list[list[float]]:
    return [
        [1.0]
        + [
            (row.features[index] - means[column]) / scales[column]
            for column, index in enumerate(feature_indexes)
        ]
        for row in rows
    ]


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-12:
            divisor = 1e-12
        for item in range(column, size + 1):
            augmented[column][item] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            for item in range(column, size + 1):
                augmented[row][item] -= factor * augmented[column][item]
    return [augmented[row][size] for row in range(size)]


def _fit_logistic(x: Sequence[Sequence[float]], y: Sequence[float]) -> list[float]:
    """Deterministic Newton fit with an unpenalized intercept."""

    width = len(x[0])
    weights = [0.0] * width
    l2 = 0.01
    count = float(len(x))
    for _ in range(100):
        probabilities = [_sigmoid(sum(w * value for w, value in zip(weights, row))) for row in x]
        gradient = [0.0] * width
        hessian = [[0.0] * width for _ in range(width)]
        for row, label, probability in zip(x, y, probabilities):
            error = probability - label
            curvature = probability * (1.0 - probability)
            for left in range(width):
                gradient[left] += error * row[left] / count
                for right in range(width):
                    hessian[left][right] += curvature * row[left] * row[right] / count
        for index in range(1, width):
            gradient[index] += l2 * weights[index]
            hessian[index][index] += l2
        hessian[0][0] += 1e-9
        step = _solve(hessian, gradient)
        weights = [weight - change for weight, change in zip(weights, step)]
        if max(abs(change) for change in step) < 1e-8:
            break
    return weights


def _probabilities(x: Sequence[Sequence[float]], weights: Sequence[float]) -> list[float]:
    return [_sigmoid(sum(w * value for w, value in zip(weights, row))) for row in x]


def _losses(labels: Sequence[float], predictions: Sequence[float]) -> list[float]:
    result = []
    for label, prediction in zip(labels, predictions):
        probability = min(max(prediction, 1e-15), 1.0 - 1e-15)
        result.append(-(label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability)))
    return result


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _bootstrap_fraction(
    rows: Sequence[_Observation], baseline_losses: Sequence[float], full_losses: Sequence[float]
) -> float:
    by_day: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        by_day.setdefault(row.day, []).append(index)
    days = sorted(by_day)
    generator = random.Random(0)
    improvements = 0
    for _ in range(BOOTSTRAP_SAMPLES):
        indexes: list[int] = []
        for _ in days:
            indexes.extend(by_day[generator.choice(days)])
        baseline = _mean([baseline_losses[index] for index in indexes])
        full = _mean([full_losses[index] for index in indexes])
        improvements += full < baseline
    return improvements / BOOTSTRAP_SAMPLES


def fit_latest(conn: sqlite3.Connection, *, trained_at: int | None = None) -> FitResult:
    """Evaluate the latest data and persist a new model only after validation."""

    rows = _observations(conn)
    sessions = len({row.day for row in rows})
    if len(rows) < MIN_RESULTS or sessions < MIN_SESSIONS:
        return FitResult("collecting", len(rows), sessions)

    split = max(1, min(len(rows) - 1, int(len(rows) * (1.0 - HOLDOUT_FRACTION))))
    train, holdout = rows[:split], rows[split:]
    labels_train = [row.label for row in train]
    labels_holdout = [row.label for row in holdout]

    baseline_means, baseline_scales = _standardize(train, (0,))
    baseline_weights = _fit_logistic(
        _matrix(train, (0,), baseline_means, baseline_scales), labels_train
    )
    baseline_predictions = _probabilities(
        _matrix(holdout, (0,), baseline_means, baseline_scales), baseline_weights
    )

    means, scales = _standardize(train, (0, 1, 2))
    weights = _fit_logistic(_matrix(train, (0, 1, 2), means, scales), labels_train)
    full_predictions = _probabilities(
        _matrix(holdout, (0, 1, 2), means, scales), weights
    )
    baseline_losses = _losses(labels_holdout, baseline_predictions)
    full_losses = _losses(labels_holdout, full_predictions)
    baseline_logloss = _mean(baseline_losses)
    logloss = _mean(full_losses)
    brier = _mean(
        [(prediction - label) ** 2 for prediction, label in zip(full_predictions, labels_holdout)]
    )
    gate_fraction = _bootstrap_fraction(holdout, baseline_losses, full_losses)
    metrics = {
        "logloss": logloss,
        "brier": brier,
        "baseline_logloss": baseline_logloss,
        "bootstrap_improvement_fraction": gate_fraction,
        "n_holdout": float(len(holdout)),
    }
    if gate_fraction < GATE_FRACTION:
        return FitResult(
            "rejected", len(rows), sessions, len(train), len(holdout), gate_fraction, metrics=metrics
        )

    previous = conn.execute(
        "SELECT version, n_train FROM model_state WHERE target=? ORDER BY version DESC LIMIT 1",
        (TARGET,),
    ).fetchone()
    if previous is not None and int(previous[1]) == len(train):
        return FitResult(
            "unchanged", len(rows), sessions, len(train), len(holdout), gate_fraction,
            version=int(previous[0]), metrics=metrics,
        )
    version = 1 if previous is None else int(previous[0]) + 1
    params = {
        "description": "Observed completion under habitual settings; not gauge-conditional.",
        "weights": weights,
        "means": means,
        "scales": scales,
        "feature_names": list(FEATURE_NAMES),
        "metrics": metrics,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO model_state(
              target, version, trained_at, params_json, n_train,
              logloss, brier, baseline_logloss
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                TARGET, version, int(time.time()) if trained_at is None else int(trained_at),
                json.dumps(params, sort_keys=True, separators=(",", ":")), len(train),
                logloss, brier, baseline_logloss,
            ),
        )
    return FitResult(
        "passed", len(rows), sessions, len(train), len(holdout), gate_fraction,
        version=version, metrics=metrics,
    )


def predict_latest(
    conn: sqlite3.Connection, level: float, density: float, scratch: float
) -> float | None:
    """Predict observed completion using the newest model that passed its gate."""

    row = conn.execute(
        "SELECT params_json FROM model_state WHERE target=? ORDER BY version DESC LIMIT 1",
        (TARGET,),
    ).fetchone()
    if row is None:
        return None
    params = json.loads(row[0])
    values = [float(level), float(density), float(scratch)]
    standardized = [
        (value - float(mean)) / float(scale)
        for value, mean, scale in zip(values, params["means"], params["scales"])
    ]
    return _sigmoid(
        float(params["weights"][0])
        + sum(
            float(weight) * value
            for weight, value in zip(params["weights"][1:], standardized)
        )
    )
