"""Deterministic cold-start recommendation and 100k-judgement planning."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from oraja_training.domain import DomainError, RecommendationRepository
from oraja_training.domain.types import (
    ModelSnapshot,
    ProfileContext,
    ProfileSettings,
    RecommendationInput,
    RecommendationOutput,
)
from oraja_training.model.core import (
    FEATURE_NAMES as MODEL_FEATURE_NAMES,
    fit_difficulty_frontier,
    numeric_level,
    predict_snapshot,
)


TARGET_JUDGED = 100_000
RESERVE_JUDGED = 10_000
LEVEL_ORDER = (
    "01 WARMUP",
    "02 FOCUS-A",
    "03 TRANSFER-A",
    "04 FOCUS-B",
    "05 TRANSFER-B",
    "06 LAMP",
    "07 REVIEW",
    "08 PROBE",
    "09 RESERVE",
)
QUOTAS = {
    "01 WARMUP": 8_000,
    "02 FOCUS-A": 21_000,
    "03 TRANSFER-A": 10_000,
    "04 FOCUS-B": 21_000,
    "05 TRANSFER-B": 10_000,
    "06 LAMP": 15_000,
    "07 REVIEW": 10_000,
    "08 PROBE": 5_000,
}
FEATURE_AXES = ("density", "scratch", "ln", "soflan")
DIFFICULTY_TABLES = ("genocide", "overjoy", "satellite", "stella")
WARMUP_FEATURES = (
    "density_p90",
    "end_density",
    "burst_max",
    "scratch_rate",
    "scratch_combo_rate",
    "ln_rate",
    "soflan_var",
    "soflan_changes",
    "stop_count",
    "micro_rate",
    "long_jack_rate",
    "avg_chord",
    "chord_ge3",
)


class MenuBuildError(DomainError):
    """Raised when owned table coverage is insufficient to build a menu."""


class RevisionConflictError(MenuBuildError):
    """Raised when an immutable revision number is reused for other content."""


@dataclass(frozen=True, slots=True)
class DifficultyRating:
    table_id: str
    source_level: str
    level_number: float | None


@dataclass(frozen=True, slots=True)
class TableFrontier:
    table_id: str
    easy: float
    normal: float
    hard: float
    warmup_anchor: float
    observations: int
    hard_clears: int


@dataclass(frozen=True, slots=True)
class Candidate:
    sha256: str
    md5: str | None
    title: str
    artist: str
    notes: int
    table_id: str
    source_level: str
    level_number: float | None
    ratings: tuple[DifficultyRating, ...]
    clear: int
    playcount: int
    last_played: int
    minbp: int | None
    recent_successes: int
    recent_failures: int
    recent_played_at: int
    recent_bp_rate: float | None
    p_complete: float
    tier: str
    target: str
    weakness: float
    primary_axis: str
    high_load: float
    feature_scores: dict[str, float]
    warmup_features: dict[str, float | None]
    warmup_feature_scores: dict[str, float]
    warmup_load: float
    chart_seconds: float | None
    practice_low: bool | None


@dataclass(frozen=True, slots=True)
class MenuItem:
    sequence: int
    sha256: str
    md5: str | None
    title: str
    artist: str
    notes: int
    expected_judged: int
    category: str
    table_id: str
    source_level: str
    band: str
    p_complete: float
    target: str
    reason: str
    attempt: int = 1
    optional: bool = False
    is_exploration: bool = False


@dataclass(frozen=True, slots=True)
class Session:
    menu_date: str
    generated_at: int
    seed: str
    readiness: str
    target_judged: int
    reserve_target: int
    core_expected_judged: int
    reserve_expected_judged: int
    weakness_axes: tuple[str, str]
    baseline_judged: int
    import_id: int
    model_version: int
    model_status: str
    table_frontiers: tuple[TableFrontier, ...]
    table_warnings: tuple[str, ...]
    warmup_adjustment: int
    queue: tuple[MenuItem, ...]
    personal: tuple[Candidate, ...]
    profile: ProfileContext = field(default_factory=ProfileContext)


@dataclass(frozen=True, slots=True)
class ArtifactRevision:
    """Immutable pair of table artifacts and its compare-and-set candidate."""

    revision: int
    content_hash: str
    root: Path
    published: bool


def _as_local_datetime(moment: float | datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if isinstance(moment, datetime):
        aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
        return aware.astimezone(zone)
    return datetime.fromtimestamp(float(moment), tz=zone)


def training_day(moment: float | datetime, timezone_name: str) -> date:
    """Return the profile's logical day, changing at local 04:00."""

    local = _as_local_datetime(moment, timezone_name)
    boundary = local.replace(hour=4, minute=0, second=0, microsecond=0)
    return local.date() if local >= boundary else local.date() - timedelta(days=1)


def next_training_date(moment: float | datetime, profile: ProfileContext) -> date:
    """Return the next logical training date using the profile's IANA zone.

    Calendar-day arithmetic is intentional: it stays correct across 23/25-hour
    DST transitions instead of adding a fixed number of seconds.
    """

    return training_day(moment, profile.timezone) + timedelta(days=1)


def _logistic(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _tier(probability: float) -> str:
    if probability >= 0.97:
        return "R0 RECOVERY"
    if probability >= 0.90:
        return "R1 WARMUP"
    if probability >= 0.65:
        return "R2 GROWTH"
    if probability >= 0.35:
        return "R3 CHALLENGE"
    return "R4 FUTURE"


def _target(clear: int, probability: float) -> str:
    if clear < 4:
        return "EASY"
    if clear < 6:
        return "HARD" if probability >= 0.95 else "NORMAL"
    if clear == 6:
        return "EXHARD"
    return "BP / EX"


def _percentile_ranks(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted((value, index) for index, value in enumerate(values))
    result = [0.0] * len(values)
    denominator = max(1, len(values) - 1)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][0] == ordered[start][0]:
            end += 1
        rank = (start + end - 1) / 2 / denominator
        for _, index in ordered[start:end]:
            result[index] = rank
        start = end
    return result


def _frontier(rows: list[tuple[float, bool]]) -> float:
    return fit_difficulty_frontier(rows)


def _optional_number(row: Mapping[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        rendered = float(value)
    except (TypeError, ValueError):
        return None
    return rendered if math.isfinite(rendered) else None


def _optional_percentile_ranks(values: Sequence[float | None]) -> list[float]:
    known = [(value, index) for index, value in enumerate(values) if value is not None]
    result = [0.0] * len(values)
    denominator = max(1, len(known) - 1)
    ordered = sorted(known)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][0] == ordered[start][0]:
            end += 1
        rank = (start + end - 1) / 2 / denominator
        for _, index in ordered[start:end]:
            result[index] = rank
        start = end
    return result


def _quantile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * max(0.0, min(1.0, fraction)))
    return ordered[index]


def _load_candidates(
    records: Sequence[Mapping[str, Any]],
    model: ModelSnapshot | None,
) -> tuple[
    list[Candidate], tuple[str, str], int, tuple[TableFrontier, ...]
]:
    rows = list(records)
    if not rows:
        raise MenuBuildError(
            "no owned 7key table entries; refresh and match difficulty tables first"
        )
    compatible_model = (
        model is not None and tuple(model.feature_names) == MODEL_FEATURE_NAMES
    )
    model_version = model.version if compatible_model else 0
    # Keep every independent table rating while scoring each chart only once.
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        sha256 = str(row["sha256"])
        grouped.setdefault(sha256, []).append(row)
    deduped = [memberships[0] for memberships in grouped.values()]

    feature_ranks: dict[str, list[float]] = {}
    for axis in FEATURE_AXES:
        feature_ranks[axis] = _percentile_ranks(
            [float(row[axis] or 0.0) for row in deduped]
        )
    warmup_feature_ranks = {
        feature: _optional_percentile_ranks(
            [_optional_number(row, feature) for row in deduped]
        )
        for feature in WARMUP_FEATURES
    }

    table_outcomes: dict[str, dict[int, list[tuple[float, bool]]]] = defaultdict(
        lambda: {4: [], 5: [], 6: []}
    )
    for sha256, memberships in grouped.items():
        base = memberships[0]
        if int(base["playcount"]) <= 0 or int(base["last_played"]) <= 0:
            continue
        clear = int(base["clear"])
        seen_ratings: set[tuple[str, str]] = set()
        for row in memberships:
            table_id = str(row["table_id"])
            source_level = str(row["level"])
            if (table_id, source_level) in seen_ratings:
                continue
            seen_ratings.add((table_id, source_level))
            level = numeric_level(source_level)
            if level is None:
                continue
            for threshold in (4, 5, 6):
                table_outcomes[table_id][threshold].append(
                    (level, clear >= threshold)
                )

    present_tables = {
        str(row["table_id"]) for memberships in grouped.values() for row in memberships
    }
    table_frontiers = tuple(
        TableFrontier(
            table_id=table_id,
            easy=_frontier(table_outcomes[table_id][4]),
            normal=_frontier(table_outcomes[table_id][5]),
            hard=_frontier(table_outcomes[table_id][6]),
            warmup_anchor=min(
                math.floor(_frontier(table_outcomes[table_id][6])),
                _quantile(
                    [
                        level for level, cleared in table_outcomes[table_id][6]
                        if cleared
                    ],
                    0.75,
                ),
            ),
            observations=len(table_outcomes[table_id][4]),
            hard_clears=sum(cleared for _, cleared in table_outcomes[table_id][6]),
        )
        for table_id in sorted(
            present_tables,
            key=lambda value: (
                DIFFICULTY_TABLES.index(value)
                if value in DIFFICULTY_TABLES else len(DIFFICULTY_TABLES),
                value,
            ),
        )
    )
    frontier_by_table = {frontier.table_id: frontier for frontier in table_frontiers}

    weakness_raw: dict[str, float] = {}
    for axis in FEATURE_AXES:
        failed = [
            feature_ranks[axis][index]
            for index, row in enumerate(deduped)
            if int(row["playcount"]) > 0 and int(row["clear"]) < 4
        ]
        cleared = [
            feature_ranks[axis][index]
            for index, row in enumerate(deduped)
            if int(row["playcount"]) > 0 and int(row["clear"]) >= 4
        ]
        failed_mean = sum(failed) / len(failed) if failed else 0.5
        cleared_mean = sum(cleared) / len(cleared) if cleared else 0.5
        weakness_raw[axis] = max(0.01, failed_mean - cleared_mean + 0.1)
    axes = tuple(
        axis for axis, _ in sorted(weakness_raw.items(), key=lambda item: -item[1])[:2]
    )
    while len(axes) < 2:
        axes += ("density",)

    candidates: list[Candidate] = []
    for index, row in enumerate(deduped):
        memberships = grouped[str(row["sha256"])]
        ratings = tuple(
            DifficultyRating(
                table_id=str(membership["table_id"]),
                source_level=str(membership["level"]),
                level_number=numeric_level(str(membership["level"])),
            )
            for membership in sorted(
                memberships,
                key=lambda value: (
                    DIFFICULTY_TABLES.index(str(value["table_id"]))
                    if str(value["table_id"]) in DIFFICULTY_TABLES
                    else len(DIFFICULTY_TABLES),
                    str(value["table_id"]),
                    numeric_level(str(value["level"])) is None,
                    numeric_level(str(value["level"])) or 0.0,
                ),
            )
        )
        primary_rating = min(
            ratings,
            key=lambda rating: (
                -frontier_by_table.get(
                    rating.table_id,
                    TableFrontier(rating.table_id, 0, 0, 0, 0, 0, 0),
                ).observations,
                DIFFICULTY_TABLES.index(rating.table_id)
                if rating.table_id in DIFFICULTY_TABLES else len(DIFFICULTY_TABLES),
                rating.table_id,
                rating.level_number is None,
                rating.level_number or 0.0,
            ),
        )
        primary_frontier = frontier_by_table.get(primary_rating.table_id)
        if primary_rating.level_number is None:
            completion_margin = 0.0
            probability = 0.5
        else:
            easy_frontier = (
                primary_rating.level_number
                if primary_frontier is None or primary_frontier.observations == 0
                else primary_frontier.easy
            )
            completion_margin = (easy_frontier - primary_rating.level_number) / 1.5
            probability = _logistic(completion_margin)
        model_probability = predict_snapshot(
            model if compatible_model else None,
            completion_margin,
            float(row["density"]),
            float(row["model_scratch"]),
        )
        if model_probability is not None:
            probability = model_probability
        clear = int(row["clear"])
        if clear >= 7:
            probability = max(probability, 0.99)
        elif clear >= 6:
            probability = max(probability, 0.97)
        elif clear >= 5:
            probability = max(probability, 0.90)
        elif (
            clear >= 4
            and int(row.get("recent_successes") or 0) >= 2
            and int(row.get("recent_failures") or 0) == 0
        ):
            probability = max(probability, 0.88)
        elif int(row["playcount"]) > 2:
            probability = max(0.03, probability - 0.05)
        scores = {axis: feature_ranks[axis][index] for axis in FEATURE_AXES}
        warmup_scores = {
            feature: warmup_feature_ranks[feature][index]
            for feature in WARMUP_FEATURES
        }
        warmup_features = {
            feature: _optional_number(row, feature) for feature in WARMUP_FEATURES
        }
        warmup_values = list(warmup_scores.values())
        warmup_load = (
            0.65 * max(warmup_values, default=0.0)
            + 0.35 * (sum(warmup_values) / len(warmup_values) if warmup_values else 0.0)
        )
        primary = max(axes, key=lambda axis: scores[axis] * weakness_raw[axis])
        weakness = sum(scores[axis] * weakness_raw[axis] for axis in axes)
        weakness /= sum(weakness_raw[axis] for axis in axes)
        candidates.append(
            Candidate(
                sha256=str(row["sha256"]),
                md5=None if row["md5"] is None else str(row["md5"]),
                title=str(row["title"] or "UNKNOWN"),
                artist=str(row["artist"] or "UNKNOWN"),
                notes=int(row["notes"]),
                table_id=primary_rating.table_id,
                source_level=primary_rating.source_level,
                level_number=primary_rating.level_number,
                ratings=ratings,
                clear=clear,
                playcount=int(row["playcount"]),
                last_played=int(row["last_played"]),
                minbp=(None if row.get("minbp") is None else int(row["minbp"])),
                recent_successes=int(row.get("recent_successes") or 0),
                recent_failures=int(row.get("recent_failures") or 0),
                recent_played_at=int(row.get("recent_played_at") or 0),
                recent_bp_rate=_optional_number(row, "recent_bp_rate"),
                p_complete=probability,
                tier=_tier(probability),
                target=_target(clear, probability),
                weakness=max(0.0, min(1.0, weakness)),
                primary_axis=primary,
                high_load=max(scores["density"], scores["scratch"]),
                feature_scores=scores,
                warmup_features=warmup_features,
                warmup_feature_scores=warmup_scores,
                warmup_load=max(0.0, min(1.0, warmup_load)),
                chart_seconds=_optional_number(row, "chart_seconds"),
                practice_low=(
                    None if row.get("practice_low") is None
                    else bool(row.get("practice_low"))
                ),
            )
        )
    return candidates, (axes[0], axes[1]), model_version, table_frontiers


def _expected(candidate: Candidate) -> int:
    survival = 1.0 if candidate.clear >= 4 else max(0.60, min(0.98, candidate.p_complete + 0.20))
    return max(1, round(candidate.notes * survival))


def _utility(
    candidate: Candidate,
    *,
    target_p: float,
    now: int,
    rng: random.Random,
    readiness: str,
) -> float:
    challenge = 1.0 - min(1.0, abs(candidate.p_complete - target_p) / 0.55)
    days = max(0.0, (now - candidate.last_played) / 86_400) if candidate.last_played else 0.0
    review = min(1.0, days / 14.0)
    information = 4.0 * candidate.p_complete * (1.0 - candidate.p_complete)
    novelty = 1.0 / (1.0 + candidate.playcount)
    fatigue = candidate.high_load * (0.30 if readiness == "tired" else 0.08)
    boredom = min(0.25, candidate.playcount / 40.0)
    return (
        0.35 * challenge
        + 0.30 * candidate.weakness
        + 0.15 * review
        + 0.10 * information
        + 0.10 * novelty
        - fatigue
        - boredom
        + rng.random() * 0.03
    )


def _to_item(
    candidate: Candidate,
    category: str,
    *,
    reason: str,
    attempt: int = 1,
    optional: bool = False,
    is_exploration: bool = False,
    rating: DifficultyRating | None = None,
    target: str | None = None,
    band: str | None = None,
) -> MenuItem:
    displayed_rating = rating or DifficultyRating(
        candidate.table_id, candidate.source_level, candidate.level_number
    )
    return MenuItem(
        sequence=0,
        sha256=candidate.sha256,
        md5=candidate.md5,
        title=candidate.title,
        artist=candidate.artist,
        notes=candidate.notes,
        expected_judged=_expected(candidate),
        category=category,
        table_id=displayed_rating.table_id,
        source_level=displayed_rating.source_level,
        band=band or candidate.tier.split()[0],
        p_complete=round(candidate.p_complete, 4),
        target=target or candidate.target,
        reason=reason,
        attempt=attempt,
        optional=optional,
        is_exploration=is_exploration,
    )


def _has_warmup_evidence(candidate: Candidate, *, now: int) -> bool:
    if candidate.clear >= 6 and candidate.last_played > 0:
        return True
    return (
        candidate.clear >= 4
        and candidate.recent_successes >= 2
        and candidate.recent_failures == 0
        and candidate.recent_played_at > 0
        and now - candidate.recent_played_at <= 30 * 86_400
        and candidate.recent_bp_rate is not None
        and candidate.recent_bp_rate <= 0.05
    )


def _warmup_features_are_safe(candidate: Candidate, *, readiness: str) -> bool:
    raw = candidate.warmup_features
    ranks = candidate.warmup_feature_scores
    if (raw.get("stop_count") or 0.0) > 0.0:
        return False
    if (raw.get("soflan_changes") or 0.0) > 4.0:
        return False
    if candidate.chart_seconds is not None and candidate.chart_seconds > 240.0:
        return False
    thresholds = {
        "density_p90": 0.82,
        "end_density": 0.85,
        "burst_max": 0.88,
        "scratch_rate": 0.88,
        "scratch_combo_rate": 0.85,
        "ln_rate": 0.90,
        "soflan_var": 0.85,
        "soflan_changes": 0.85,
        "micro_rate": 0.85,
        "long_jack_rate": 0.85,
        "avg_chord": 0.90,
        "chord_ge3": 0.88,
    }
    if any(ranks.get(feature, 0.0) >= limit for feature, limit in thresholds.items()):
        return False
    return candidate.warmup_load <= (0.55 if readiness == "tired" else 0.68)


def _select_warmup(
    candidates: list[Candidate],
    used: set[str],
    *,
    quota: int,
    frontiers: Sequence[TableFrontier],
    now: int,
    rng: random.Random,
    readiness: str,
    adjustment: int,
) -> list[MenuItem]:
    """Select familiar, low-load charts inside each table's own HARD band."""

    frontier_by_table = {frontier.table_id: frontier for frontier in frontiers}
    effective_shift = adjustment - (1 if readiness == "tired" else 0)
    eligible: list[tuple[Candidate, DifficultyRating, float]] = []
    for candidate in candidates:
        if candidate.sha256 in used or not _has_warmup_evidence(candidate, now=now):
            continue
        if not _warmup_features_are_safe(candidate, readiness=readiness):
            continue
        allowed: list[tuple[DifficultyRating, TableFrontier, float]] = []
        for rating in candidate.ratings:
            if rating.table_id not in DIFFICULTY_TABLES:
                continue
            frontier = frontier_by_table.get(rating.table_id)
            if (
                rating.level_number is None
                or frontier is None
                or frontier.observations < 8
                or frontier.hard_clears < 3
            ):
                continue
            anchor = frontier.warmup_anchor + effective_shift
            lower, upper = anchor - 2.0, anchor
            assert rating.level_number is not None
            if lower <= rating.level_number <= upper:
                progress = max(0.0, min(1.0, (rating.level_number - lower) / 2.0))
                allowed.append((rating, frontier, progress))
        if not allowed:
            continue
        rating, _, progress = min(
            allowed,
            key=lambda value: (
                -value[1].observations,
                abs(value[2] - 0.5),
                value[0].table_id,
            ),
        )
        eligible.append((candidate, rating, progress))

    selected: list[tuple[Candidate, DifficultyRating, float]] = []
    total = 0
    while eligible and len(selected) < 4 and total < quota:
        target_progress = len(selected) / 3.0
        choice = min(
            eligible,
            key=lambda value: (
                0.50 * abs(value[2] - target_progress)
                + 0.35 * value[0].warmup_load
                + 0.10 * min(1.0, (value[0].chart_seconds or 120.0) / 240.0)
                - (0.08 if value[0].practice_low else 0.0)
                - min(0.05, value[0].playcount / 200.0)
                + rng.random() * 0.01,
                value[0].sha256,
            ),
        )
        selected.append(choice)
        total += _expected(choice[0])
        chosen_sha = choice[0].sha256
        eligible = [value for value in eligible if value[0].sha256 != chosen_sha]

    selected.sort(
        key=lambda value: (
            0.75 * value[2] + 0.25 * value[0].warmup_load,
            value[0].sha256,
        )
    )
    items: list[MenuItem] = []
    for candidate, rating, _ in selected:
        used.add(candidate.sha256)
        items.append(
            _to_item(
                candidate,
                "01 WARMUP",
                rating=rating,
                target="COMFORT",
                band="WARMUP",
                reason=(
                    f"{rating.table_id} {rating.source_level} / "
                    f"familiar low-load"
                ),
            )
        )
    return items


def _select(
    candidates: list[Candidate],
    used: set[str],
    *,
    category: str,
    quota: int,
    target_p: float,
    predicate: Callable[[Candidate], bool],
    now: int,
    rng: random.Random,
    readiness: str,
    attempts: int = 1,
    optional: bool = False,
    allow_fallback: bool = True,
) -> list[MenuItem]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.sha256 not in used
        and predicate(candidate)
        and not (readiness == "tired" and candidate.high_load >= 0.80)
    ]
    if not eligible and allow_fallback:
        eligible = [candidate for candidate in candidates if candidate.sha256 not in used]
    eligible.sort(
        key=lambda candidate: _utility(
            candidate,
            target_p=target_p,
            now=now,
            rng=rng,
            readiness=readiness,
        ),
        reverse=True,
    )
    selected: list[MenuItem] = []
    total = 0
    candidate_index = 0
    while eligible:
        # One in five picks explores another well-matched candidate rather than
        # always taking the highest utility.  The seeded RNG keeps it auditable.
        is_exploration = candidate_index % 5 == 4 and len(eligible) > 1
        pick = rng.randrange(min(10, len(eligible))) if is_exploration else 0
        candidate = eligible.pop(pick)
        reason = f"{candidate.primary_axis} / {candidate.tier}"
        for attempt in range(1, attempts + 1):
            selected.append(
                _to_item(
                    candidate,
                    category,
                    reason=reason,
                    attempt=attempt,
                    optional=optional,
                    is_exploration=is_exploration,
                )
            )
            total += _expected(candidate)
        used.add(candidate.sha256)
        candidate_index += 1
        if total >= quota:
            break
    return selected


def _order(core: dict[str, list[MenuItem]]) -> list[MenuItem]:
    ordered = list(core["01 WARMUP"])
    first_focus: list[MenuItem] = []
    repeats: dict[str, MenuItem] = {}
    for category in ("02 FOCUS-A", "04 FOCUS-B"):
        for item in core[category]:
            if item.attempt == 1:
                first_focus.append(item)
            else:
                repeats[item.sha256] = item
    fillers = []
    for category in (
        "03 TRANSFER-A",
        "05 TRANSFER-B",
        "06 LAMP",
        "07 REVIEW",
        "08 PROBE",
    ):
        fillers.extend(core[category])
    # Groups of four focus charts space one another.  The final partial group
    # consumes only the number of fillers needed to preserve the same gap.
    for start in range(0, len(first_focus), 4):
        group = first_focus[start : start + 4]
        ordered.extend(group)
        for _ in range(4 - len(group)):
            if fillers:
                ordered.append(fillers.pop(0))
        ordered.extend(
            repeats[focus.sha256]
            for focus in group
            if focus.sha256 in repeats
        )
    ordered.extend(fillers)
    return ordered


def _table_warnings(
    sources: Sequence[Mapping[str, Any]],
    candidates: Sequence[Candidate],
    frontiers: Sequence[TableFrontier],
) -> tuple[str, ...]:
    """Describe missing or failed table coverage without hiding it behind fallback."""

    by_id = {str(source.get("table_id")): source for source in sources}
    matched = {
        rating.table_id for candidate in candidates for rating in candidate.ratings
    }
    frontier_by_id = {frontier.table_id: frontier for frontier in frontiers}
    warnings: list[str] = []
    for table_id in DIFFICULTY_TABLES:
        source = by_id.get(table_id)
        if source is None:
            warnings.append(f"{table_id}: difficulty table has not been refreshed")
            continue
        error = source.get("last_error")
        if error:
            warnings.append(f"{table_id}: refresh failed ({error})")
        elif table_id not in matched:
            warnings.append(f"{table_id}: no owned charts matched")
        else:
            frontier = frontier_by_id.get(table_id)
            if (
                frontier is None
                or frontier.observations < 8
                or frontier.hard_clears < 3
            ):
                observations = 0 if frontier is None else frontier.observations
                hard_clears = 0 if frontier is None else frontier.hard_clears
                warnings.append(
                    f"{table_id}: insufficient lamp evidence for warmup "
                    f"({observations} observations, {hard_clears} HARD+)"
                )
    return tuple(warnings)


def build_session_from_input(
    recommendation_input: RecommendationInput,
    *,
    menu_date: str | date | None = None,
    readiness: str | None = None,
    target_judged: int | None = None,
    reserve_judged: int | None = None,
    clock: Callable[[], float] = time.time,
) -> Session:
    """Build a reproducible recommendation from storage-neutral input."""

    profile_settings = recommendation_input.profile.settings or ProfileSettings(
        timezone=recommendation_input.profile.timezone,
        target_judged=recommendation_input.profile.target_judged,
        reserve_judged=recommendation_input.profile.reserve_judged,
        readiness=recommendation_input.profile.readiness,
    )
    effective_settings = ProfileSettings(
        timezone=profile_settings.timezone,
        target_judged=(
            profile_settings.target_judged if target_judged is None else target_judged
        ),
        reserve_judged=(
            profile_settings.reserve_judged if reserve_judged is None else reserve_judged
        ),
        readiness=profile_settings.readiness if readiness is None else readiness,
    )
    effective_profile = ProfileContext(
        profile_id=recommendation_input.profile.profile_id,
        display_name=recommendation_input.profile.display_name,
        timezone=effective_settings.timezone,
        seed_namespace=recommendation_input.profile.seed_namespace,
        settings=effective_settings,
    )
    now = int(clock())
    rendered_date = (
        next_training_date(now, effective_profile).isoformat()
        if menu_date is None
        else menu_date.isoformat() if isinstance(menu_date, date) else str(menu_date)
    )
    datetime.strptime(rendered_date, "%Y-%m-%d")
    if recommendation_input.import_id <= 0:
        raise MenuBuildError("ingest a daily score/scoredatalog snapshot first")
    import_id = recommendation_input.import_id
    baseline_judged = recommendation_input.baseline_judged
    candidates, axes, model_version, table_frontiers = _load_candidates(
        recommendation_input.candidates, recommendation_input.model
    )
    table_warnings = _table_warnings(
        recommendation_input.table_sources, candidates, table_frontiers
    )
    seed = hashlib.sha256(
        f"{effective_profile.deterministic_namespace}:"
        f"{rendered_date}:{import_id}:{model_version}:{effective_settings.readiness}:"
        f"{effective_settings.target_judged}:{effective_settings.reserve_judged}:"
        f"{recommendation_input.warmup_adjustment}".encode("utf-8")
    ).hexdigest()[:16]
    rng = random.Random(seed)
    used: set[str] = set()
    tired_shift = 0.08 if effective_settings.readiness == "tired" else 0.0
    core: dict[str, list[MenuItem]] = {}
    core["01 WARMUP"] = _select_warmup(
        candidates,
        used,
        quota=QUOTAS["01 WARMUP"],
        frontiers=table_frontiers,
        now=now,
        rng=rng,
        readiness=effective_settings.readiness,
        adjustment=recommendation_input.warmup_adjustment,
    )
    if not core["01 WARMUP"]:
        table_warnings += (
            "warmup: no chart met safe lamp-evidence and feature criteria",
        )
    for category, axis in (("02 FOCUS-A", axes[0]), ("04 FOCUS-B", axes[1])):
        core[category] = _select(
            candidates, used, category=category, quota=QUOTAS[category],
            target_p=0.77 + tired_shift,
            predicate=lambda c, axis=axis: c.primary_axis == axis and 0.62 <= c.p_complete <= 0.92,
            now=now, rng=rng, readiness=effective_settings.readiness, attempts=2,
        )
    for category, axis in (("03 TRANSFER-A", axes[0]), ("05 TRANSFER-B", axes[1])):
        core[category] = _select(
            candidates, used, category=category, quota=QUOTAS[category],
            target_p=0.70 + tired_shift,
            predicate=lambda c, axis=axis: c.primary_axis == axis and c.playcount <= 1 and 0.50 <= c.p_complete <= 0.90,
            now=now, rng=rng, readiness=effective_settings.readiness,
        )
    core["06 LAMP"] = _select(
        candidates, used, category="06 LAMP", quota=round(QUOTAS["06 LAMP"] * 0.75),
        target_p=0.55 + tired_shift,
        predicate=lambda c: c.clear < 4 and 0.30 <= c.p_complete <= 0.78,
        now=now, rng=rng, readiness=effective_settings.readiness, allow_fallback=False,
    )
    core["06 LAMP"].extend(
        _select(
            candidates, used, category="06 LAMP",
            quota=QUOTAS["06 LAMP"] - round(QUOTAS["06 LAMP"] * 0.75),
            target_p=0.97, predicate=lambda c: c.clear in {4, 5} and c.p_complete >= 0.95,
            now=now, rng=rng, readiness=effective_settings.readiness, allow_fallback=False,
        )
    )
    core["07 REVIEW"] = _select(
        candidates, used, category="07 REVIEW", quota=QUOTAS["07 REVIEW"],
        target_p=0.85 + tired_shift,
        predicate=lambda c: c.last_played > 0 and now - c.last_played >= 3 * 86_400,
        now=now, rng=rng, readiness=effective_settings.readiness,
        allow_fallback=False,
    )
    core["08 PROBE"] = _select(
        candidates, used, category="08 PROBE", quota=QUOTAS["08 PROBE"],
        target_p=0.82 + tired_shift,
        predicate=lambda c: 0.70 <= c.p_complete <= 0.95,
        now=now, rng=rng, readiness=effective_settings.readiness,
    )
    queue = _order(core)
    core_total = sum(item.expected_judged for item in queue)
    if core_total < effective_settings.target_judged:
        supplement = _select(
            candidates, used, category="05 TRANSFER-B",
            quota=effective_settings.target_judged - core_total, target_p=0.75 + tired_shift,
            predicate=lambda c: True, now=now, rng=rng, readiness=effective_settings.readiness,
        )
        queue.extend(supplement)
        core_total += sum(item.expected_judged for item in supplement)
    reserve = _select(
        candidates, used, category="09 RESERVE", quota=effective_settings.reserve_judged,
        target_p=0.88 + tired_shift, predicate=lambda c: c.p_complete >= 0.65,
        now=now, rng=rng, readiness=effective_settings.readiness, optional=True,
    )
    queue.extend(reserve)
    queue = [replace(item, sequence=index) for index, item in enumerate(queue, 1)]
    reserve_total = sum(item.expected_judged for item in reserve)
    return Session(
        menu_date=rendered_date,
        generated_at=now,
        seed=seed,
        readiness=effective_settings.readiness,
        target_judged=effective_settings.target_judged,
        reserve_target=effective_settings.reserve_judged,
        core_expected_judged=core_total,
        reserve_expected_judged=reserve_total,
        weakness_axes=axes,
        baseline_judged=baseline_judged,
        import_id=import_id,
        model_version=model_version,
        model_status="validated_model" if model_version else "cold_start",
        table_frontiers=table_frontiers,
        table_warnings=table_warnings,
        warmup_adjustment=recommendation_input.warmup_adjustment,
        queue=tuple(queue),
        personal=tuple(sorted(candidates, key=lambda c: (-c.p_complete, c.title))),
        profile=effective_profile,
    )


def build_session(
    source: RecommendationInput | object,
    *,
    menu_date: str | date | None = None,
    readiness: str | None = None,
    target_judged: int | None = None,
    reserve_judged: int | None = None,
    clock: Callable[[], float] = time.time,
    profile: ProfileContext | None = None,
) -> Session:
    """Build from domain input or the legacy local SQLite adapter."""

    if isinstance(source, RecommendationInput):
        recommendation_input = source
    else:
        from oraja_training.db.recommendation_adapter import load_recommendation_input

        recommendation_input = load_recommendation_input(
            source, profile=profile or ProfileContext()
        )
    if profile is not None and recommendation_input.profile != profile:
        recommendation_input = replace(recommendation_input, profile=profile)
    return build_session_from_input(
        recommendation_input,
        menu_date=menu_date,
        readiness=readiness,
        target_judged=target_judged,
        reserve_judged=reserve_judged,
        clock=clock,
    )


def build_session_from_repository(
    repository: RecommendationRepository,
    *,
    profile: ProfileContext,
    menu_date: str | date | None = None,
    readiness: str | None = None,
    target_judged: int | None = None,
    reserve_judged: int | None = None,
    clock: Callable[[], float] = time.time,
) -> Session:
    """Load through a repository port and run the storage-free planner."""

    return build_session_from_input(
        repository.load_input(profile),
        menu_date=menu_date,
        readiness=readiness,
        target_judged=target_judged,
        reserve_judged=reserve_judged,
        clock=clock,
    )


def recommendation_output(session: Session) -> RecommendationOutput:
    """Convert a planned session to the public adapter output value."""

    return RecommendationOutput(
        profile=session.profile,
        menu_date=session.menu_date,
        seed=session.seed,
        queue=tuple(asdict(item) for item in session.queue),
        personal=tuple(asdict(candidate) for candidate in session.personal),
        import_id=session.import_id,
        baseline_judged=session.baseline_judged,
        model_version=session.model_version,
        readiness=session.readiness,
        generated_at=session.generated_at,
        target_judged=session.target_judged,
        reserve_target=session.reserve_target,
        core_expected_judged=session.core_expected_judged,
        reserve_expected_judged=session.reserve_expected_judged,
        weakness_axes=session.weakness_axes,
        model_status=session.model_status,
        table_frontiers=tuple(asdict(value) for value in session.table_frontiers),
        table_warnings=session.table_warnings,
        warmup_adjustment=session.warmup_adjustment,
    )


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    with temporary.open("wb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _immutable_json(path: Path, value: Any) -> None:
    """Create a content-addressed file without ever overwriting its bytes."""

    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    if path.exists():
        if path.read_bytes() != payload:
            raise RevisionConflictError(f"immutable artifact conflict: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise RevisionConflictError(f"immutable artifact conflict: {path.name}")


def _table_payloads(session: Session) -> dict[str, Any]:
    display_name = session.profile.display_name
    recommend_header = {
        "name": f"{display_name} Personal Recommend",
        "symbol": "R",
        "data_url": "score.json",
        "level_order": [
            "R0 RECOVERY", "R1 WARMUP", "R2 GROWTH", "R3 CHALLENGE", "R4 FUTURE"
        ],
        "mode": "beat-7k",
    }
    recommend_score = [
        {
            "sha256": candidate.sha256,
            "md5": candidate.md5,
            "level": candidate.tier,
            "title": candidate.title,
            "artist": candidate.artist,
            "comment": (
                " + ".join(
                    f"{rating.table_id} {rating.source_level}"
                    for rating in candidate.ratings
                )
                + f" / target {candidate.target}"
            ),
        }
        for candidate in session.personal
    ]
    today_header = {
        "name": f"{display_name} Daily {session.menu_date}",
        "symbol": "D",
        "data_url": "score.json",
        "level_order": list(LEVEL_ORDER),
        "mode": "beat-7k",
    }
    unique: dict[str, MenuItem] = {}
    for item in session.queue:
        unique.setdefault(item.sha256, item)
    today_score = [
        {
            "sha256": item.sha256,
            "md5": item.md5,
            "level": item.category,
            "title": item.title,
            "artist": item.artist,
            "comment": f"#{item.sequence} / {item.target} / {item.reason}",
        }
        for item in unique.values()
    ]
    return {
        "table/recommend/header.json": recommend_header,
        "table/recommend/score.json": recommend_score,
        "table/today/header.json": today_header,
        "table/today/score.json": today_score,
    }


def _artifact_content_hash(payloads: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_json(payloads[name]))
        digest.update(b"\n")
    return digest.hexdigest()


def _latest_pointer(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_revision_directory(
    root: Path,
    revision: int,
    content_hash: str,
    files: dict[str, Any],
) -> Path:
    revisions = root / "revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    revision_root = revisions / f"{revision}-{content_hash}"
    if revision_root.exists():
        for relative, value in files.items():
            _immutable_json(revision_root / relative, value)
        return revision_root

    temporary = revisions / f".{revision}-{content_hash}.{os.getpid()}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for relative, value in files.items():
            _immutable_json(temporary / relative, value)
        try:
            os.replace(temporary, revision_root)
        except FileExistsError:
            for relative, value in files.items():
                _immutable_json(revision_root / relative, value)
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            temporary.rmdir()
    except Exception:
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        if temporary.exists():
            temporary.rmdir()
        raise
    return revision_root


def write_export(
    session: Session,
    output_dir: str | Path,
    *,
    revision: int | None = None,
) -> ArtifactRevision:
    """Write both tables to an immutable revision and advance the local pointer.

    The files directly below ``output_dir`` are a compatibility view for the
    existing localhost server. The canonical artifact is kept below a
    content-addressed revision directory and is never overwritten.
    """

    root = Path(output_dir).expanduser().resolve()
    payloads = _table_payloads(session)
    content_hash = _artifact_content_hash(payloads)
    pointer = _latest_pointer(root)
    current_revision = int(pointer.get("revision", 0) or 0)
    candidate_revision = current_revision + 1 if revision is None else int(revision)
    if candidate_revision <= 0:
        raise RevisionConflictError("revision must be positive")
    if candidate_revision == current_revision and pointer.get("content_hash") != content_hash:
        raise RevisionConflictError("revision already points to different content")

    manifest = {
        "menu_date": session.menu_date,
        "generated_at": session.generated_at,
        "seed": session.seed,
        "readiness": session.readiness,
        "target_judged": session.target_judged,
        "baseline_judged": session.baseline_judged,
        "core_expected_judged": session.core_expected_judged,
        "reserve_expected_judged": session.reserve_expected_judged,
        "weakness_axes": session.weakness_axes,
        "import_id": session.import_id,
        "model_version": session.model_version,
        "model_status": session.model_status,
        "table_frontiers": [asdict(value) for value in session.table_frontiers],
        "table_warnings": list(session.table_warnings),
        "warmup_adjustment": session.warmup_adjustment,
        "revision": candidate_revision,
        "content_hash": content_hash,
        "timezone": session.profile.timezone,
    }
    session_json = {**manifest, "queue": [asdict(item) for item in session.queue]}
    review = {
        "menu_date": session.menu_date,
        "status": session.model_status,
        "message": (
            "完走確率は検証済みモデルです。学習効果の順位付けはヒューリスティックです。"
            if session.model_version
            else "学習効果はヒューリスティックです。確率モデルは検証ゲート通過後に有効化します。"
        ),
        "weakness_axes": session.weakness_axes,
        "table_warnings": list(session.table_warnings),
        "warmup_adjustment": session.warmup_adjustment,
    }
    files = {
        **payloads,
        "manifest.json": manifest,
        "session.json": session_json,
        "review/latest.json": review,
    }
    revision_root = _write_revision_directory(
        root, candidate_revision, content_hash, files
    )

    latest = _latest_pointer(root)
    latest_revision = int(latest.get("revision", 0) or 0)
    published = candidate_revision > latest_revision
    if published:
        # This is a filesystem compare-and-set: a later completion wins even
        # when Queue/Workflow deliveries finish in reverse order.
        for relative, value in files.items():
            _atomic_json(root / relative, value)
        _atomic_json(
            root / "latest.json",
            {
                "revision": candidate_revision,
                "content_hash": content_hash,
                "object_prefix": str(revision_root.relative_to(root)),
            },
        )
    return ArtifactRevision(candidate_revision, content_hash, revision_root, published)
