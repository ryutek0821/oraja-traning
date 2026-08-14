"""Auditable two-arm self-experiment assignment and outcome evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
import time
from typing import Any, Mapping, Sequence


ARMS = ("coach", "control")
INTERVAL_DAYS = (1, 3, 7, 14)
DAY_SECONDS = 86_400


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


def _draw(seed: str, session_key: str, purpose: str) -> int:
    material = f"oraja-experiment-v1\0{seed}\0{session_key}\0{purpose}".encode()
    return int.from_bytes(hashlib.sha256(material).digest(), "big")


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
    return {
        "session_id": int(row["id"]),
        "experiment_id": int(row["experiment_id"]),
        "session_key": str(row["session_key"]),
        "arm": str(row["arm"]),
        "arm_probability": float(row["arm_probability"]),
        "selection_probability": float(row["selection_probability"]),
        "candidate_hash": str(row["candidate_hash"]),
        "selected": {
            "sha256": str(row["selected_sha256"]),
            "mode": int(row["selected_mode"]),
            "p_pred": float(row["selected_p_pred"]),
        },
        "transfer": {
            "sha256": str(row["transfer_sha256"]),
            "mode": int(row["transfer_mode"]),
            "p_pred": float(row["transfer_p_pred"]),
        },
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
    experiment = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise ValueError(f"unknown experiment {experiment_id}")
    if not int(experiment["starts_at"]) <= int(session_at) < int(experiment["ends_at"]):
        raise ValueError("session_at is outside the experiment assignment period")

    candidates = _normalise_candidates(candidate_sets)
    arm_identities = {
        (item.sha256, item.mode) for arm in ARMS for item in candidates[arm]
    }
    if any(
        (item.sha256, item.mode) in arm_identities for item in candidates["transfer"]
    ):
        raise ValueError("transfer candidates must be unpractised charts outside both arms")
    candidates_json = _canonical_candidates(candidates)
    candidate_hash = hashlib.sha256(candidates_json.encode()).hexdigest()
    existing = conn.execute(
        "SELECT id, session_at, candidate_hash FROM experiment_sessions "
        "WHERE experiment_id = ? AND session_key = ?",
        (experiment_id, session_key),
    ).fetchone()
    if existing is not None:
        if int(existing["session_at"]) != int(session_at) or str(existing["candidate_hash"]) != candidate_hash:
            raise ValueError("session_key was already assigned with different inputs")
        return _assignment_result(conn, int(existing["id"]))

    arm = ARMS[_draw(str(experiment["seed"]), session_key, "arm") % len(ARMS)]
    arm_candidates = candidates[arm]
    selected = arm_candidates[
        _draw(str(experiment["seed"]), session_key, "selection") % len(arm_candidates)
    ]
    transfer_candidates = candidates["transfer"]
    transfer = transfer_candidates[
        _draw(str(experiment["seed"]), session_key, "transfer")
        % len(transfer_candidates)
    ]
    assigned = int(time.time()) if assigned_at is None else int(assigned_at)
    selection_probability = (1.0 / len(ARMS)) * (1.0 / len(arm_candidates))

    with conn:
        cursor = conn.execute(
            """
            INSERT INTO experiment_sessions(
              experiment_id, session_key, session_at, arm, arm_probability,
              selection_probability, candidate_hash, candidates_json,
              selected_sha256, selected_mode, selected_p_pred,
              transfer_sha256, transfer_mode, transfer_p_pred, assigned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id, session_key, int(session_at), arm, 0.5,
                selection_probability, candidate_hash, candidates_json,
                selected.sha256, selected.mode, selected.p_pred,
                transfer.sha256, transfer.mode, transfer.p_pred, assigned,
            ),
        )
        session_id = int(cursor.lastrowid)
        targets = []
        for interval in INTERVAL_DAYS:
            due_at = int(session_at) + interval * DAY_SECONDS
            for kind, candidate in (("retention", selected), ("transfer", transfer)):
                targets.append(
                    (
                        session_id, kind, interval, candidate.sha256, candidate.mode,
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
    return _assignment_result(conn, session_id)


def resolve_targets(
    conn: sqlite3.Connection, *, experiment_id: int, now: int | None = None
) -> dict[str, int]:
    """Resolve due targets only when exactly one scorable play is in its window."""

    resolved_at = int(time.time()) if now is None else int(now)
    targets = conn.execute(
        """
        SELECT target.* FROM experiment_targets target
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
            plays = conn.execute(
                """
                SELECT id, completed FROM plays
                WHERE sha256 = ? AND mode = ? AND is_course = 0
                  AND played_at >= ? AND played_at < ?
                ORDER BY played_at, id
                """,
                (
                    target["sha256"], target["mode"], target["due_at"],
                    target["window_closes_at"],
                ),
            ).fetchall()
            scorable = [play for play in plays if play["completed"] is not None]
            status = "pending"
            note: str | None = None
            outcome: int | None = None
            play_id: int | None = None
            if len(plays) > 1:
                status, note = "duplicate", "multiple plays in evaluation window"
            elif len(scorable) == 1:
                status = "resolved"
                play_id = int(scorable[0]["id"])
                outcome = int(bool(scorable[0]["completed"]))
            elif resolved_at >= int(target["window_closes_at"]):
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
