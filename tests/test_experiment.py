from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import pytest

from oraja_training.cli import main
from oraja_training.db import store
from oraja_training.plan.experiment import (
    DAY_SECONDS,
    assign_session,
    build_candidate_sets,
    list_targets,
    report_experiment,
    resolve_targets,
    start_experiment,
)


BASE = 1_800_000_000


def _sha(value: int) -> str:
    return f"{value:064x}"


def _sets(
    offset: int = 0, *, mode: int = 0
) -> dict[str, list[dict[str, object]]]:
    return {
        "coach": [
            {"sha256": _sha(offset + 1), "mode": mode, "p_pred": 0.8},
            {"sha256": _sha(offset + 2), "mode": mode, "p_pred": 0.6},
        ],
        "control": [
            {"sha256": _sha(offset + 3), "mode": mode, "p_pred": 0.5},
            {"sha256": _sha(offset + 4), "mode": mode, "p_pred": 0.4},
        ],
        "transfer": [
            {
                "sha256": _sha(offset + index),
                "mode": mode,
                "p_pred": 0.4 + index / 100,
            }
            for index in range(5, 10)
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


def _insert_play(
    conn,
    *,
    sha256: str,
    played_at: int,
    completed: int | None,
    serial: int,
    mode: int = 0,
    source: str = "collector",
    source_generation: int | None = None,
    playcount: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO plays(
          sha256, mode, played_at, playcount, source_generation, source,
          clear, completed, is_course, payload_hash, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            sha256,
            mode,
            played_at,
            serial if playcount is None else playcount,
            serial if source_generation is None else source_generation,
            source,
            4 if completed else 1,
            completed,
            f"experiment-{serial}",
            played_at,
        ),
    )


def test_resolve_ignores_non_scorable_play_with_unique_scorable_play(
    tmp_path,
) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="non-scorable-coexists",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE,
        )
        retention = assignment["selected"]
        with conn:
            _insert_play(
                conn,
                sha256=retention["sha256"],
                mode=retention["mode"],
                played_at=BASE + DAY_SECONDS + 10,
                completed=None,
                serial=1,
            )
            _insert_play(
                conn,
                sha256=retention["sha256"],
                mode=retention["mode"],
                played_at=BASE + DAY_SECONDS + 20,
                completed=1,
                serial=2,
            )

        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 2 * DAY_SECONDS
        )
        assert counts == {
            "resolved": 1,
            "missing": 1,
            "duplicate": 0,
            "pending": 0,
        }
        target = conn.execute(
            """
            SELECT target.status, target.outcome, play.completed
            FROM experiment_targets target
            LEFT JOIN plays play ON play.id = target.resolved_play_id
            WHERE target.session_id = ? AND target.target_kind = 'retention'
              AND target.interval_days = 1
            """,
            (assignment["session_id"],),
        ).fetchone()
        assert tuple(target) == ("resolved", 1, 1)
    finally:
        conn.close()


def test_resolve_deduplicates_official_ir_and_collector_copy(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="semantic-play-copy",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE,
        )
        retention = assignment["selected"]
        played_at = BASE + DAY_SECONDS + 10
        with conn:
            _insert_play(
                conn,
                sha256=retention["sha256"],
                mode=retention["mode"],
                played_at=played_at,
                completed=0,
                serial=1,
                source="official_ir",
                source_generation=-3,
                playcount=1,
            )
            _insert_play(
                conn,
                sha256=retention["sha256"],
                mode=retention["mode"],
                played_at=played_at,
                completed=1,
                serial=2,
                source="collector",
                source_generation=0,
                playcount=1,
            )

        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 2 * DAY_SECONDS
        )
        assert counts == {
            "resolved": 1,
            "missing": 1,
            "duplicate": 0,
            "pending": 0,
        }
        target = conn.execute(
            """SELECT target.status, target.outcome, play.source
               FROM experiment_targets target
               LEFT JOIN plays play ON play.id = target.resolved_play_id
               WHERE target.session_id = ? AND target.target_kind = 'retention'
                 AND target.interval_days = 1""",
            (assignment["session_id"],),
        ).fetchone()
        assert tuple(target) == ("resolved", 1, "collector")
    finally:
        conn.close()


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
                candidate_sets=_sets(index * 10),
                assigned_at=BASE,
            )
            arms.append(result["arm"])
            assert result["arm_probability"] == 0.5
            assert result["selection_probability"] == 0.25
            assert result["transfer_selection_probability"] == 0.2
            assert result["transfer"] == {
                key: value
                for key, value in result["transfers"][0].items()
                if key not in {"interval_days", "selection_probability"}
            }
            assert [item["interval_days"] for item in result["transfers"]] == [
                1, 3, 7, 14
            ]
            assert len(
                {(item["sha256"], item["mode"]) for item in result["transfers"]}
            ) == 4
            assert all(
                item["selection_probability"] == 0.2
                for item in result["transfers"]
            )
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
        assert first["transfers"] == assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="session-0",
            session_at=BASE,
            candidate_sets=_sets(),
        )["transfers"]
        assert conn.execute("SELECT count(*) FROM experiment_sessions").fetchone()[0] == 200
        assert conn.execute("SELECT count(*) FROM experiment_targets").fetchone()[0] == 1600
    finally:
        conn.close()


def test_assignment_serializes_transfer_reservations_across_connections(tmp_path) -> None:
    path = tmp_path / "assistant.db"
    conn = store.init(path)
    try:
        experiment_id = _start(conn)
    finally:
        conn.close()
    barrier = threading.Barrier(2)

    def assign(session_key: str) -> dict[str, object] | str:
        worker = store.init(path)
        try:
            barrier.wait()
            try:
                return assign_session(
                    worker,
                    experiment_id=experiment_id,
                    session_key=session_key,
                    session_at=BASE,
                    candidate_sets=_sets(),
                    assigned_at=BASE,
                )
            except ValueError as error:
                return str(error)
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(assign, ("concurrent-a", "concurrent-b")))

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, str)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "four distinct charts" in failures[0]
    conn = store.init(path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM experiment_sessions"
        ).fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT count(DISTINCT sha256 || ':' || mode)
            FROM experiment_targets WHERE target_kind = 'transfer'
            """
        ).fetchone()[0] == 4
    finally:
        conn.close()


def test_candidate_sets_use_latest_focus_and_same_level_random_pool(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        personal = [
            {
                "sha256": _sha(index),
                "title": f"Chart {index}",
                "artist": "Artist",
                "table_id": "satellite",
                "source_level": "sl5",
                "playcount": 0,
                "clear": 0,
                "primary_axis": "scratch" if index == 7 else "density",
                "p_complete": probability,
            }
            for index, probability in (
                (1, 0.8), (2, 0.6), (3, 0.7), (4, 0.65), (5, 0.62),
                (6, 0.58), (7, 0.56),
            )
        ]
        payload = {
            "queue": [
                {
                    "sha256": _sha(1),
                    "category": "02 FOCUS-A",
                    "attempt": 1,
                    "table_id": "satellite",
                    "source_level": "sl5",
                }
            ],
            "personal": personal,
        }
        with conn:
            conn.execute(
                "INSERT INTO sessions(created_at, arm, slots_json) VALUES (?, ?, ?)",
                (BASE, "model", json.dumps(payload)),
            )

        candidates = build_candidate_sets(
            conn, experiment_id=experiment_id, session_key="menu-session"
        )

        assert [item["sha256"] for item in candidates["coach"]] == [_sha(1)]
        assert len(candidates["control"]) == 1
        assert len(candidates["transfer"]) == 5
        assert {
            candidates["control"][0]["sha256"],
            *(item["sha256"] for item in candidates["transfer"]),
        } == {_sha(index) for index in range(2, 8)}
        assert all(item["title"].startswith("Chart") for values in candidates.values() for item in values)

        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="menu-session",
            session_at=BASE,
            candidate_sets=candidates,
            assigned_at=BASE,
        )
        assert assignment["transfer_selection_probability"] == 0.2
        assert len({item["sha256"] for item in assignment["transfers"]}) == 4
        stored_candidates = json.loads(
            conn.execute(
                "SELECT candidates_json FROM experiment_sessions WHERE id = ?",
                (assignment["session_id"],),
            ).fetchone()[0]
        )
        assert len(stored_candidates["transfer"]) == 5
        retry_candidates = build_candidate_sets(
            conn, experiment_id=experiment_id, session_key="menu-session"
        )
        retried = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="menu-session",
            session_at=BASE,
            candidate_sets=retry_candidates,
            assigned_at=BASE + 1,
        )
        assert retried["session_id"] == assignment["session_id"]
        assert conn.execute(
            "SELECT count(*) FROM experiment_sessions"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_menu_candidates_preserve_mode_one_through_target_resolution(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        personal = [
            {
                "sha256": _sha(index),
                "mode": 1,
                "title": f"LN Chart {index}",
                "artist": "Artist",
                "table_id": "satellite",
                "source_level": "sl5",
                "playcount": 0,
                "clear": 0,
                "primary_axis": "scratch" if index == 7 else "density",
                "p_complete": 0.5 + index / 100,
            }
            for index in range(1, 8)
        ]
        payload = {
            "queue": [
                {
                    "sha256": _sha(1),
                    "mode": 1,
                    "category": "02 FOCUS-A",
                    "attempt": 1,
                }
            ],
            "personal": personal,
        }
        with conn:
            conn.execute(
                "INSERT INTO sessions(created_at, arm, slots_json) VALUES (?, ?, ?)",
                (BASE, "model", json.dumps(payload)),
            )

        candidates = build_candidate_sets(
            conn, experiment_id=experiment_id, session_key="mode-one"
        )
        assert all(
            item["mode"] == 1
            for group in candidates.values()
            for item in group
        )
        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="mode-one",
            session_at=BASE,
            candidate_sets=candidates,
            assigned_at=BASE,
        )
        day_one = assignment["transfers"][0]
        with conn:
            _insert_play(
                conn,
                sha256=day_one["sha256"],
                mode=0,
                played_at=BASE + DAY_SECONDS + 10,
                completed=0,
                serial=1,
            )
            _insert_play(
                conn,
                sha256=day_one["sha256"],
                mode=1,
                played_at=BASE + DAY_SECONDS + 20,
                completed=1,
                serial=2,
            )

        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 2 * DAY_SECONDS
        )
        assert counts == {
            "resolved": 1,
            "missing": 1,
            "duplicate": 0,
            "pending": 0,
        }
        resolved = list_targets(
            conn, experiment_id=experiment_id, status="resolved"
        )["targets"]
        assert [(item["target_kind"], item["mode"], item["outcome"]) for item in resolved] == [
            ("transfer", 1, 1)
        ]
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
        schedule = list_targets(conn, experiment_id=experiment_id)
        assert len(schedule["targets"]) == 8
        assert {target["interval_days"] for target in schedule["targets"]} == {
            1, 3, 7, 14
        }
        assert all(target["status"] == "pending" for target in schedule["targets"])
        assert {
            target["selection_probability"]
            for target in schedule["targets"]
            if target["target_kind"] == "transfer"
        } == {0.2}
        assert all(target["due_at_utc"].endswith("+00:00") for target in schedule["targets"])
        assert all(
            target["window_closes_at_utc"].endswith("+00:00")
            for target in schedule["targets"]
        )
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
        assert counts == {"resolved": 0, "missing": 0, "duplicate": 0, "pending": 2}

        counts = resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 2 * DAY_SECONDS
        )
        assert counts == {"resolved": 1, "missing": 0, "duplicate": 1, "pending": 0}

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
        assert counts["missing"] == 1
        assert conn.execute(
            "SELECT count(*) FROM experiment_targets WHERE status='missing'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT outcome FROM experiment_targets WHERE status='resolved'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM experiment_targets WHERE status='duplicate'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_resolve_excludes_transfer_played_before_its_window(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        assignment = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="early-transfer",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE,
        )
        day_fourteen = next(
            item for item in assignment["transfers"]
            if item["interval_days"] == 14
        )
        with conn:
            _insert_play(
                conn,
                sha256=day_fourteen["sha256"],
                mode=day_fourteen["mode"],
                played_at=BASE + 2 * DAY_SECONDS,
                completed=1,
                serial=1,
            )
            _insert_play(
                conn,
                sha256=day_fourteen["sha256"],
                mode=day_fourteen["mode"],
                played_at=BASE + 14 * DAY_SECONDS + 10,
                completed=1,
                serial=2,
            )

        resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 15 * DAY_SECONDS
        )
        target = conn.execute(
            """
            SELECT status, outcome, resolved_play_id, resolution_note
            FROM experiment_targets
            WHERE session_id = ? AND target_kind = 'transfer'
              AND interval_days = 14
            """,
            (assignment["session_id"],),
        ).fetchone()
        assert tuple(target) == (
            "duplicate",
            None,
            None,
            "transfer chart played before evaluation window",
        )
        transfer_day_fourteen = next(
            result for result in report_experiment(
                conn, experiment_id=experiment_id
            )["results"]
            if result["target_kind"] == "transfer"
            and result["interval_days"] == 14
        )
        assert transfer_day_fourteen["arms"][assignment["arm"]]["n"] == 0
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
                        transfer = next(
                            item for item in session["transfers"]
                            if item["interval_days"] == interval
                        )
                        for candidate in (session["selected"], transfer):
                            serial += 1
                            _insert_play(
                                conn,
                                sha256=candidate["sha256"],
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


def test_report_detects_no_synthetic_arm_effect(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn, minimum=2)
        assignments: dict[str, list[dict[str, object]]] = {"coach": [], "control": []}
        index = 0
        while any(len(values) < 2 for values in assignments.values()):
            result = assign_session(
                conn,
                experiment_id=experiment_id,
                session_key=f"no-effect-{index}",
                session_at=BASE,
                candidate_sets=_sets(index * 10),
                assigned_at=BASE,
            )
            if len(assignments[result["arm"]]) < 2:
                assignments[result["arm"]].append(result)
            index += 1

        serial = 1_000
        with conn:
            for sessions in assignments.values():
                for session in sessions:
                    for interval in (1, 3, 7, 14):
                        transfer = next(
                            item for item in session["transfers"]
                            if item["interval_days"] == interval
                        )
                        for candidate in (session["selected"], transfer):
                            serial += 1
                            _insert_play(
                                conn,
                                sha256=candidate["sha256"],
                                played_at=BASE + interval * DAY_SECONDS + serial,
                                completed=1,
                                serial=serial,
                            )
        resolve_targets(
            conn, experiment_id=experiment_id, now=BASE + 15 * DAY_SECONDS
        )
        report = report_experiment(conn, experiment_id=experiment_id)
        assert all(result["status"] == "estimable" for result in report["results"])
        assert all(
            result["coach_minus_control_success_rate"] == 0.0
            for result in report["results"]
        )
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
    assignment = json.loads(capsys.readouterr().out)
    assert [item["interval_days"] for item in assignment["transfers"]] == [
        1, 3, 7, 14
    ]
    assert len({item["sha256"] for item in assignment["transfers"]}) == 4
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
        expanded = _sets()
        expanded["coach"].append(
            {"sha256": _sha(99), "mode": 0, "p_pred": 0.7}
        )
        with pytest.raises(ValueError, match="different inputs"):
            assign_session(
                conn,
                experiment_id=experiment_id,
                session_key="same",
                session_at=BASE,
                candidate_sets=expanded,
                assigned_at=BASE,
            )
    finally:
        conn.close()


def test_legacy_single_transfer_assignment_remains_idempotent(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        legacy_candidates = _sets()
        legacy_candidates["transfer"] = legacy_candidates["transfer"][:1]
        candidates_json = json.dumps(
            legacy_candidates,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        selected = legacy_candidates["coach"][0]
        transfer = legacy_candidates["transfer"][0]
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
                    experiment_id,
                    "legacy",
                    BASE,
                    "coach",
                    0.5,
                    0.25,
                    hashlib.sha256(candidates_json.encode()).hexdigest(),
                    candidates_json,
                    selected["sha256"],
                    selected["mode"],
                    selected["p_pred"],
                    transfer["sha256"],
                    transfer["mode"],
                    transfer["p_pred"],
                    BASE,
                ),
            )
            session_id = int(cursor.lastrowid)
            for interval in (1, 3, 7, 14):
                due_at = BASE + interval * DAY_SECONDS
                for kind, candidate in (
                    ("retention", selected),
                    ("transfer", transfer),
                ):
                    conn.execute(
                        """
                        INSERT INTO experiment_targets(
                          session_id, target_kind, interval_days, sha256, mode,
                          due_at, window_closes_at, p_pred
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            kind,
                            interval,
                            candidate["sha256"],
                            candidate["mode"],
                            due_at,
                            due_at + DAY_SECONDS,
                            candidate["p_pred"],
                        ),
                    )

        retried = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="legacy",
            session_at=BASE,
            candidate_sets=legacy_candidates,
            assigned_at=BASE + 1,
        )
        assert retried["session_id"] == session_id
        assert retried["transfer_selection_probability"] == 1.0
        assert len({item["sha256"] for item in retried["transfers"]}) == 1
        assert build_candidate_sets(
            conn, experiment_id=experiment_id, session_key="legacy"
        )["transfer"] == legacy_candidates["transfer"]
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 10
        assert conn.execute(
            "SELECT count(*) FROM experiment_sessions"
        ).fetchone()[0] == 1
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


def test_assignment_rejects_overlap_between_coach_and_control(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        candidates = _sets()
        candidates["control"][0] = dict(candidates["coach"][0])
        with pytest.raises(ValueError, match="coach and control"):
            assign_session(
                conn,
                experiment_id=experiment_id,
                session_key="overlapping-arms",
                session_at=BASE,
                candidate_sets=candidates,
                assigned_at=BASE,
            )
    finally:
        conn.close()


def test_assignment_requires_one_unused_transfer_chart_per_interval(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        candidates = _sets()
        candidates["transfer"] = candidates["transfer"][:3]
        with pytest.raises(ValueError, match="four distinct charts"):
            assign_session(
                conn,
                experiment_id=experiment_id,
                session_key="too-few-transfer-charts",
                session_at=BASE,
                candidate_sets=candidates,
                assigned_at=BASE,
            )
    finally:
        conn.close()


def test_assignment_filters_practised_and_reserved_candidates(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        experiment_id = _start(conn)
        with conn:
            _insert_play(
                conn,
                sha256=_sha(5),
                played_at=BASE - 100,
                completed=1,
                serial=1,
            )
        first = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="first",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE,
        )
        assert {item["sha256"] for item in first["transfers"]} == {
            _sha(index) for index in range(6, 10)
        }
        retried_first = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="first",
            session_at=BASE,
            candidate_sets=_sets(),
            assigned_at=BASE + 1,
        )
        assert retried_first["session_id"] == first["session_id"]
        assert first["input_candidate_hash"] != first["candidate_hash"]

        second_candidates = _sets()
        second_candidates["transfer"].extend(
            {
                "sha256": _sha(index),
                "mode": 0,
                "p_pred": 0.5,
            }
            for index in range(10, 15)
        )
        second = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="second",
            session_at=BASE + 1,
            candidate_sets=second_candidates,
            assigned_at=BASE,
        )
        retried_second = assign_session(
            conn,
            experiment_id=experiment_id,
            session_key="second",
            session_at=BASE + 1,
            candidate_sets=second_candidates,
            assigned_at=BASE + 1,
        )
        assert retried_second["session_id"] == second["session_id"]
        first_used = {
            (first["selected"]["sha256"], first["selected"]["mode"]),
            *((item["sha256"], item["mode"]) for item in first["transfers"]),
        }
        assert (second["selected"]["sha256"], second["selected"]["mode"]) not in first_used
        assert all(
            (item["sha256"], item["mode"]) not in first_used
            for item in second["transfers"]
        )
    finally:
        conn.close()
