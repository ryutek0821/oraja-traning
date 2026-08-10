"""Build assistant-owned chart features from a read-only songinfo connection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import astuple, dataclass
import math
import sqlite3
import statistics
from typing import Any

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


def _row_value(row: Mapping[str, Any] | sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def extract_features(
    row: Mapping[str, Any] | sqlite3.Row,
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


_INSERT = """
INSERT INTO chart_features (
  sha256, feature_version, density_mean, density_p90, density_p99,
  end_density, burst_max, scratch_rate, scratch_p90, scratch_combo_rate,
  ln_rate, soflan_var, soflan_changes, stop_count, chart_seconds, total_notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(sha256) DO UPDATE SET
  feature_version=excluded.feature_version,
  density_mean=excluded.density_mean,
  density_p90=excluded.density_p90,
  density_p99=excluded.density_p99,
  end_density=excluded.end_density,
  burst_max=excluded.burst_max,
  scratch_rate=excluded.scratch_rate,
  scratch_p90=excluded.scratch_p90,
  scratch_combo_rate=excluded.scratch_combo_rate,
  ln_rate=excluded.ln_rate,
  soflan_var=excluded.soflan_var,
  soflan_changes=excluded.soflan_changes,
  stop_count=excluded.stop_count,
  chart_seconds=excluded.chart_seconds,
  total_notes=excluded.total_notes
"""


def build_all(
    songinfo_conn: sqlite3.Connection,
    assistant_conn: sqlite3.Connection | None = None,
    *,
    feature_version: int = FEATURE_VERSION,
) -> int:
    """Upsert all valid ``information`` rows and return the written count.

    ``songinfo_conn`` may safely be a ``mode=ro``/``query_only`` connection;
    only ``assistant_conn`` is written.  Passing one connection is supported
    for compact fixtures containing both tables.
    """

    destination = assistant_conn if assistant_conn is not None else songinfo_conn
    old_factory = songinfo_conn.row_factory
    songinfo_conn.row_factory = sqlite3.Row
    try:
        rows = songinfo_conn.execute(
            "SELECT sha256, distribution, speedchange, lanenotes FROM information "
            "ORDER BY sha256"
        )
        written = 0
        for row in rows:
            try:
                features = extract_features(row, feature_version=feature_version)
            except SongInfoDecodeError:
                continue
            destination.execute(_INSERT, astuple(features))
            written += 1
        destination.commit()
        return written
    finally:
        songinfo_conn.row_factory = old_factory
