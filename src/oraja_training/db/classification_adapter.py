"""SQLite persistence for external chart classifications."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import astuple
from typing import Any

from oraja_training.tables.classification import (
    ClassificationBatch,
    ClassificationSourceSpec,
)


def replace_classification_source(
    conn: sqlite3.Connection, batch: ClassificationBatch
) -> None:
    """Atomically replace one source after its complete batch was validated."""

    table = batch.table
    conn.execute(
        """
        INSERT INTO classification_sources(
          source_id, family, page_url, header_url, data_url, content_digest,
          fetched_at, stale, entry_count, classification_count, matched_count,
          last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
          family=excluded.family,
          page_url=excluded.page_url,
          header_url=excluded.header_url,
          data_url=excluded.data_url,
          content_digest=excluded.content_digest,
          fetched_at=excluded.fetched_at,
          stale=excluded.stale,
          entry_count=excluded.entry_count,
          classification_count=excluded.classification_count,
          matched_count=excluded.matched_count,
          last_error=excluded.last_error
        """,
        (
            batch.source.source_id,
            batch.source.family,
            batch.source.url,
            table.header_url,
            table.data_url,
            batch.content_digest,
            table.fetched_at,
            int(table.stale),
            batch.entry_count,
            batch.classification_count,
            batch.matched_count,
            "stale cache used after refresh failure" if table.stale else None,
        ),
    )
    conn.execute(
        "DELETE FROM chart_classifications WHERE source_id = ?",
        (batch.source.source_id,),
    )
    conn.executemany(
        """
        INSERT INTO chart_classifications(
          source_id, source_key, sha256, md5, local_sha256, family,
          base_scale, base_level, classification_scale, classification_level,
          raw_level, title, match_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (astuple(row) for row in batch.rows),
    )


def record_classification_error(
    conn: sqlite3.Connection,
    source: ClassificationSourceSpec,
    error: str,
) -> None:
    """Record a failed attempt without deleting the last known-good rows."""

    conn.execute(
        """
        INSERT INTO classification_sources(
          source_id, family, page_url, stale, last_error
        ) VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(source_id) DO UPDATE SET
          family=excluded.family,
          page_url=excluded.page_url,
          stale=1,
          last_error=excluded.last_error
        """,
        (source.source_id, source.family, source.url, error),
    )


def load_chart_classifications(
    conn: sqlite3.Connection,
    sha256s: Iterable[str] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Read normalized classifications matched to locally owned charts."""

    parameters: tuple[str, ...] = ()
    where = "WHERE local_sha256 IS NOT NULL"
    if sha256s is not None:
        values = tuple(dict.fromkeys(str(value) for value in sha256s))
        if not values:
            return ()
        where += f" AND local_sha256 IN ({','.join('?' for _ in values)})"
        parameters = values
    cursor = conn.execute(
        f"""
        SELECT local_sha256 AS sha256, source_id, family, base_scale, base_level,
               classification_scale, classification_level, raw_level,
               match_status
          FROM chart_classifications
          {where}
         ORDER BY local_sha256, family, classification_scale, source_id
        """,
        parameters,
    )
    columns = tuple(str(column[0]) for column in cursor.description or ())
    return tuple(dict(zip(columns, row)) for row in cursor.fetchall())
