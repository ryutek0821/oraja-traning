"""Resolve bmstable entries against locally owned charts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any
import unicodedata

from .fetch import TableEntry


_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MatchedEntry:
    entry: TableEntry
    chart: Any
    matched_by: str


@dataclass(frozen=True, slots=True)
class UnmatchedEntry:
    entry: TableEntry
    reason: str


@dataclass(frozen=True, slots=True)
class TableMatchSummary:
    table_id: str
    total: int
    matched: int
    unmatched: int
    sha256_matches: int
    md5_matches: int
    title_matches: int = 0

    @property
    def match_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class MatchReport:
    """Match results kept separate per source difficulty-table scale."""

    matches: tuple[MatchedEntry, ...]
    unmatched_entries: tuple[UnmatchedEntry, ...]
    tables: tuple[TableMatchSummary, ...]

    @property
    def total(self) -> int:
        return len(self.matches) + len(self.unmatched_entries)

    @property
    def matched(self) -> int:
        return len(self.matches)

    @property
    def unmatched(self) -> int:
        return len(self.unmatched_entries)

    @property
    def match_rate(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def for_table(self, table_id: str) -> TableMatchSummary | None:
        return next((table for table in self.tables if table.table_id == table_id), None)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _hash(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if pattern.fullmatch(normalized) else None


def _title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return normalized or None


def _entry(value: TableEntry | Mapping[str, Any]) -> TableEntry:
    if isinstance(value, TableEntry):
        return value
    table_id = value.get("table_id")
    level = value.get("level")
    if not isinstance(table_id, str) or not table_id:
        raise ValueError("every table entry must have a non-empty table_id")
    if level is None:
        raise ValueError("every table entry must have a level")
    return TableEntry(
        table_id=table_id,
        level=str(level),
        sha256=_hash(value.get("sha256"), _HEX_64),
        md5=_hash(value.get("md5"), _HEX_32),
        title=str(value["title"]) if value.get("title") is not None else None,
        data=dict(value),
    )


def resolve(
    entries: Iterable[TableEntry | Mapping[str, Any]], charts: Iterable[Any]
) -> MatchReport:
    """Match entries to owned charts, trying SHA-256 before MD5.

    Ambiguous local hashes are deliberately not guessed.  Table IDs remain on
    every match and statistics are emitted independently for each scale.
    """

    sha_index: dict[str, list[Any]] = defaultdict(list)
    md5_index: dict[str, list[Any]] = defaultdict(list)
    title_index: dict[str, list[Any]] = defaultdict(list)
    for chart in charts:
        sha256 = _hash(_get(chart, "sha256"), _HEX_64)
        md5 = _hash(_get(chart, "md5"), _HEX_32)
        if sha256 is not None:
            sha_index[sha256].append(chart)
        if md5 is not None:
            md5_index[md5].append(chart)
        title = _title(_get(chart, "title"))
        if title is not None:
            title_index[title].append(chart)

    normalized_entries = tuple(_entry(entry) for entry in entries)
    legacy_title_counts = Counter(
        title
        for entry in normalized_entries
        if entry.data.get("_match") == "unique_title"
        if (title := _title(entry.title)) is not None
    )
    matches: list[MatchedEntry] = []
    misses: list[UnmatchedEntry] = []
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "matched": 0, "sha256": 0, "md5": 0, "title": 0}
    )
    for entry in normalized_entries:
        count = counts[entry.table_id]
        count["total"] += 1
        candidates = sha_index.get(entry.sha256, []) if entry.sha256 else []
        matched_by = "sha256"
        if len(candidates) != 1:
            md5_candidates = md5_index.get(entry.md5, []) if entry.md5 else []
            if len(md5_candidates) == 1:
                candidates = md5_candidates
                matched_by = "md5"
            elif len(candidates) > 1 or len(md5_candidates) > 1:
                misses.append(UnmatchedEntry(entry, "ambiguous_local_hash"))
                continue
            elif entry.data.get("_match") == "unique_title":
                normalized_title = _title(entry.title)
                if (
                    normalized_title is None
                    or legacy_title_counts[normalized_title] != 1
                ):
                    misses.append(UnmatchedEntry(entry, "ambiguous_table_title"))
                    continue
                title_candidates = title_index.get(normalized_title, [])
                if len(title_candidates) == 1:
                    candidates = title_candidates
                    matched_by = "title"
                elif len(title_candidates) > 1:
                    misses.append(UnmatchedEntry(entry, "ambiguous_local_title"))
                    continue
                else:
                    misses.append(UnmatchedEntry(entry, "not_owned"))
                    continue
            else:
                misses.append(UnmatchedEntry(entry, "not_owned"))
                continue
        matches.append(MatchedEntry(entry, candidates[0], matched_by))
        count["matched"] += 1
        count[matched_by] += 1

    summaries = tuple(
        TableMatchSummary(
            table_id=table_id,
            total=count["total"],
            matched=count["matched"],
            unmatched=count["total"] - count["matched"],
            sha256_matches=count["sha256"],
            md5_matches=count["md5"],
            title_matches=count["title"],
        )
        for table_id, count in sorted(counts.items())
    )
    return MatchReport(tuple(matches), tuple(misses), summaries)
