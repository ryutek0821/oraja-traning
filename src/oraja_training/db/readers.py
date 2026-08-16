"""Read-only readers for beatoraja SQLite databases.

The source databases are deliberately opened through SQLite URI filenames.
Live databases use ``mode=ro`` while static fixtures use ``immutable=1``.
Every connection is additionally marked ``query_only`` as a defence in depth.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from typing import Any, TypeVar


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQLITE_VARIABLE_CHUNK = 900


class ReaderSchemaError(RuntimeError):
    """Raised when a beatoraja table lacks a required column."""


@dataclass(frozen=True, slots=True)
class DynamicRow(Mapping[str, Any]):
    """A schema-tolerant row with both mapping and attribute access."""

    values: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True, slots=True)
class ScoreRow(DynamicRow):
    """One row from ``score`` or ``scoredatalog``."""


@dataclass(frozen=True, slots=True)
class SongRow(DynamicRow):
    """One row from ``songdata.song``."""


@dataclass(frozen=True, slots=True)
class InfoRow(DynamicRow):
    """One row from ``songinfo.information``."""


@dataclass(frozen=True, slots=True)
class ChartPatternRow(DynamicRow):
    """Optional analysis row produced by oraja-constellator."""


@dataclass(frozen=True, slots=True)
class ScoreLogRow(DynamicRow):
    """One schema-version-tolerant row from ``scorelog``."""


@dataclass(frozen=True, slots=True)
class PlayerRow(DynamicRow):
    """One cumulative daily row from ``score.player``."""


RowT = TypeVar("RowT", bound=DynamicRow)


def _sqlite_uri(path: str | Path, query: str) -> str:
    resolved = Path(path).expanduser().resolve(strict=True)
    return f"{resolved.as_uri()}?{query}"


def _configure_read_only(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    timeout = max(0, int(busy_timeout_ms))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {timeout}")


def open_live(
    path: str | Path, *, busy_timeout_ms: int = 1_000
) -> sqlite3.Connection:
    """Open a live beatoraja DB read-only, retaining SQLite locking semantics."""

    conn = sqlite3.connect(
        _sqlite_uri(path, "mode=ro"),
        uri=True,
        timeout=busy_timeout_ms / 1_000,
    )
    _configure_read_only(conn, busy_timeout_ms)
    return conn


def open_private_copy(
    path: str | Path, *, busy_timeout_ms: int = 1_000
) -> sqlite3.Connection:
    """Open an owned live-DB copy, allowing recovery only on that copy."""

    conn = sqlite3.connect(
        _sqlite_uri(path, "mode=rw"),
        uri=True,
        timeout=busy_timeout_ms / 1_000,
    )
    _configure_read_only(conn, busy_timeout_ms)
    return conn


def open_snapshot(
    path: str | Path, *, busy_timeout_ms: int = 1_000
) -> sqlite3.Connection:
    """Open a static DB snapshot without locks; never use this for live DBs."""

    conn = sqlite3.connect(
        _sqlite_uri(path, "mode=ro&immutable=1"),
        uri=True,
        timeout=busy_timeout_ms / 1_000,
    )
    _configure_read_only(conn, busy_timeout_ms)
    return conn


def _quoted_identifier(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _table_column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    quoted = _quoted_identifier(table)
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")]


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the runtime column set for ``table`` using ``PRAGMA table_info``."""

    return set(_table_column_names(conn, table))


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    quoted = _quoted_identifier(table)
    positions = (
        (int(row[5]), str(row[1]))
        for row in conn.execute(f"PRAGMA table_info({quoted})")
        if int(row[5]) > 0
    )
    return tuple(name for _position, name in sorted(positions))


def _require_overwrite_scoredatalog_schema(conn: sqlite3.Connection) -> None:
    expected = ("sha256", "mode")
    actual = _primary_key_columns(conn, "scoredatalog")
    if actual == expected:
        return
    rendered = "<none>" if not actual else f"({', '.join(actual)})"
    raise ReaderSchemaError(
        "unsupported scoredatalog schema: expected overwrite-only PRIMARY KEY "
        f"(sha256, mode), found {rendered}; append-style scoredatalog tables "
        "cannot be collected safely"
    )


def _require_columns(table: str, actual: Iterable[str], required: set[str]) -> None:
    missing = required.difference(actual)
    if missing:
        rendered = ", ".join(sorted(missing))
        raise ReaderSchemaError(f"{table} is missing required columns: {rendered}")


def _select_rows(
    conn: sqlite3.Connection,
    table: str,
    row_type: type[RowT],
    *,
    required: set[str],
    where_sql: str = "",
    parameters: Iterable[Any] = (),
    order_sql: str = "",
) -> list[RowT]:
    columns = _table_column_names(conn, table)
    _require_columns(table, columns, required)
    select_columns = ", ".join(_quoted_identifier(column) for column in columns)
    sql = f"SELECT {select_columns} FROM {_quoted_identifier(table)}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    if order_sql:
        sql += f" ORDER BY {order_sql}"
    return [row_type(dict(row)) for row in conn.execute(sql, tuple(parameters))]


_SCORE_REQUIRED = {
    "sha256",
    "mode",
    "clear",
    "epg",
    "lpg",
    "egr",
    "lgr",
    "egd",
    "lgd",
    "ebd",
    "lbd",
    "epr",
    "lpr",
    "ems",
    "lms",
    "notes",
    "minbp",
    "playcount",
    "date",
}


def read_scoredatalog(conn: sqlite3.Connection) -> list[ScoreRow]:
    """Read the complete latest-play snapshot with runtime column detection."""

    _require_overwrite_scoredatalog_schema(conn)
    return _select_rows(
        conn,
        "scoredatalog",
        ScoreRow,
        required=_SCORE_REQUIRED,
        order_sql='"sha256", "mode"',
    )


def read_score(conn: sqlite3.Connection) -> list[ScoreRow]:
    """Read all aggregate score rows with runtime column detection."""

    return _select_rows(
        conn,
        "score",
        ScoreRow,
        required=_SCORE_REQUIRED,
        order_sql='"sha256", "mode"',
    )


def read_player(conn: sqlite3.Connection) -> list[PlayerRow]:
    """Read cumulative daily player totals used for actual keystroke deltas."""

    return _select_rows(
        conn,
        "player",
        PlayerRow,
        required={
            "date",
            "playcount",
            "playtime",
            "epg",
            "lpg",
            "egr",
            "lgr",
            "egd",
            "lgd",
            "ebd",
            "lbd",
            "epr",
            "lpr",
            "ems",
            "lms",
        },
        order_sql='"date"',
    )


def read_scorelog(conn: sqlite3.Connection) -> list[ScoreLogRow]:
    """Read score updates without assuming optional judge columns exist."""

    return _select_rows(
        conn,
        "scorelog",
        ScoreLogRow,
        required={
            "sha256",
            "mode",
            "clear",
            "oldclear",
            "score",
            "oldscore",
            "combo",
            "oldcombo",
            "minbp",
            "oldminbp",
            "date",
        },
        order_sql='"date", "sha256", "mode"',
    )


def _read_by_hashes(
    conn: sqlite3.Connection,
    table: str,
    row_type: type[RowT],
    sha256s: Iterable[str] | None,
    required: set[str],
) -> list[RowT]:
    if sha256s is None:
        return _select_rows(
            conn,
            table,
            row_type,
            required=required,
            order_sql='"sha256", "path"' if table == "song" else '"sha256"',
        )

    hashes = list(dict.fromkeys(sha256s))
    if not hashes:
        return []

    rows: list[RowT] = []
    for offset in range(0, len(hashes), _SQLITE_VARIABLE_CHUNK):
        chunk = hashes[offset : offset + _SQLITE_VARIABLE_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            _select_rows(
                conn,
                table,
                row_type,
                required=required,
                where_sql=f'"sha256" IN ({placeholders})',
                parameters=chunk,
                order_sql='"sha256", "path"'
                if table == "song"
                else '"sha256"',
            )
        )
    return rows


def read_songs(
    conn: sqlite3.Connection, sha256s: Iterable[str] | None = None
) -> list[SongRow]:
    """Read selected songs, or all songs when ``sha256s`` is ``None``."""

    return _read_by_hashes(
        conn,
        "song",
        SongRow,
        sha256s,
        {"sha256", "md5", "title", "artist", "notes", "mode", "path"},
    )


def read_songinfo(
    conn: sqlite3.Connection, sha256s: Iterable[str] | None = None
) -> list[InfoRow]:
    """Read selected song information rows with schema detection."""

    return _read_by_hashes(
        conn,
        "information",
        InfoRow,
        sha256s,
        {"sha256"},
    )


def read_chart_patterns(
    conn: sqlite3.Connection, sha256s: Iterable[str] | None = None
) -> list[ChartPatternRow]:
    """Read optional high-resolution pattern analysis without requiring it."""

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bmscf_chart_analysis'"
    ).fetchone()
    if exists is None:
        return []
    return _read_by_hashes(
        conn,
        "bmscf_chart_analysis",
        ChartPatternRow,
        sha256s,
        {"sha256"},
    )
