"""Auditable two-arm self-experiment assignment and outcome evaluation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
from typing import Any, Mapping, Sequence


ARMS = ("coach", "control")
INTERVAL_DAYS = (1, 3, 7, 14)
DAY_SECONDS = 86_400


def _utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Candidate:
    sha256: str
    mode: int
    p_pred: float


def _candidate(value: Mapping[str, Any]) -> Candidate:
    sha256 = str(value.get("sha256", "")).lower()
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("candidate sha256 must be 64 lowercase hexadecimal characters")
    mode = int(value.get("mode", 0))
    probability = float(value.get("p_pred"))
    if not 0.0 <= probability <= 1.0:
        raise ValueError("candidate p_pred must be between 0 and 1")
    return Candidate(sha256=sha256, mode=mode, p_pred=probability)


def _normalise_candidates(
    values: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, tuple[Candidate, ...]]:
    result: dict[str, tuple[Candidate, ...]] = {}
    for group in (*ARMS, "transfer"):
        candidates = tuple(_candidate(value) for value in values.get(group, ()))
        if not candidates:
            raise ValueError(f"candidate set {group!r} must not be empty")
        identities = {(item.sha256, item.mode) for item in candidates}
        if len(identities) != len(candidates):
            raise ValueError(f"candidate set {group!r} contains duplicate charts")
        result[group] = tuple(
            sorted(candidates, key=lambda item: (item.sha256, item.mode, item.p_pred))
        )
    coach = {(item.sha256, item.mode) for item in result["coach"]}
    control = {(item.sha256, item.mode) for item in result["control"]}
    if coach & control:
        raise ValueError("coach and control candidate sets must not overlap")
    return result


def _canonical_candidates(candidates: Mapping[str, Sequence[Candidate]]) -> str:
    return json.dumps(
        {
            group: [asdict(item) for item in candidates[group]]
            for group in (*ARMS, "transfer")
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _transfer_selection_probability(candidates_json: str) -> float:
    try:
        stored = _normalise_candidates(json.loads(candidates_json))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("stored experiment candidates are invalid") from error
    return 1.0 / len(stored["transfer"])


def _draw(seed: str, session_key: str, purpose: str) -> int:
    material = f"oraja-experiment-v1\0{seed}\0{session_key}\0{purpose}".encode()
    return int.from_bytes(hashlib.sha256(material).digest(), "big")


def _menu_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate(
        {
            "sha256": value.get("sha256"),
            "mode": value.get("mode", 0),
            "p_pred": value.get("p_complete"),
        }
    )
    return {
        **asdict(candidate),
        "title": str(value.get("title") or ""),
        "artist": str(value.get("artist") or ""),
    }


def build_candidate_sets(
    conn: sqlite3.Connection,
    *,
    experiment_id: int,
    session_key: str,
) -> dict[str, list[dict[str, Any]]]:
    """Derive one auditable same-table/level comparison from the latest menu."""

    experiment = conn.execute(
        "SELECT seed FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise ValueError(f"unknown experiment {experiment_id}")
    existing = conn.execute(
        """
        SELECT candidates_json FROM experiment_sessions
        WHERE experiment_id = ? AND session_key = ?
        """,
        (experiment_id, session_key),
    ).fetchone()
    if existing is not None:
        try:
            stored = _normalise_candidates(json.loads(str(existing[0])))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("stored experiment candidates are invalid") from error
        return {
            group: [asdict(item) for item in stored[group]]
            for group in (*ARMS, "transfer")
        }
    row = conn.execute(
        "SELECT slots_json FROM sessions ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("generate a Daily Menu before assigning an experiment session")
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("latest Daily Menu payload is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("latest Daily Menu payload is invalid")
    queue = payload.get("queue")
    personal = payload.get("personal")
    if not isinstance(queue, list) or not isinstance(personal, list):
        raise ValueError("latest Daily Menu has no candidate pool")

    personal_rows = [item for item in personal if isinstance(item, dict)]
    by_sha = {
        str(item.get("sha256")): item
        for item in personal_rows
        if isinstance(item.get("sha256"), str)
    }
    queue_identities: set[tuple[str, int]] = set()
    for item in queue:
        if not isinstance(item, dict) or not isinstance(item.get("sha256"), str):
            continue
        sha256 = str(item["sha256"])
        source = by_sha.get(sha256, {})
        queue_identities.add(
            (sha256, int(source.get("mode", item.get("mode", 0))))
        )
    reserved_rows = conn.execute(
        """
        SELECT selected_sha256 AS sha256, selected_mode AS mode
        FROM experiment_sessions WHERE experiment_id = ?
        UNION
        SELECT transfer_sha256, transfer_mode
        FROM experiment_sessions WHERE experiment_id = ?
        UNION
        SELECT target.sha256, target.mode
        FROM experiment_targets target
        JOIN experiment_sessions session ON session.id = target.session_id
        WHERE session.experiment_id = ?
        """,
        (experiment_id, experiment_id, experiment_id),
    ).fetchall()
    reserved = {
        (str(reserved_row["sha256"]), int(reserved_row["mode"]))
        for reserved_row in reserved_rows
    }

    focus_by_stratum: dict[
        tuple[str, str, bool, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for item in queue:
        if (
            not isinstance(item, dict)
            or item.get("category") not in {"02 FOCUS-A", "04 FOCUS-B"}
            or int(item.get("attempt") or 1) != 1
        ):
            continue
        sha256 = item.get("sha256")
        source = by_sha.get(str(sha256))
        if source is None:
            continue
        identity = (str(sha256), int(source.get("mode", 0)))
        if identity in reserved:
            continue
        key = (
            str(source.get("table_id") or item.get("table_id") or ""),
            str(source.get("source_level") or item.get("source_level") or ""),
            int(source.get("playcount") or 0) > 0,
            str(source.get("primary_axis") or ""),
        )
        if key[0] and key[1] and key[3]:
            focus_by_stratum[key].append(source)

    possibilities: list[
        tuple[
            tuple[str, str, bool, str],
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ] = []
    for key, coach_rows in sorted(focus_by_stratum.items()):
        table_id, source_level, was_played, primary_axis = key
        baseline = [
            item
            for item in personal_rows
            if str(item.get("table_id") or "") == table_id
            and str(item.get("source_level") or "") == source_level
            and (int(item.get("playcount") or 0) > 0) == was_played
            and (
                str(item.get("sha256") or ""), int(item.get("mode", 0))
            ) not in queue_identities | reserved
        ]
        transfer_pool = [
            item
            for item in personal_rows
            if str(item.get("table_id") or "") == table_id
            and str(item.get("source_level") or "") == source_level
            and int(item.get("playcount") or 0) == 0
            and int(item.get("clear") or 0) == 0
            and str(item.get("primary_axis") or "") == primary_axis
            and (
                str(item.get("sha256") or ""), int(item.get("mode", 0))
            ) not in queue_identities | reserved
        ]
        if not baseline or len(transfer_pool) < len(INTERVAL_DAYS):
            continue
        transfers = sorted(
            transfer_pool,
            key=lambda item: (
                str(item.get("sha256") or ""), int(item.get("mode", 0))
            ),
        )
        transfer_identities = {
            (str(item.get("sha256") or ""), int(item.get("mode", 0)))
            for item in transfers
        }
        control_rows = sorted(
            (
                item for item in baseline
                if (str(item.get("sha256") or ""), int(item.get("mode", 0)))
                not in transfer_identities
            ),
            key=lambda item: (
                str(item.get("sha256") or ""), int(item.get("mode", 0))
            ),
        )
        if not control_rows:
            continue
        possibilities.append((key, coach_rows, control_rows, transfers))
    if not possibilities:
        raise ValueError(
            "latest Daily Menu has no focus/control/unused-transfer stratum"
        )

    selected = possibilities[
        _draw(str(experiment["seed"]), session_key, "candidate-stratum")
        % len(possibilities)
    ]
    _, coach_rows, control_rows, transfer_rows = selected
    return {
        "coach": [_menu_candidate(item) for item in coach_rows],
        "control": [_menu_candidate(item) for item in control_rows],
        "transfer": [_menu_candidate(item) for item in transfer_rows],
    }


def start_experiment(
    conn: sqlite3.Connection,
    *,
    name: str,
    seed: str,
    starts_at: int,
    days: int = 14,
    min_samples_per_arm: int = 20,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Create a fixed-duration experiment without touching a beatoraja DB."""

    if not name.strip() or not seed:
        raise ValueError("name and seed must not be empty")
    if days < 1 or min_samples_per_arm < 1:
        raise ValueError("days and min_samples_per_arm must be positive")
    created = int(time.time()) if created_at is None else int(created_at)
    ends_at = int(starts_at) + int(days) * DAY_SECONDS
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO experiments(
              name, seed, starts_at, ends_at, min_samples_per_arm, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name.strip(), seed, int(starts_at), ends_at, min_samples_per_arm, created),
        )
    return {
        "experiment_id": int(cursor.lastrowid),
        "name": name.strip(),
        "starts_at": int(starts_at),
        "ends_at": ends_at,
        "min_samples_per_arm": min_samples_per_arm,
    }


def _assignment_result(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM experiment_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("assignment disappeared")

    def rendered_candidate(
        sha256: str, mode: int, p_pred: float
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sha256": sha256,
            "mode": mode,
            "p_pred": p_pred,
        }
        chart = conn.execute(
            "SELECT title, artist FROM charts WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if chart is not None:
            result["title"] = str(chart["title"] or "")
            result["artist"] = str(chart["artist"] or "")
        return result

    selected = rendered_candidate(
        str(row["selected_sha256"]),
        int(row["selected_mode"]),
        float(row["selected_p_pred"]),
    )
    legacy_transfer = rendered_candidate(
        str(row["transfer_sha256"]),
        int(row["transfer_mode"]),
        float(row["transfer_p_pred"]),
    )
    transfer_probability = _transfer_selection_probability(
        str(row["candidates_json"])
    )
    transfer_rows = conn.execute(
        """
        SELECT interval_days, sha256, mode, p_pred
        FROM experiment_targets
        WHERE session_id = ? AND target_kind = 'transfer'
        ORDER BY interval_days
        """,
        (session_id,),
    ).fetchall()
    transfers: list[dict[str, Any]] = []
    for target in transfer_rows:
        rendered = rendered_candidate(
            str(target["sha256"]),
            int(target["mode"]),
            float(target["p_pred"]),
        )
        rendered["interval_days"] = int(target["interval_days"])
        rendered["selection_probability"] = transfer_probability
        transfers.append(rendered)
    return {
        "session_id": int(row["id"]),
        "experiment_id": int(row["experiment_id"]),
        "session_key": str(row["session_key"]),
        "arm": str(row["arm"]),
        "arm_probability": float(row["arm_probability"]),
        "selection_probability": float(row["selection_probability"]),
        "transfer_selection_probability": transfer_probability,
        "candidate_hash": str(row["candidate_hash"]),
        "input_candidate_hash": str(row["input_candidate_hash"]),
        "selected": selected,
        # Retain the original day-1 field for callers and schema-v5 rows.
        "transfer": legacy_transfer,
        "transfers": transfers,
    }


def assign_session(
    conn: sqlite3.Connection,
    *,
    experiment_id: int,
    session_key: str,
    session_at: int,
    candidate_sets: Mapping[str, Sequence[Mapping[str, Any]]],
    assigned_at: int | None = None,
) -> dict[str, Any]:
    """Assign one whole session to one arm and schedule its evaluations."""

    if not session_key:
        raise ValueError("session_key must not be empty")
    submitted = _normalise_candidates(candidate_sets)
    submitted_json = _canonical_candidates(submitted)
    input_candidate_hash = hashlib.sha256(submitted_json.encode()).hexdigest()
    if conn.in_transaction:
        raise RuntimeError("assign_session requires a connection outside a transaction")

    session_id: int | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        experiment = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment is None:
            raise ValueError(f"unknown experiment {experiment_id}")
        if not (
            int(experiment["starts_at"])
            <= int(session_at)
            < int(experiment["ends_at"])
        ):
            raise ValueError("session_at is outside the experiment assignment period")

        existing = conn.execute(
            """
            SELECT id, session_at, candidate_hash, input_candidate_hash,
                   candidates_json
            FROM experiment_sessions
            WHERE experiment_id = ? AND session_key = ?
            """,
            (experiment_id, session_key),
        ).fetchone()
        if existing is not None:
            if int(existing["session_at"]) != int(session_at):
                raise ValueError("session_key was already assigned with different inputs")
            try:
                stored = _normalise_candidates(
                    json.loads(str(existing["candidates_json"]))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("stored experiment candidates are invalid") from error
            stored_json = _canonical_candidates(stored)
            stored_input_hash = str(
                existing["input_candidate_hash"] or existing["candidate_hash"]
            )
            if (
                input_candidate_hash != stored_input_hash
                and submitted_json != stored_json
            ):
                raise ValueError("session_key was already assigned with different inputs")
            session_id = int(existing["id"])
        else:
            used_rows = conn.execute(
                """
                SELECT selected_sha256, selected_mode,
                       transfer_sha256, transfer_mode
                FROM experiment_sessions
                WHERE experiment_id = ?
                """,
                (experiment_id,),
            ).fetchall()
            reserved = {
                identity
                for row in used_rows
                for identity in (
                    (str(row["selected_sha256"]), int(row["selected_mode"])),
                    (str(row["transfer_sha256"]), int(row["transfer_mode"])),
                )
            }
            target_rows = conn.execute(
                """
                SELECT target.sha256, target.mode
                FROM experiment_targets target
                JOIN experiment_sessions session ON session.id = target.session_id
                WHERE session.experiment_id = ?
                """,
                (experiment_id,),
            ).fetchall()
            reserved.update(
                (str(row["sha256"]), int(row["mode"])) for row in target_rows
            )
            candidates = {
                group: tuple(
                    item
                    for item in submitted[group]
                    if (item.sha256, item.mode) not in reserved
                )
                for group in (*ARMS, "transfer")
            }
            for group in ARMS:
                if not candidates[group]:
                    raise ValueError(
                        f"candidate set {group!r} has no chart unused by this experiment"
                    )
            transfer_candidates = tuple(
                item
                for item in candidates["transfer"]
                if conn.execute(
                    """
                    SELECT 1 FROM (
                      SELECT 1 FROM score_state
                      WHERE sha256 = ? AND mode = ?
                        AND (playcount > 0 OR clear > 0)
                      UNION ALL
                      SELECT 1 FROM plays
                      WHERE sha256 = ? AND mode = ? AND is_course = 0
                        AND (playcount > 0 OR clear > 0 OR completed IS NOT NULL)
                    )
                    LIMIT 1
                    """,
                    (item.sha256, item.mode, item.sha256, item.mode),
                ).fetchone()
                is None
            )
            if len(transfer_candidates) < len(INTERVAL_DAYS):
                raise ValueError(
                    "transfer candidates must contain four distinct charts "
                    "unpractised at session start"
                )
            candidates["transfer"] = transfer_candidates
            arm_identities = {
                (item.sha256, item.mode)
                for arm in ARMS
                for item in candidates[arm]
            }
            if any(
                (item.sha256, item.mode) in arm_identities
                for item in candidates["transfer"]
            ):
                raise ValueError(
                    "transfer candidates must be unpractised charts outside both arms"
                )
            candidates_json = _canonical_candidates(candidates)
            candidate_hash = hashlib.sha256(candidates_json.encode()).hexdigest()
            arm = ARMS[
                _draw(str(experiment["seed"]), session_key, "arm") % len(ARMS)
            ]
            arm_candidates = candidates[arm]
            selected = arm_candidates[
                _draw(str(experiment["seed"]), session_key, "selection")
                % len(arm_candidates)
            ]
            remaining_transfers = list(transfer_candidates)
            transfers: dict[int, Candidate] = {}
            for interval in INTERVAL_DAYS:
                purpose = "transfer" if interval == 1 else f"transfer:{interval}"
                transfers[interval] = remaining_transfers.pop(
                    _draw(str(experiment["seed"]), session_key, purpose)
                    % len(remaining_transfers)
                )
            # Schema-v5 columns remain the compatibility alias for day 1;
            # experiment_targets is authoritative for all four intervals.
            transfer = transfers[1]
            assigned = int(time.time()) if assigned_at is None else int(assigned_at)
            selection_probability = (
                1.0 / len(ARMS)
            ) * (1.0 / len(arm_candidates))
            cursor = conn.execute(
                """
                INSERT INTO experiment_sessions(
                  experiment_id, session_key, session_at, arm, arm_probability,
                  selection_probability, candidate_hash, candidates_json,
                  selected_sha256, selected_mode, selected_p_pred,
                  transfer_sha256, transfer_mode, transfer_p_pred, assigned_at,
                  input_candidate_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id, session_key, int(session_at), arm, 0.5,
                    selection_probability, candidate_hash, candidates_json,
                    selected.sha256, selected.mode, selected.p_pred,
                    transfer.sha256, transfer.mode, transfer.p_pred, assigned,
                    input_candidate_hash,
                ),
            )
            session_id = int(cursor.lastrowid)
            targets: list[tuple[Any, ...]] = []
            for interval in INTERVAL_DAYS:
                due_at = int(session_at) + interval * DAY_SECONDS
                for kind, candidate in (
                    ("retention", selected),
                    ("transfer", transfers[interval]),
                ):
                    targets.append(
                        (
                            session_id, kind, interval,
                            candidate.sha256, candidate.mode,
                            due_at, due_at + DAY_SECONDS, candidate.p_pred,
                        )
                    )
            conn.executemany(
                """
                INSERT INTO experiment_targets(
                  session_id, target_kind, interval_days, sha256, mode,
                  due_at, window_closes_at, p_pred
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                targets,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if session_id is None:
        raise RuntimeError("assignment did not produce a session")
    return _assignment_result(conn, session_id)


def resolve_targets(
    conn: sqlite3.Connection, *, experiment_id: int, now: int | None = None
) -> dict[str, int]:
    """Resolve due targets only when exactly one scorable play is in its window."""

    resolved_at = int(time.time()) if now is None else int(now)
    targets = conn.execute(
        """
        SELECT target.*, session.session_at AS session_at
        FROM experiment_targets target
        JOIN experiment_sessions session ON session.id = target.session_id
        WHERE session.experiment_id = ? AND target.status = 'pending'
          AND target.due_at <= ?
        ORDER BY target.id
        """,
        (experiment_id, resolved_at),
    ).fetchall()
    counts = {"resolved": 0, "missing": 0, "duplicate": 0, "pending": 0}
    with conn:
        for target in targets:
            if resolved_at < int(target["window_closes_at"]):
                counts["pending"] += 1
                continue
            status = "pending"
            note: str | None = None
            outcome: int | None = None
            play_id: int | None = None
            early_play = None
            if str(target["target_kind"]) == "transfer":
                early_play = conn.execute(
                    """
                    SELECT 1 FROM plays
                    WHERE sha256 = ? AND mode = ? AND is_course = 0
                      AND played_at >= ? AND played_at < ?
                    LIMIT 1
                    """,
                    (
                        target["sha256"], target["mode"],
                        target["session_at"], target["due_at"],
                    ),
                ).fetchone()
            if early_play is not None:
                status = "duplicate"
                note = "transfer chart played before evaluation window"
            else:
                scorable_plays = conn.execute(
                    """
                    WITH semantic_plays AS (
                      SELECT id, completed, played_at,
                             row_number() OVER (
                               PARTITION BY sha256, mode, played_at, playcount
                               ORDER BY CASE source
                                          WHEN 'collector' THEN 0
                                          WHEN 'official_ir' THEN 1
                                          WHEN 'daily_snapshot' THEN 2
                                          WHEN 'legacy_last_snapshot' THEN 3
                                          ELSE 4
                                        END,
                                        ingested_at DESC, id DESC
                             ) AS semantic_rank
                      FROM plays
                      WHERE sha256 = ? AND mode = ? AND is_course = 0
                        AND played_at >= ? AND played_at < ?
                        AND completed IS NOT NULL
                    )
                    SELECT id, completed FROM semantic_plays
                    WHERE semantic_rank = 1
                    ORDER BY played_at, id
                    """,
                    (
                        target["sha256"], target["mode"], target["due_at"],
                        target["window_closes_at"],
                    ),
                ).fetchall()
                if len(scorable_plays) > 1:
                    status, note = "duplicate", "multiple plays in evaluation window"
                elif len(scorable_plays) == 1:
                    candidate_play_id = int(scorable_plays[0]["id"])
                    already_used = conn.execute(
                        """
                        SELECT 1 FROM experiment_targets other
                        JOIN experiment_sessions session
                          ON session.id = other.session_id
                        WHERE session.experiment_id = ? AND other.id <> ?
                          AND other.resolved_play_id = ?
                        LIMIT 1
                        """,
                        (experiment_id, int(target["id"]), candidate_play_id),
                    ).fetchone()
                    if already_used is not None:
                        status = "duplicate"
                        note = "play already used by another target"
                    else:
                        status = "resolved"
                        play_id = candidate_play_id
                        outcome = int(bool(scorable_plays[0]["completed"]))
                else:
                    status = "missing"
                    note = "no scorable play in evaluation window"
            if status == "pending":
                counts[status] += 1
                continue
            conn.execute(
                """
                UPDATE experiment_targets
                SET status = ?, outcome = ?, resolved_play_id = ?,
                    resolved_at = ?, resolution_note = ?
                WHERE id = ?
                """,
                (status, outcome, play_id, resolved_at, note, target["id"]),
            )
            counts[status] += 1
    return counts


def list_targets(
    conn: sqlite3.Connection,
    *,
    experiment_id: int,
    status: str | None = "pending",
) -> dict[str, Any]:
    """List the exact revisit schedule without exposing beatoraja source data."""

    if status not in {None, "pending", "resolved", "missing", "duplicate"}:
        raise ValueError("unknown experiment target status")
    parameters: list[Any] = [experiment_id]
    status_clause = ""
    if status is not None:
        status_clause = " AND target.status = ?"
        parameters.append(status)
    rows = conn.execute(
        f"""
        SELECT target.id, target.target_kind, target.interval_days,
               target.sha256, target.mode, target.due_at,
               target.window_closes_at, target.p_pred, target.status,
               target.outcome, target.resolution_note,
               session.id AS session_id, session.session_key, session.arm,
               session.selection_probability, session.candidates_json,
               chart.title, chart.artist
        FROM experiment_targets target
        JOIN experiment_sessions session ON session.id = target.session_id
        LEFT JOIN charts chart ON chart.sha256 = target.sha256
        WHERE session.experiment_id = ?{status_clause}
        ORDER BY target.due_at, target.target_kind, target.id
        """,
        parameters,
    ).fetchall()
    return {
        "experiment_id": experiment_id,
        "status": "all" if status is None else status,
        "targets": [
            {
                "target_id": int(row["id"]),
                "session_id": int(row["session_id"]),
                "session_key": str(row["session_key"]),
                "arm": str(row["arm"]),
                "target_kind": str(row["target_kind"]),
                "interval_days": int(row["interval_days"]),
                "sha256": str(row["sha256"]),
                "mode": int(row["mode"]),
                "title": None if row["title"] is None else str(row["title"]),
                "artist": None if row["artist"] is None else str(row["artist"]),
                "due_at": int(row["due_at"]),
                "due_at_utc": _utc_iso(int(row["due_at"])),
                "window_closes_at": int(row["window_closes_at"]),
                "window_closes_at_utc": _utc_iso(
                    int(row["window_closes_at"])
                ),
                "p_pred": float(row["p_pred"]),
                "selection_probability": (
                    float(row["selection_probability"])
                    if str(row["target_kind"]) == "retention"
                    else _transfer_selection_probability(
                        str(row["candidates_json"])
                    )
                ),
                "status": str(row["status"]),
                "outcome": None if row["outcome"] is None else int(row["outcome"]),
                "resolution_note": (
                    None
                    if row["resolution_note"] is None
                    else str(row["resolution_note"])
                ),
            }
            for row in rows
        ],
    }


def report_experiment(conn: sqlite3.Connection, *, experiment_id: int) -> dict[str, Any]:
    """Return preregistered arm differences and Brier scores by target/interval."""

    experiment = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise ValueError(f"unknown experiment {experiment_id}")
    rows = conn.execute(
        """
        SELECT session.arm, target.target_kind, target.interval_days,
               target.p_pred, target.outcome
        FROM experiment_targets target
        JOIN experiment_sessions session ON session.id = target.session_id
        WHERE session.experiment_id = ? AND target.status = 'resolved'
        ORDER BY target.target_kind, target.interval_days, session.arm
        """,
        (experiment_id,),
    ).fetchall()
    grouped: dict[tuple[str, int, str], list[tuple[float, int]]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["target_kind"]), int(row["interval_days"]), str(row["arm"])), []
        ).append((float(row["p_pred"]), int(row["outcome"])))

    minimum = int(experiment["min_samples_per_arm"])
    results: list[dict[str, Any]] = []
    for kind in ("retention", "transfer"):
        for interval in INTERVAL_DAYS:
            arms: dict[str, dict[str, Any]] = {}
            for arm in ARMS:
                observations = grouped.get((kind, interval, arm), [])
                count = len(observations)
                arms[arm] = {
                    "n": count,
                    "success_rate": (
                        None if not count else sum(outcome for _, outcome in observations) / count
                    ),
                    "brier": (
                        None
                        if not count
                        else sum((probability - outcome) ** 2 for probability, outcome in observations)
                        / count
                    ),
                }
            conclusive = all(arms[arm]["n"] >= minimum for arm in ARMS)
            results.append(
                {
                    "target_kind": kind,
                    "interval_days": interval,
                    "status": "estimable" if conclusive else "inconclusive",
                    "reason": None if conclusive else "minimum samples per arm not reached",
                    "arms": arms,
                    "coach_minus_control_success_rate": (
                        arms["coach"]["success_rate"] - arms["control"]["success_rate"]
                        if conclusive
                        else None
                    ),
                    "coach_minus_control_brier": (
                        arms["coach"]["brier"] - arms["control"]["brier"]
                        if conclusive
                        else None
                    ),
                }
            )
    statuses = conn.execute(
        """
        SELECT target.status, count(*) AS n FROM experiment_targets target
        JOIN experiment_sessions session ON session.id = target.session_id
        WHERE session.experiment_id = ? GROUP BY target.status
        """,
        (experiment_id,),
    ).fetchall()
    return {
        "experiment_id": experiment_id,
        "name": str(experiment["name"]),
        "min_samples_per_arm": minimum,
        "target_statuses": {str(row["status"]): int(row["n"]) for row in statuses},
        "results": results,
    }
