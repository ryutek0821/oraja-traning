from __future__ import annotations

import hashlib
import json

import pytest

from oraja_training.cli import main
from oraja_training.db import store
from oraja_training.plan.experiment import (
    DAY_SECONDS,
    assign_session,
    report_experiment,
    resolve_targets,
    start_experiment,
)


BASE = 1_800_000_000


def _sha(value: int) -> str:
    return f"{value:064x}"


def _sets(offset: int = 0) -> dict[str, list[dict[str, object]]]:
    return {
        "coach": [
            {"sha256": _sha(offset + 1), "mode": 0, "p_pred": 0.8},
            {"sha256": _sha(offset + 2), "mode": 0, "p_pred": 0.6},
        ],
        "control": [
            {"sha256": _sha(offset + 3), "mode": 0, "p_pred": 0.5},
            {"sha256": _sha(offset + 4), "mode": 0, "p_pred": 0.4},
        ],
        "transfer": [
            {"sha256": _sha(offset + 5), "mode": 0, "p_pred": 0.55},
            {"sha256": _sha(offset + 6), "mode": 0, "p_pred": 0.45},
        ],
    }


def _start(conn, *, minimum: int = 2) -> int:
    return start_experiment(
        conn,
        name="two-week-test",
        seed="fixed-seed",
        starts_at=BASE,
        days=30,
        min_samples_per_arm=minimum,
        created_at=BASE,
    )["experiment_id"]


def _insert_play(conn, *, sha256: str, played_at: int, completed: int, serial: int) -> None:
    conn.execute(
        """
        INSERT INTO plays(
          sha256, mode, played_at, playcount, source_generation, source,
          clear, completed, is_course, payload_hash, ingested_at
        ) VALUES (?, 0, ?, ?, ?, 'collector', ?, ?, 0, ?, ?)
        """,
        (
            sha256, played_at, serial, serial, 4 if completed else 1,
            completed, f"experiment-{serial}", played_at,
        ),
    )


def test_assignment_is_deterministic_balanced_and_session_level(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        arms = []
        for index in range(200):
            result = assign_session(
                conn,
                experiment_id=experiment_id,
                session_key=f"session-{index}",
                session_at=BASE + index,
                candidate_sets=_sets(),
                assigned_at=BASE,
            )
            arms.append(result["arm"])
            assert result["arm_probability"] == 0.5
            assert result["selection_probability"] == 0.25
            assert len(result["candidate_hash"]) == 64
            assert conn.execute(
                "SELECT count(DISTINCT arm) FROM experiment_sessions WHERE id = ?",
                (result["session_id"],),
            ).fetchone()[0] == 1
        assert 75 <= arms.count("coach") <= 125

        first = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="session-0",
            session_at=BASE,
            candidate_sets={key: list(reversed(value)) for key, value in _sets().items()},
        )
        assert first["arm"] == arms[0]
        assert conn.execute("SELECT count(*) FROM experiment_sessions").fetchone()[0] == 200
        assert conn.execute("SELECT count(*) FROM experiment_targets").fetchone()[0] == 1600
    finally:
        conn.close()


def test_resolve_handles_unique_missing_duplicate_and_out_of_window(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="resolution",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE,
        )
        retention = assignment["selected"]["sha256"]
        transfer = assignment["transfer"]["sha256"]
        with conn:
            _insert_play(
                conn, sha256=retention, played_at=BASE + DAY_SECONDS + 10,
                completed=1, serial=1,
            )
            _insert_play(
                conn, sha256=transfer, played_at=BASE + 10,
                completed=1, serial=2,
            )
        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + DAY_SECONDS + 100
        )
        assert counts == {"resolved": 1, "missing": 0, "duplicate": 0, "pending": 1}

        with conn:
            _insert_play(
                conn, sha256=retention, played_at=BASE + 3 * DAY_SECONDS + 10,
                completed=1, serial=3,
            )
            _insert_play(
                conn, sha256=retention, played_at=BASE + 3 * DAY_SECONDS + 20,
                completed=0, serial=4,
            )
        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 4 * DAY_SECONDS
        )
        assert counts["duplicate"] == 1
        assert counts["missing"] == 2
        assert conn.execute(
            "SELECT outcome FROM experiment_targets WHERE status='resolved'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM experiment_targets WHERE status='duplicate'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_report_detects_synthetic_effect_and_enforces_minimum(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn, minimum=2)
        assignments: dict[str, list[dict[str, object]]] = {"coach": [], "control": []}
        index = 0
        while any(len(values) < 2 for values in assignments.values()):
            result = assign_session(
                conn,
                experiment_id=experiment_id,
                session_key=f"effect-{index}",
                session_at=BASE,
                candidate_sets=_sets(index * 10),
                assigned_at=BASE,
            )
            if len(assignments[result["arm"]]) < 2:
                assignments[result["arm"]].append(result)
            index += 1
        serial = 100
        with conn:
            for arm, sessions in assignments.items():
                for session in sessions:
                    for interval in (1, 3, 7, 14):
                        for key in ("selected", "transfer"):
                            serial += 1
                            _insert_play(
                                conn,
                                sha256=session[key]["sha256"],
                                played_at=BASE + interval * DAY_SECONDS + serial,
                                completed=1 if arm == "coach" else 0,
                                serial=serial,
                            )
        resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 15 * DAY_SECONDS
        )
        report = report_experiment(conn, experiment_id=experiment_id)
        day_one_retention = next(
            result for result in report["results"]
            if result["target_kind"] == "retention" and result["interval_days"] == 1
        )
        assert day_one_retention["status"] == "estimable"
        assert day_one_retention["coach_minus_control_success_rate"] == 1.0
        assert day_one_retention["arms"]["coach"]["brier"] < 0.2
        assert day_one_retention["arms"]["control"]["brier"] > 0.1

        sparse_id = start_experiment(
            conn, name="sparse", seed="sparse", starts_at=BASE,
            days=30, min_samples_per_arm=3, created_at=BASE,
        )["experiment_id"]
        sparse = report_experiment(conn, experiment_id=sparse_id)
        assert all(result["status"] == "inconclusive" for result in sparse["results"])
        assert all(result["coach_minus_control_brier"] is None for result in sparse["results"])
    finally:
        conn.close()


def test_cli_uses_only_assistant_db_and_candidate_json_is_read_only(tmp_path, capsys) -> None:
    source = tmp_path / "score.db"
    source.write_bytes(b"beatoraja-source-sentinel")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assistant = tmp_path / "assistant.db"
    assert main(
        [
            "experiment", "start", "--assistant-db", str(assistant),
            "--name", "cli", "--seed", "seed", "--starts-at", str(BASE),
        ]
    ) == 0
    experiment_id = json.loads(capsys.readouterr().out)["experiment_id"]
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps(_sets()), encoding="utf-8")
    candidate_before = candidates.read_bytes()
    assert main(
        [
            "experiment", "assign", "--assistant-db", str(assistant),
            "--experiment-id", str(experiment_id), "--session-key", "cli-1",
            "--session-at", str(BASE), "--candidates-json", str(candidates),
        ]
    ) == 0
    assert candidates.read_bytes() == candidate_before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_assignment_rejects_changed_inputs_for_existing_session(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        assign_session(
            conn, experiment_id=experiment_id, session_key="same",
            session_at=BASE, candidate_sets=_sets(), assigned_at=BASE,
        )
        with pytest.raises(ValueError, match="different inputs"):
            assign_session(
                conn, experiment_id=experiment_id, session_key="same",
                session_at=BASE, candidate_sets=_sets(100), assigned_at=BASE,
            )
    finally:
        conn.close()


def test_assignment_rejects_transfer_chart_present_in_training_arms(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        candidates = _sets()
        candidates["transfer"][0] = dict(candidates["coach"][0])
        with pytest.raises(ValueError, match="unpractised"):
            assign_session(
                conn,
                experiment_id=experiment_id,
                session_key="leaking-transfer",
                session_at=BASE,
                candidate_sets=candidates,
                assigned_at=BASE,
            )
    finally:
        conn.close()
