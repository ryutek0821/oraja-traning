"""Public database-independent model core.

SQLite compatibility functions are provided by ``db.model_adapter`` and are
exposed lazily below so existing CLI imports continue to work.
"""

from __future__ import annotations

from oraja_training.domain import ModelRepository, UnitOfWork

from .core import (
    FEATURE_NAMES,
    FitResult,
    TARGET,
    fit_from_repository,
    fit_observations,
    fit_repository,
    numeric_level,
    predict_snapshot,
)


def fit_from_port(
    repository: ModelRepository,
    *,
    unit_of_work: UnitOfWork | None = None,
    trained_at: int | None = None,
) -> FitResult:
    """Public core entry point for container/service adapters."""

    return fit_repository(
        repository, unit_of_work=unit_of_work, trained_at=trained_at
    )


def fit_latest(connection: object, *, trained_at: int | None = None) -> FitResult:
    """Compatibility shim for the local SQLite adapter."""

    from oraja_training.db.model_adapter import fit_latest as adapter_fit_latest

    return adapter_fit_latest(connection, trained_at=trained_at)


def predict_latest(connection: object, level: float, density: float, scratch: float) -> float | None:
    """Compatibility shim for the local SQLite adapter."""

    from oraja_training.db.model_adapter import predict_latest as adapter_predict_latest

    return adapter_predict_latest(connection, level, density, scratch)


__all__ = [
    "FEATURE_NAMES",
    "FitResult",
    "TARGET",
    "fit_from_port",
    "fit_from_repository",
    "fit_latest",
    "fit_observations",
    "fit_repository",
    "numeric_level",
    "predict_latest",
    "predict_snapshot",
]
