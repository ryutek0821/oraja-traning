"""Protocols implemented by local SQLite and future service adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from .types import (
    ModelSnapshot,
    Observation,
    ProfileContext,
    RecommendationInput,
    RecommendationOutput,
)


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary required by a core operation."""

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class FeatureRepository(Protocol):
    """Read chart information and persist calculated features."""

    def iter_songinfo(self) -> Iterable[Mapping[str, Any]]: ...

    def save_features(self, features: Iterable[Any]) -> int: ...


@runtime_checkable
class ModelRepository(Protocol):
    """Port for model observations and validated snapshots."""

    def iter_observations(self) -> Iterable[Observation]: ...

    def latest_model(self) -> ModelSnapshot | None: ...

    def save_model(self, model: ModelSnapshot) -> None: ...


@runtime_checkable
class RecommendationRepository(Protocol):
    """Port for profile-owned recommendation input and output."""

    def load_input(self, profile: ProfileContext) -> RecommendationInput: ...

    def save_output(self, output: RecommendationOutput) -> None: ...
