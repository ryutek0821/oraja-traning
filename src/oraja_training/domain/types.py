"""Small immutable values exchanged between core algorithms and adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Mapping
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DomainValidationError


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    """Profile-owned settings used when a daily table is generated."""

    timezone: str = "Asia/Tokyo"
    target_judged: int = 100_000
    reserve_judged: int = 10_000
    readiness: str = "normal"

    def __post_init__(self) -> None:
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise DomainValidationError("timezone must be a non-empty IANA name")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainValidationError("timezone must be a valid IANA name") from exc
        if self.target_judged <= 0:
            raise DomainValidationError("target_judged must be positive")
        if self.reserve_judged < 0:
            raise DomainValidationError("reserve_judged must not be negative")
        if self.readiness not in {"normal", "tired"}:
            raise DomainValidationError("readiness must be 'normal' or 'tired'")


@dataclass(frozen=True, slots=True)
class ProfileContext:
    """Identity and deterministic namespace for one recommendation profile."""

    profile_id: str = "local-profile"
    display_name: str = "Personal"
    timezone: str = "Asia/Tokyo"
    seed_namespace: str | None = None
    target_judged: int = 100_000
    reserve_judged: int = 10_000
    readiness: str = "normal"
    settings: ProfileSettings | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "display_name", "timezone"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"{field_name} must be a non-empty string")
        if self.seed_namespace is not None and (
            not isinstance(self.seed_namespace, str) or not self.seed_namespace.strip()
        ):
            raise DomainValidationError("seed_namespace must be a non-empty string")
        profile_settings = self.settings or ProfileSettings(
            timezone=self.timezone,
            target_judged=self.target_judged,
            reserve_judged=self.reserve_judged,
            readiness=self.readiness,
        )
        object.__setattr__(self, "timezone", profile_settings.timezone)
        object.__setattr__(self, "target_judged", profile_settings.target_judged)
        object.__setattr__(self, "reserve_judged", profile_settings.reserve_judged)
        object.__setattr__(self, "readiness", profile_settings.readiness)
        object.__setattr__(self, "settings", profile_settings)

    @property
    def deterministic_namespace(self) -> str:
        return self.seed_namespace or self.profile_id


@dataclass(frozen=True, slots=True)
class Play:
    """Normalized play value; it contains no SQLite row or connection."""

    sha256: str
    mode: int
    played_at: int
    playcount: int
    clear: int
    notes: int | None
    completed: int | None
    source: str
    is_course: bool
    source_generation: int = 0
    ex: int | None = None
    minbp: int | None = None
    judged: int | None = None
    empty_poor: int | None = None
    survival: float | None = None
    bp_rate: float | None = None
    credited_gauge_kind: str | None = None
    selected_gauge_kind: str | None = None
    option: int | None = None
    seed: int | None = None
    random: int | None = None
    trophy: str | None = None
    exceeded_aggregate_score: bool = False
    lost_events: int = 0
    payload_hash: str = ""
    ingested_at: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not self.sha256.strip():
            raise DomainValidationError("play.sha256 must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise DomainValidationError("play.source must be a non-empty string")
        # -1 is the IR-import namespace and -2 is the daily-snapshot
        # namespace; both are valid non-poller generations.
        if self.playcount < 0 or self.source_generation < -2 or self.lost_events < 0:
            raise DomainValidationError("play counters must not be negative")

    def as_record(self) -> dict[str, Any]:
        """Return a plain mapping suitable for an adapter-owned insert."""

        record = asdict(self)
        record["is_course"] = int(self.is_course)
        record["exceeded_aggregate_score"] = int(self.exceeded_aggregate_score)
        return record


@dataclass(frozen=True, slots=True)
class Chart:
    """Chart metadata used by core algorithms."""

    sha256: str
    md5: str | None
    title: str
    artist: str
    notes: int
    song_mode: int
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not self.sha256.strip():
            raise DomainValidationError("chart.sha256 must be a non-empty string")
        if self.notes < 0:
            raise DomainValidationError("chart.notes must not be negative")


@dataclass(frozen=True, slots=True)
class Observation:
    """One model observation after adapter-side row selection."""

    played_at: int
    day: int
    features: tuple[float, float, float]
    label: float

    def __post_init__(self) -> None:
        if len(self.features) != 3:
            raise DomainValidationError("observation.features must contain three values")
        if not all(math.isfinite(float(value)) for value in self.features):
            raise DomainValidationError("observation.features must be finite")
        if not 0.0 <= float(self.label) <= 1.0:
            raise DomainValidationError("observation.label must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    """Validated model parameters, independent of the storage format."""

    target: str
    version: int
    trained_at: int
    n_train: int
    weights: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    feature_names: tuple[str, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target.strip():
            raise DomainValidationError("model.target must be a non-empty string")
        if self.version < 0 or self.n_train < 0:
            raise DomainValidationError("model version and n_train must not be negative")
        width = len(self.feature_names)
        if width == 0 or len(self.weights) != width + 1:
            raise DomainValidationError(
                "model.weights must contain an intercept and one value per feature"
            )
        if len(self.means) != width or len(self.scales) != width:
            raise DomainValidationError("model normalization vectors must match features")
        if any(not math.isfinite(float(value)) for value in self.weights):
            raise DomainValidationError("model.weights must be finite")
        if any(not math.isfinite(float(value)) for value in self.means):
            raise DomainValidationError("model.means must be finite")
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in self.scales):
            raise DomainValidationError("model.scales must be finite and positive")


@dataclass(frozen=True, slots=True)
class RecommendationInput:
    """All persisted values needed to build a deterministic recommendation."""

    profile: ProfileContext
    import_id: int
    baseline_judged: int
    candidates: tuple[Mapping[str, Any], ...]
    model: ModelSnapshot | None = None
    table_sources: tuple[Mapping[str, Any], ...] = ()
    warmup_adjustment: int = 0

    def __post_init__(self) -> None:
        if self.import_id < 0 or self.baseline_judged < 0:
            raise DomainValidationError("recommendation input counters must not be negative")
        if self.warmup_adjustment not in {-1, 0, 1}:
            raise DomainValidationError("warmup_adjustment must be -1, 0, or 1")


@dataclass(frozen=True, slots=True)
class RecommendationOutput:
    """Storage-neutral recommendation result for a future remote adapter."""

    profile: ProfileContext
    menu_date: str
    seed: str
    queue: tuple[Mapping[str, Any], ...]
    personal: tuple[Mapping[str, Any], ...]
    import_id: int = 0
    baseline_judged: int = 0
    model_version: int = 0
    readiness: str = "normal"
    generated_at: int = 0
    target_judged: int = 0
    reserve_target: int = 0
    core_expected_judged: int = 0
    reserve_expected_judged: int = 0
    weakness_axes: tuple[str, ...] = ()
    model_status: str = "cold_start"
    table_frontiers: tuple[Mapping[str, Any], ...] = ()
    table_warnings: tuple[str, ...] = ()
    warmup_adjustment: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.menu_date, str)
            or not isinstance(self.seed, str)
            or not self.menu_date.strip()
            or not self.seed.strip()
        ):
            raise DomainValidationError("recommendation output date and seed are required")
        if (
            self.import_id < 0
            or self.baseline_judged < 0
            or self.model_version < 0
            or self.target_judged < 0
            or self.reserve_target < 0
            or self.core_expected_judged < 0
            or self.reserve_expected_judged < 0
        ):
            raise DomainValidationError("recommendation output counters must not be negative")
        if self.warmup_adjustment not in {-1, 0, 1}:
            raise DomainValidationError("warmup_adjustment must be -1, 0, or 1")
