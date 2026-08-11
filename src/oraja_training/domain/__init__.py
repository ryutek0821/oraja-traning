"""Database-independent domain contracts for collection and recommendation."""

from .errors import (
    CoreContractError,
    DomainError,
    DomainValidationError,
    RepositoryError,
)
from .ports import FeatureRepository, ModelRepository, RecommendationRepository, UnitOfWork
from .types import (
    Chart,
    ModelSnapshot,
    Observation,
    Play,
    ProfileContext,
    ProfileSettings,
    RecommendationInput,
    RecommendationOutput,
)

__all__ = [
    "Chart",
    "CoreContractError",
    "DomainError",
    "DomainValidationError",
    "FeatureRepository",
    "ModelRepository",
    "Observation",
    "Play",
    "ProfileContext",
    "ProfileSettings",
    "RecommendationInput",
    "RecommendationOutput",
    "RecommendationRepository",
    "RepositoryError",
    "ModelSnapshot",
    "UnitOfWork",
]
