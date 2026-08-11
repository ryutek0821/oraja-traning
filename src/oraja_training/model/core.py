"""Database-independent model fitting and prediction."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import math
import random
import time

from oraja_training.domain import ModelRepository, UnitOfWork
from oraja_training.domain.types import ModelSnapshot, Observation


TARGET = "observed_completion"
FEATURE_NAMES = ("level", "density_p99", "scratch_rate")
MIN_RESULTS = 200
MIN_SESSIONS = 10
HOLDOUT_FRACTION = 0.20
BOOTSTRAP_SAMPLES = 100
GATE_FRACTION = 0.80


@dataclass(frozen=True)
class FitResult:
    """Outcome of a fit attempt; only passed outcomes produce a new snapshot."""

    status: str
    n_results: int
    n_sessions: int
    n_train: int = 0
    n_holdout: int = 0
    gate_fraction: float | None = None
    version: int | None = None
    metrics: dict[str, float] | None = None
    model: ModelSnapshot | None = None


def numeric_level(value: object) -> float | None:
    text = str(value).strip()
    try:
        result = float(text)
    except (TypeError, ValueError):
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


def _standardize(
    train: Sequence[Observation], feature_indexes: Sequence[int]
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
    rows: Sequence[Observation],
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
        probabilities = [
            _sigmoid(sum(w * value for w, value in zip(weights, row)))
            for row in x
        ]
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
        result.append(
            -(label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability))
        )
    return result


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _bootstrap_fraction(
    rows: Sequence[Observation],
    baseline_losses: Sequence[float],
    full_losses: Sequence[float],
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


def fit_observations(
    observations: Iterable[Observation],
    *,
    previous: ModelSnapshot | None = None,
    trained_at: int | None = None,
) -> FitResult:
    """Fit from adapter-provided observations without opening a database."""

    rows = list(observations)
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
            "rejected", len(rows), sessions, len(train), len(holdout), gate_fraction,
            metrics=metrics,
        )

    version = 1 if previous is None else previous.version + 1
    if previous is not None and previous.n_train == len(train):
        return FitResult(
            "unchanged", len(rows), sessions, len(train), len(holdout), gate_fraction,
            version=previous.version, metrics=metrics, model=previous,
        )

    snapshot = ModelSnapshot(
        target=TARGET,
        version=version,
        trained_at=int(time.time()) if trained_at is None else int(trained_at),
        n_train=len(train),
        weights=tuple(weights),
        means=tuple(means),
        scales=tuple(scales),
        feature_names=FEATURE_NAMES,
        metrics=metrics,
        description="Observed completion under habitual settings; not gauge-conditional.",
    )
    return FitResult(
        "passed", len(rows), sessions, len(train), len(holdout), gate_fraction,
        version=version, metrics=metrics, model=snapshot,
    )


def fit_repository(
    repository: ModelRepository,
    *,
    unit_of_work: UnitOfWork | None = None,
    trained_at: int | None = None,
) -> FitResult:
    """Fit and persist through ports without knowing the storage backend."""

    result = fit_observations(
        repository.iter_observations(),
        previous=repository.latest_model(),
        trained_at=trained_at,
    )
    if result.status != "passed" or result.model is None:
        return result

    if unit_of_work is None:
        repository.save_model(result.model)
        return result

    try:
        repository.save_model(result.model)
        unit_of_work.commit()
    except Exception:
        unit_of_work.rollback()
        raise
    return result


# Explicit alias for adapters that prefer to name the boundary by its source.
fit_from_repository = fit_repository


def predict_snapshot(
    snapshot: ModelSnapshot | None,
    level: float,
    density: float,
    scratch: float,
) -> float | None:
    """Predict from an immutable snapshot, or return ``None`` at cold start."""

    if snapshot is None:
        return None
    values = [float(level), float(density), float(scratch)]
    standardized = [
        (value - mean) / scale
        for value, mean, scale in zip(values, snapshot.means, snapshot.scales)
    ]
    return _sigmoid(
        snapshot.weights[0]
        + sum(weight * value for weight, value in zip(snapshot.weights[1:], standardized))
    )
