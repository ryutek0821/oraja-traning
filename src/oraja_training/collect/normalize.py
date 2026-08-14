"""Canonical payload hashing and play-derived values."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from typing import Any

from oraja_training.domain import Play


JUDGEMENT_COLUMNS = (
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
)

EMPTY_POOR_COLUMNS = ("ems", "lms")

CREDITED_GAUGE_BY_CLEAR = {
    4: "EASY",
    5: "NORMAL",
    6: "HARD",
    7: "EXHARD",
}


def _canonical_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan"
        elif math.isinf(value):
            rendered = "inf" if value > 0 else "-inf"
        elif value == 0:
            rendered = "0"
        else:
            rendered = value.hex()
        return ["float", rendered]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    return [type(value).__name__, repr(value)]


def canonical_payload(row: Mapping[str, Any]) -> str:
    """Return a deterministic, type-preserving string for every row column."""

    normalized = [
        [str(column), _canonical_value(row[column])] for column in sorted(row)
    ]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def payload_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(row).encode("utf-8")).hexdigest()


def ex_score(row: Mapping[str, Any]) -> int | None:
    required = ("epg", "lpg", "egr", "lgr")
    if any(row.get(column) is None for column in required):
        return None
    return (
        (int(row["epg"]) + int(row["lpg"])) * 2
        + int(row["egr"])
        + int(row["lgr"])
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def normalize_play(
    row: Mapping[str, Any],
    *,
    source: str,
    source_generation: int,
    aggregate_row: Mapping[str, Any] | None = None,
    lost_events: int = 0,
    ingested_at: int | None = None,
) -> Play:
    """Convert one score-like mapping into a storage-neutral :class:`Play`.

    The input is intentionally typed as ``Mapping`` rather than a SQLite row.
    Readers and remote ingestion adapters can therefore share this conversion
    without importing a database driver into the collection core.
    """

    clear = int(row["clear"])
    notes = _optional_int(row.get("notes"))
    minbp = _optional_int(row.get("minbp"))
    if source == "ir_import":
        judged = None
        empty_poor = None
        survival = None
        completed = None
    else:
        judged = sum(int(row.get(column) or 0) for column in JUDGEMENT_COLUMNS)
        empty_poor = sum(
            int(row.get(column) or 0) for column in EMPTY_POOR_COLUMNS
        )
        survival = judged / notes if notes is not None and notes > 0 else None
        completed = int(survival >= 0.95) if survival is not None else None
    bp_rate = minbp / notes if minbp is not None and notes is not None and notes > 0 else None

    # ``clear`` is a result lamp, not the selected/start gauge.  Only lamps
    # 4..7 identify a credited result gauge; assist lamps 2/3 are ambiguous.
    credited_gauge_kind = CREDITED_GAUGE_BY_CLEAR.get(clear)

    current_ex = ex_score(row)
    aggregate_ex = ex_score(aggregate_row) if aggregate_row is not None else None
    exceeded = int(
        current_ex is not None
        and aggregate_ex is not None
        and current_ex > aggregate_ex
    )

    return Play(
        sha256=str(row["sha256"]),
        mode=int(row["mode"]),
        played_at=int(row["date"]),
        playcount=int(row["playcount"]),
        clear=clear,
        notes=notes,
        completed=completed,
        source=source,
        is_course=(
            source in {"collector", "legacy_last_snapshot", "daily_snapshot"}
            and clear == 0
        ),
        source_generation=int(source_generation),
        ex=current_ex,
        minbp=minbp,
        judged=judged,
        empty_poor=empty_poor,
        survival=survival,
        bp_rate=bp_rate,
        credited_gauge_kind=credited_gauge_kind,
        selected_gauge_kind=None,
        option=_optional_int(row.get("option")),
        seed=_optional_int(row.get("seed")),
        random=_optional_int(row.get("random")),
        trophy=None if row.get("trophy") is None else str(row["trophy"]),
        exceeded_aggregate_score=bool(exceeded),
        lost_events=max(0, int(lost_events)),
        payload_hash=payload_hash(row),
        ingested_at=int(time.time()) if ingested_at is None else int(ingested_at),
    )


def derive_play(
    row: Mapping[str, Any],
    *,
    source: str,
    source_generation: int,
    aggregate_row: Mapping[str, Any] | None = None,
    lost_events: int = 0,
    ingested_at: int | None = None,
) -> dict[str, Any]:
    """Compatibility mapping for the local SQLite adapter and old callers."""

    return normalize_play(
        row,
        source=source,
        source_generation=source_generation,
        aggregate_row=aggregate_row,
        lost_events=lost_events,
        ingested_at=ingested_at,
    ).as_record()
