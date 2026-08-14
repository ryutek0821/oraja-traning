"""Build assistant-owned chart features from a read-only songinfo connection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
import statistics
from typing import Any

from oraja_training.domain import FeatureRepository

from .songinfo import (
    SongInfoDecodeError,
    decode_distribution,
    decode_lanenotes,
    decode_speedchange,
)


FEATURE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ChartFeatures:
    sha256: str
    feature_version: int
    density_mean: float
    density_p90: float
    density_p99: float
    end_density: float
    burst_max: float
    scratch_rate: float
    scratch_p90: float
    scratch_combo_rate: float
    ln_rate: float
    soflan_var: float
    soflan_changes: int
    stop_count: int
    chart_seconds: float
    total_notes: int


def _nearest_rank(values: Sequence[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def extract_features(
    row: Mapping[str, Any],
    *,
    feature_version: int = FEATURE_VERSION,
) -> ChartFeatures:
    """Extract one deterministic feature row from songinfo ``information``."""

    sha256 = str(_row_value(row, "sha256") or "").lower()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise SongInfoDecodeError("information row has an invalid sha256")

    distribution = decode_distribution(_row_value(row, "distribution"))
    speeds = decode_speedchange(_row_value(row, "speedchange"))
    lanes = decode_lanenotes(_row_value(row, "lanenotes"))

    # LN starts are stored separately from normal notes.  Both are physical
    # chart notes and must contribute to density and total-notes features.
    densities = [bucket[0] + bucket[2] + bucket[3] + bucket[5] for bucket in distribution]
    scratches = [bucket[0] + bucket[2] for bucket in distribution]
    distributed_notes = sum(densities)
    scratch_notes = sum(scratches)
    active_scratch = sum(scratch > 0 for scratch in scratches)
    scratch_combo = sum(
        scratch > 0 and (bucket[3] + bucket[5]) > 0
        for scratch, bucket in zip(scratches, distribution, strict=True)
    )

    # Ignore only trailing padding when calculating the end window. Internal
    # rests are meaningful density zeroes and remain part of every statistic.
    last_note = next(
        (index for index in range(len(densities) - 1, -1, -1) if densities[index]),
        -1,
    )
    end_window = densities[max(0, last_note - 4) : last_note + 1]

    normal_notes = sum(lane[0] for lane in lanes)
    long_notes = sum(lane[1] for lane in lanes)
    playable_lane_notes = normal_notes + long_notes
    # Per-second pairs are capped at base36's two-character maximum.  The
    # per-lane totals remain exact for pathological ultra-dense charts.
    total_notes = playable_lane_notes or distributed_notes

    speed_values = [speed for speed, _ in speeds]
    changes = sum(
        not math.isclose(previous, current, rel_tol=0.0, abs_tol=1e-12)
        for previous, current in zip(speed_values, speed_values[1:])
    )
    stop_count = sum(math.isclose(speed, 0.0, abs_tol=1e-12) for speed in speed_values)
    chart_seconds = (
        speeds[-1][1] / 1000.0 if speeds and speeds[-1][1] > 0 else float(len(distribution))
    )

    return ChartFeatures(
        sha256=sha256,
        feature_version=int(feature_version),
        density_mean=(sum(densities) / len(densities) if densities else 0.0),
        density_p90=_nearest_rank(densities, 0.90),
        density_p99=_nearest_rank(densities, 0.99),
        end_density=(sum(end_window) / len(end_window) if end_window else 0.0),
        burst_max=float(max(densities, default=0)),
        scratch_rate=(scratch_notes / total_notes if total_notes else 0.0),
        scratch_p90=_nearest_rank(scratches, 0.90),
        scratch_combo_rate=(scratch_combo / active_scratch if active_scratch else 0.0),
        ln_rate=(long_notes / playable_lane_notes if playable_lane_notes else 0.0),
        soflan_var=(statistics.pvariance(speed_values) if speed_values else 0.0),
        soflan_changes=changes,
        stop_count=stop_count,
        chart_seconds=chart_seconds,
        total_notes=total_notes,
    )


def build_feature_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    feature_version: int = FEATURE_VERSION,
) -> tuple[ChartFeatures, ...]:
    """Calculate feature values without reading from or writing to storage.

    Invalid SongInformation rows are skipped for compatibility with the
    original bulk import.  A remote adapter can use the returned immutable
    values and decide independently how to report or persist skipped rows.
    """

    result: list[ChartFeatures] = []
    for row in rows:
        try:
            result.append(extract_features(row, feature_version=feature_version))
        except SongInfoDecodeError:
            continue
    return tuple(result)


def build_from_repository(
    repository: FeatureRepository,
    *,
    feature_version: int = FEATURE_VERSION,
) -> int:
    """Run the pure feature calculation through a repository port."""

    rows = repository.iter_songinfo()
    features = build_feature_rows(rows, feature_version=feature_version)
    return int(repository.save_features(features))


def build_all(
    songinfo_source: object,
    assistant_source: object | None = None,
    *,
    feature_version: int = FEATURE_VERSION,
) -> int:
    """Compatibility shim; SQLite I/O lives in ``db.feature_adapter``."""

    from oraja_training.db.feature_adapter import build_all as adapter_build_all

    return adapter_build_all(
        songinfo_source,
        assistant_source,
        feature_version=feature_version,
    )
