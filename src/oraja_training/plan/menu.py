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
import re
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
from oraja_training.model.core import predict_snapshot


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


class MenuBuildError(DomainError):
    """Raised when owned table coverage is insufficient to build a menu."""


class RevisionConflictError(MenuBuildError):
    """Raised when an immutable revision number is reused for other content."""


@dataclass(frozen=True, slots=True)
class Candidate:
    sha256: str
    md5: str | None
    title: str
    artist: str
    notes: int
    table_id: str
    source_level: str
    level_number: float
    clear: int
    playcount: int
    last_played: int
    p_complete: float
    tier: str
    target: str
    weakness: float
    primary_axis: str
    high_load: float
    feature_scores: dict[str, float]


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


def _number(level: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", level)
    return float(match.group()) if match else 0.0


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
    for rank, (_, index) in enumerate(ordered):
        result[index] = rank / denominator
    return result


def _frontier(rows: list[tuple[float, bool]]) -> float:
    if not rows:
        return 0.0
    low = min(level for level, _ in rows) - 3.0
    high = max(level for level, _ in rows) + 3.0
    best = low
    best_loss = float("inf")
    for step in range(241):
        ability = low + (high - low) * step / 240
        loss = 0.0
        for level, cleared in rows:
            p = min(0.999, max(0.001, _logistic((ability - level) / 1.5)))
            loss -= math.log(p if cleared else 1.0 - p)
        if loss < best_loss:
            best, best_loss = ability, loss
    return best


def _load_candidates(
    records: Sequence[Mapping[str, Any]],
    model: ModelSnapshot | None,
) -> tuple[list[Candidate], tuple[str, str], int]:
    rows = list(records)
    if not rows:
        raise MenuBuildError(
            "no owned 7key table entries; refresh and match difficulty tables first"
        )
    model_version = 0 if model is None else model.version
    # One chart is recommended once even when multiple independent scales contain it.
    deduped: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        sha256 = str(row["sha256"])
        if sha256 not in seen:
            seen.add(sha256)
            deduped.append(row)

    feature_ranks: dict[str, list[float]] = {}
    for axis in FEATURE_AXES:
        feature_ranks[axis] = _percentile_ranks(
            [float(row[axis] or 0.0) for row in deduped]
        )
    table_outcomes: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for row in deduped:
        if int(row["playcount"]) > 0:
            table_outcomes[str(row["table_id"])].append(
                (_number(str(row["level"])), int(row["clear"]) >= 4)
            )
    frontiers = {
        table_id: _frontier(outcomes)
        for table_id, outcomes in table_outcomes.items()
    }

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
        table_id = str(row["table_id"])
        level = _number(str(row["level"]))
        frontier = frontiers.get(table_id, level)
        probability = _logistic((frontier - level) / 1.5)
        model_probability = predict_snapshot(
            model, level, float(row["density"]), float(row["model_scratch"])
        )
        if model_probability is not None:
            probability = model_probability
        clear = int(row["clear"])
        if clear >= 7:
            probability = max(probability, 0.99)
        elif clear >= 6:
            probability = max(probability, 0.97)
        elif clear >= 4:
            probability = max(probability, 0.90)
        elif int(row["playcount"]) > 2:
            probability = max(0.03, probability - 0.05)
        scores = {axis: feature_ranks[axis][index] for axis in FEATURE_AXES}
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
                table_id=table_id,
                source_level=str(row["level"]),
                level_number=level,
                clear=clear,
                playcount=int(row["playcount"]),
                last_played=int(row["last_played"]),
                p_complete=probability,
                tier=_tier(probability),
                target=_target(clear, probability),
                weakness=max(0.0, min(1.0, weakness)),
                primary_axis=primary,
                high_load=max(scores["density"], scores["scratch"]),
                feature_scores=scores,
            )
        )
    return candidates, (axes[0], axes[1]), model_version


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
    days = max(0.0, (now - candidate.last_played) / 86_400) if candidate.last_played else 30.0
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
) -> MenuItem:
    return MenuItem(
        sequence=0,
        sha256=candidate.sha256,
        md5=candidate.md5,
        title=candidate.title,
        artist=candidate.artist,
        notes=candidate.notes,
        expected_judged=_expected(candidate),
        category=category,
        table_id=candidate.table_id,
        source_level=candidate.source_level,
        band=candidate.tier.split()[0],
        p_complete=round(candidate.p_complete, 4),
        target=candidate.target,
        reason=reason,
        attempt=attempt,
        optional=optional,
        is_exploration=is_exploration,
    )


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
    # Each focus chart gets exactly three intervening attempts before retry.
    for focus in first_focus:
        ordered.append(focus)
        for _ in range(3):
            if fillers:
                ordered.append(fillers.pop(0))
        repeat = repeats.get(focus.sha256)
        if repeat is not None:
            ordered.append(repeat)
    ordered.extend(fillers)
    return ordered


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
    candidates, axes, model_version = _load_candidates(
        recommendation_input.candidates, recommendation_input.model
    )
    seed = hashlib.sha256(
        f"{effective_profile.deterministic_namespace}:"
        f"{rendered_date}:{import_id}:{model_version}:{effective_settings.readiness}:"
        f"{effective_settings.target_judged}:{effective_settings.reserve_judged}".encode("utf-8")
    ).hexdigest()[:16]
    rng = random.Random(seed)
    used: set[str] = set()
    tired_shift = 0.08 if effective_settings.readiness == "tired" else 0.0
    core: dict[str, list[MenuItem]] = {}
    core["01 WARMUP"] = _select(
        candidates, used, category="01 WARMUP", quota=QUOTAS["01 WARMUP"],
        target_p=0.96, predicate=lambda c: c.p_complete >= 0.90 and c.clear >= 4,
        now=now, rng=rng, readiness=effective_settings.readiness,
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
        predicate=lambda c: c.last_played == 0 or now - c.last_played >= 3 * 86_400,
        now=now, rng=rng, readiness=effective_settings.readiness,
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
        rendered_date,
        now,
        seed,
        effective_settings.readiness,
        effective_settings.target_judged,
        effective_settings.reserve_judged,
        core_total,
        reserve_total,
        axes,
        baseline_judged,
        import_id,
        model_version,
        "validated_model" if model_version else "cold_start",
        tuple(queue),
        tuple(sorted(candidates, key=lambda c: (-c.p_complete, c.title))),
        effective_profile,
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
            "comment": f"{candidate.table_id} {candidate.source_level} / target {candidate.target}",
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
