"""Normalize curated chart-pattern tables without treating them as difficulty scales."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .fetch import TableData, TableEntry
from .match import resolve

_BASE_LEVEL = re.compile(r"^(sl|st)(\d+)$")
_CLASSIFICATION_LEVEL = re.compile(r"^(.+?)(-?\d+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")


class ClassificationError(ValueError):
    """Raised when an external classification label is not understood safely."""


@dataclass(frozen=True, slots=True)
class ClassificationSourceSpec:
    """One explicitly supported external classification source."""

    source_id: str
    family: str
    url: str
    scales: tuple[str, ...]


DEFAULT_CLASSIFICATION_SOURCES = (
    ClassificationSourceSpec(
        "slst-code-stream",
        "code_stream",
        "https://djhemorrhoid.github.io/tables/slst-code-stream/header.json",
        ("乱打", "重発狂"),
    ),
    ClassificationSourceSpec(
        "slst-mini-jack",
        "mini_jack",
        "https://djhemorrhoid.github.io/tables/slst-mini-jack/header.json",
        ("微縦連", "連打複合"),
    ),
    ClassificationSourceSpec(
        "slst-arm",
        "arm",
        "https://djhemorrhoid.github.io/tables/slst-arm/header.json",
        ("Ude", "腕"),
    ),
    ClassificationSourceSpec(
        "slst-delay",
        "delay",
        "https://djhemorrhoid.github.io/tables/slst-delay/header.json",
        ("///", "dl"),
    ),
)


@dataclass(frozen=True, slots=True)
class ChartClassification:
    """One technique-scale membership from an upstream chart row."""

    source_id: str
    source_key: str
    sha256: str | None
    md5: str | None
    local_sha256: str | None
    family: str
    base_scale: str
    base_level: int
    classification_scale: str
    classification_level: int
    raw_level: str
    title: str | None
    match_status: str


@dataclass(frozen=True, slots=True)
class ClassificationBatch:
    """Validated source metadata and normalized rows for one atomic refresh."""

    source: ClassificationSourceSpec
    table: TableData
    content_digest: str
    entry_count: int
    classification_count: int
    matched_count: int
    rows: tuple[ChartClassification, ...]


def _source_key(entry: TableEntry) -> str:
    if entry.sha256 is not None:
        return f"sha256:{entry.sha256}"
    if entry.md5 is not None:
        return f"md5:{entry.md5}"
    raise ClassificationError("classification entry has no hash")


def parse_classification_level(
    raw_level: str, source: ClassificationSourceSpec
) -> tuple[str, int, tuple[tuple[str, int], ...]]:
    parts = tuple(part.strip() for part in raw_level.split(","))
    if len(parts) < 2 or any(not part for part in parts):
        raise ClassificationError(
            f"{source.source_id} level {raw_level!r} is not a composite classification"
        )
    base = _BASE_LEVEL.fullmatch(parts[0])
    if base is None:
        raise ClassificationError(
            f"{source.source_id} level {raw_level!r} has no supported sl/st base"
        )
    classifications: list[tuple[str, int]] = []
    seen_scales: set[str] = set()
    for value in parts[1:]:
        match = _CLASSIFICATION_LEVEL.fullmatch(value)
        if match is None or match.group(1) not in source.scales:
            raise ClassificationError(
                f"{source.source_id} level {raw_level!r} has unsupported classification {value!r}"
            )
        scale = match.group(1)
        if scale in seen_scales:
            raise ClassificationError(
                f"{source.source_id} level {raw_level!r} repeats scale {scale!r}"
            )
        seen_scales.add(scale)
        classifications.append((scale, int(match.group(2))))
    return base.group(1), int(base.group(2)), tuple(classifications)


def _validate_hash(
    entry: TableEntry,
    name: str,
    normalized: str | None,
    pattern: re.Pattern[str],
) -> None:
    raw = entry.data.get(name)
    if raw is None or raw == "":
        return
    if (
        not isinstance(raw, str)
        or pattern.fullmatch(raw.strip().lower()) is None
        or normalized != raw.strip().lower()
    ):
        raise ClassificationError(
            f"{entry.table_id} entry has invalid {name} {raw!r}"
        )


def _content_digest(table: TableData) -> str:
    payload = {
        "header": dict(table.header),
        "entries": [dict(entry.data) for entry in table.entries],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_classification_batch(
    source: ClassificationSourceSpec,
    table: TableData,
    charts: Iterable[Mapping[str, Any]],
) -> ClassificationBatch:
    """Validate, structure, and match one external classification table."""

    chart_rows = tuple(charts)
    report = resolve(table.entries, chart_rows)
    matched = {
        id(value.entry): (str(value.chart["sha256"]), value.matched_by)
        for value in report.matches
    }
    missed = {id(value.entry): value.reason for value in report.unmatched_entries}
    rows: list[ChartClassification] = []
    seen_sha256: set[str] = set()
    seen_md5: set[str] = set()
    for entry in table.entries:
        _validate_hash(entry, "sha256", entry.sha256, _SHA256)
        _validate_hash(entry, "md5", entry.md5, _MD5)
        for name, value, seen in (
            ("sha256", entry.sha256, seen_sha256),
            ("md5", entry.md5, seen_md5),
        ):
            if value is None:
                continue
            if value in seen:
                raise ClassificationError(
                    f"{source.source_id} repeats {name} {value}"
                )
            seen.add(value)
        base_scale, base_level, classifications = parse_classification_level(
            entry.level, source
        )
        local_sha256, match_status = matched.get(
            id(entry), (None, missed.get(id(entry), "not_owned"))
        )
        for scale, level in classifications:
            rows.append(
                ChartClassification(
                    source_id=source.source_id,
                    source_key=_source_key(entry),
                    sha256=entry.sha256,
                    md5=entry.md5,
                    local_sha256=local_sha256,
                    family=source.family,
                    base_scale=base_scale,
                    base_level=base_level,
                    classification_scale=scale,
                    classification_level=level,
                    raw_level=entry.level,
                    title=entry.title,
                    match_status=match_status,
                )
            )
    return ClassificationBatch(
        source=source,
        table=table,
        content_digest=_content_digest(table),
        entry_count=len(table.entries),
        classification_count=len(rows),
        matched_count=report.matched,
        rows=tuple(rows),
    )
