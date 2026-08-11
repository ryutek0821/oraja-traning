"""Local SQLite adapter for the database-independent feature extractor."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import astuple
import sqlite3
from typing import Any

from oraja_training.features.build import (
    FEATURE_VERSION,
    ChartFeatures,
    build_feature_rows,
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


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    columns = [str(column[0]) for column in cursor.description or ()]
    return dict(zip(columns, row if isinstance(row, tuple) else tuple(row)))


class SQLiteFeatureRepository:
    """Feature port backed by one read connection and one write connection."""

    def __init__(
        self,
        songinfo_conn: sqlite3.Connection,
        assistant_conn: sqlite3.Connection | None = None,
    ) -> None:
        self.songinfo_conn = songinfo_conn
        self.assistant_conn = (
            assistant_conn if assistant_conn is not None else songinfo_conn
        )

    def iter_songinfo(self) -> Iterator[Mapping[str, Any]]:
        cursor = self.songinfo_conn.execute(
            "SELECT sha256, distribution, speedchange, lanenotes FROM information "
            "ORDER BY sha256"
        )
        for row in cursor:
            yield _row_mapping(cursor, row)

    def save_features(self, features: Iterable[ChartFeatures]) -> int:
        rows = [astuple(feature) for feature in features]
        if rows:
            self.assistant_conn.executemany(_INSERT, rows)
        self.assistant_conn.commit()
        return len(rows)


def build_all(
    songinfo_conn: sqlite3.Connection,
    assistant_conn: sqlite3.Connection | None = None,
    *,
    feature_version: int = FEATURE_VERSION,
) -> int:
    """Read SQLite rows, invoke pure extraction, and persist the result."""

    repository = SQLiteFeatureRepository(songinfo_conn, assistant_conn)
    features = build_feature_rows(
        repository.iter_songinfo(), feature_version=feature_version
    )
    return repository.save_features(features)
