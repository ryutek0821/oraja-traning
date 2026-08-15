from __future__ import annotations

import json
import random
import time
from datetime import datetime

import pytest

from oraja_training.db import store
from oraja_training.db.recommendation_adapter import SQLiteRecommendationRepository
from oraja_training.domain import ProfileContext, RecommendationInput
from oraja_training.plan.menu import (
    WARMUP_FEATURES,
    _load_candidates,
    _select_warmup,
    build_session,
    build_session_from_input,
    next_training_date,
    recommendation_output,
    training_day,
    write_export,
)


NOW = 1_800_000_000


def _record(
    index: int,
    *,
    table_id: str = "satellite",
    level: str | int = 0,
    clear: int = 6,
    playcount: int = 4,
    sha256: str | None = None,
    recent_successes: int = 0,
    recent_failures: int = 0,
    recent_bp_rate: float | None = None,
    stop_count: int = 0,
    burst_max: float = 12.0,
) -> dict[str, object]:
    return {
        "sha256": sha256 or f"{index:064x}",
        "md5": f"{index:032x}",
        "title": f"Warmup Chart {index}",
        "artist": "Artist",
        "notes": 1800,
        "table_id": table_id,
        "level": str(level),
        "clear": clear,
        "playcount": playcount,
        "last_played": NOW - 86_400,
        "minbp": 20,
        "recent_successes": recent_successes,
        "recent_failures": recent_failures,
        "recent_played_at": NOW - 60 if recent_successes or recent_failures else 0,
        "recent_second_played_at": NOW - 120 if recent_successes >= 2 else 0,
        "recent_bp_rate": recent_bp_rate,
        "density": 20.0,
        "scratch": 5.0,
        "model_scratch": 0.05,
        "ln": 0.05,
        "soflan": float(stop_count),
        "density_p90": 12.0,
        "end_density": 12.0,
        "burst_max": burst_max,
        "scratch_rate": 0.05,
        "scratch_combo_rate": 0.02,
        "ln_rate": 0.05,
        "soflan_var": 0.0,
        "soflan_changes": 0,
        "stop_count": stop_count,
        "chart_seconds": 120.0,
        "rhythm_family": 1,
        "avg_chord": 1.3,
        "chord_ge3": 0.08,
        "micro_rate": 0.04,
        "long_jack_rate": 0.01,
        "grid_bpm": 160.0,
        "stream_sec": 8.0,
        "last_kill": 1.0,
        "practice_low": 1,
    }


def _warmup_records() -> tuple[dict[str, object], ...]:
    rows = [
        _record(index, level=index, clear=6 if index <= 7 else 1)
        for index in range(16)
    ]
    rows.extend(
        (
            _record(100, level=4, clear=4, playcount=1),
            _record(
                101,
                level=4,
                clear=4,
                recent_successes=2,
                recent_bp_rate=0.02,
            ),
            _record(102, level=4, stop_count=1),
            _record(103, level=4, burst_max=100.0),
            _record(104, level="★???"),
        )
    )
    return tuple(rows)
def test_training_day_uses_local_0400_across_dst_and_month_boundaries() -> None:
    assert training_day(datetime.fromisoformat("2026-08-14T03:59:59+09:00"), "Asia/Tokyo").isoformat() == "2026-08-13"
    assert training_day(datetime.fromisoformat("2026-08-14T04:00:00+09:00"), "Asia/Tokyo").isoformat() == "2026-08-14"
    assert training_day(datetime.fromisoformat("2026-09-01T03:59:59+09:00"), "Asia/Tokyo").isoformat() == "2026-08-31"

    profile = ProfileContext("profile-a", "Alice", "America/New_York")
    assert next_training_date(datetime.fromisoformat("2026-03-08T03:59:59-04:00"), profile).isoformat() == "2026-03-08"
    assert next_training_date(datetime.fromisoformat("2026-03-08T04:00:00-04:00"), profile).isoformat() == "2026-03-09"
    assert next_training_date(datetime.fromisoformat("2026-11-01T03:59:59-05:00"), profile).isoformat() == "2026-11-01"
    assert next_training_date(datetime.fromisoformat("2026-11-01T04:00:00-05:00"), profile).isoformat() == "2026-11-02"


def _assistant(tmp_path):
    conn = store.init(tmp_path / "assistant.db")
    now = int(time.time())
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO daily_imports(
              imported_at, effective_date, score_sha256, scoredatalog_sha256,
              baseline_judged, cumulative_playcount
            ) VALUES (?, '2026-08-09', 'score', 'log', 1080762, 507)
            """,
            (now,),
        )
        import_id = cursor.lastrowid
        for index in range(180):
            sha = f"{index:064x}"
            md5 = f"{index:032x}"
            level = index % 13
            clear = 6 if level <= 3 else 4 if level <= 6 else 1
            playcount = 4 if clear >= 4 else index % 3
            conn.execute(
                "INSERT INTO charts VALUES (?, ?, ?, ?, ?, 7, ?, ?)",
                (sha, md5, f"Chart {index}", f"Artist {index % 30}", 1800 + index % 900, f"C:/{index}.bms", now),
            )
            conn.execute(
                "INSERT INTO table_entries VALUES ('satellite', ?, ?, ?, ?, ?)",
                (str(level), sha, md5, f"Chart {index}", now),
            )
            conn.execute(
                "INSERT INTO score_state VALUES (?, 0, ?, 1000, 50, 500, ?, ?, ?, ?, ?)",
                (sha, clear, 1800 + index % 900, playcount, now - (index % 20) * 86400, import_id, f"hash-{index}"),
            )
            conn.execute(
                """
                INSERT INTO chart_features VALUES (
                  ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    sha, 8 + index % 8, 12 + index % 10, 18 + index % 15,
                    8, 24 + index % 10, (index % 20) / 100,
                    4 + index % 8, (index % 10) / 10, (index % 12) / 20,
                    index % 5, index % 4, index % 3, 120, 1800 + index % 900,
                ),
            )
    return conn


def test_session_is_deterministic_and_reaches_budget(tmp_path) -> None:
    conn = _assistant(tmp_path)
    try:
        first = build_session(conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200)
        second = build_session(conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200)
    finally:
        conn.close()

    assert first == second
    assert first.core_expected_judged >= 100_000
    assert first.reserve_expected_judged >= 10_000
    assert first.baseline_judged == 1_080_762
    assert any(item.is_exploration for item in first.queue)

    positions: dict[str, list[int]] = {}
    for index, item in enumerate(first.queue):
        if item.category in {"02 FOCUS-A", "04 FOCUS-B"}:
            positions.setdefault(item.sha256, []).append(index)
    assert positions
    assert all(len(value) == 2 for value in positions.values())
    assert all(value[1] - value[0] - 1 == 3 for value in positions.values())


def test_personal_menu_payload_preserves_mode_two_score_state(tmp_path) -> None:
    conn = _assistant(tmp_path)
    sha256 = f"{0:064x}"
    with conn:
        conn.execute(
            "UPDATE score_state SET mode = 2 WHERE sha256 = ? AND mode = 0",
            (sha256,),
        )
    try:
        session = build_session(
            conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200
        )
        candidate = next(item for item in session.personal if item.sha256 == sha256)
        assert candidate.mode == 2
        assert all(item.mode in {0, 1, 2} for item in session.queue)
        SQLiteRecommendationRepository(conn).save_output(
            recommendation_output(session)
        )
        payload = json.loads(
            conn.execute(
                "SELECT slots_json FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        persisted = next(
            item for item in payload["personal"] if item["sha256"] == sha256
        )
        assert persisted["mode"] == 2
    finally:
        conn.close()


def test_pending_transfer_target_is_excluded_from_future_menus(tmp_path) -> None:
    conn = _assistant(tmp_path)
    reserved_sha256 = f"{0:064x}"
    with conn:
        experiment = conn.execute(
            """
            INSERT INTO experiments(
              name, seed, starts_at, ends_at, min_samples_per_arm, created_at
            ) VALUES ('reserved', 'seed', 1, 2000000000, 1, 1)
            """
        )
        assignment = conn.execute(
            """
            INSERT INTO experiment_sessions(
              experiment_id, session_key, session_at, arm, arm_probability,
              selection_probability, candidate_hash, candidates_json,
              selected_sha256, selected_mode, selected_p_pred,
              transfer_sha256, transfer_mode, transfer_p_pred, assigned_at,
              input_candidate_hash
            ) VALUES (?, 'reserved', 1, 'coach', 0.5, 0.5, 'hash', '{}',
                      ?, 0, 0.5, ?, 0, 0.5, 1, 'input-hash')
            """,
            (int(experiment.lastrowid), reserved_sha256, reserved_sha256),
        )
        conn.execute(
            """
            INSERT INTO experiment_targets(
              session_id, target_kind, interval_days, sha256, mode,
              due_at, window_closes_at, p_pred
            ) VALUES (?, 'transfer', 14, ?, 0, 100, 200, 0.5)
            """,
            (int(assignment.lastrowid), reserved_sha256),
        )
    try:
        session = build_session(
            conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200
        )
        assert reserved_sha256 not in {item.sha256 for item in session.personal}
        assert reserved_sha256 not in {item.sha256 for item in session.queue}
    finally:
        conn.close()


def test_export_has_unique_table_entries_and_repeated_queue(tmp_path) -> None:
    conn = _assistant(tmp_path)
    try:
        session = build_session(conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200)
    finally:
        conn.close()
    output = tmp_path / "export"
    write_export(session, output)

    today = json.loads((output / "table/today/score.json").read_text())
    queue = json.loads((output / "session.json").read_text())["queue"]
    assert len({row["sha256"] for row in today}) == len(today)
    assert len(queue) > len(today)
    assert json.loads((output / "manifest.json").read_text())["baseline_judged"] == 1_080_762


def test_validated_model_is_used_and_reported(tmp_path) -> None:
    conn = _assistant(tmp_path)
    params = {
        "weights": [0.0, 0.0, 0.0, 0.0],
        "means": [0.0, 0.0, 0.0],
        "scales": [1.0, 1.0, 1.0],
        "feature_names": [
            "table_completion_margin", "density_p99", "scratch_rate"
        ],
    }
    with conn:
        conn.execute(
            "INSERT INTO model_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("observed_completion", 3, 1, json.dumps(params), 240, 0.4, 0.2, 0.5),
        )
    try:
        session = build_session(conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200)
    finally:
        conn.close()

    assert session.model_status == "validated_model"
    assert session.model_version == 3
    assert any(candidate.p_complete == 0.5 for candidate in session.personal)
    output = tmp_path / "export-model"
    write_export(session, output)
    review = json.loads((output / "review/latest.json").read_text())
    assert review["status"] == "validated_model"


def test_legacy_raw_level_model_is_not_reused_as_normalized_model(tmp_path) -> None:
    conn = _assistant(tmp_path)
    params = {
        "weights": [0.0, 0.0, 0.0, 0.0],
        "means": [0.0, 0.0, 0.0],
        "scales": [1.0, 1.0, 1.0],
    }
    with conn:
        conn.execute(
            "INSERT INTO model_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("observed_completion", 3, 1, json.dumps(params), 240, 0.4, 0.2, 0.5),
        )
    try:
        session = build_session(
            conn, menu_date="2026-08-10", clock=lambda: 1_786_291_200
        )
    finally:
        conn.close()

    assert session.model_status == "cold_start"
    assert session.model_version == 0


def test_warmup_requires_evidence_and_rejects_unsafe_or_unknown_charts() -> None:
    candidates, _, _, frontiers = _load_candidates(_warmup_records(), None)

    items = _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(1),
        readiness="normal",
        adjustment=0,
    )

    selected = {item.sha256 for item in items}
    assert f"{100:064x}" not in selected  # one EASY lamp is not familiarity
    assert f"{101:064x}" in selected  # repeated, recent low-BP clears are evidence
    assert f"{102:064x}" not in selected  # stop sequence
    assert f"{103:064x}" not in selected  # extreme burst percentile
    assert f"{104:064x}" not in selected  # non-numeric difficulty such as ★???
    assert items and all(item.target == "COMFORT" for item in items)
    assert all(item.band == "WARMUP" for item in items)


def test_warmup_requires_two_successes_inside_the_recent_window() -> None:
    rows = list(_warmup_records())
    old_evidence = {
        **_record(
            105,
            level=4,
            clear=4,
            recent_successes=2,
            recent_bp_rate=0.02,
        ),
        "recent_second_played_at": NOW - 31 * 86_400,
    }
    rows.append(old_evidence)
    candidates, _, _, frontiers = _load_candidates(tuple(rows), None)

    items = _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(1),
        readiness="normal",
        adjustment=0,
    )

    assert f"{105:064x}" not in {item.sha256 for item in items}


def test_recent_full_play_failed_lamps_are_not_warmup_successes(tmp_path) -> None:
    conn = _assistant(tmp_path)
    sha256 = f"{4:064x}"
    try:
        with conn:
            for index, played_at in enumerate((NOW - 120, NOW - 60), start=1):
                conn.execute(
                    """INSERT INTO plays(
                        sha256, mode, played_at, playcount, source_generation,
                        source, clear, completed, bp_rate, is_course,
                        payload_hash, ingested_at
                       ) VALUES (?, 0, ?, ?, 0, 'collector', 1, 1, 0.02, 0, ?, ?)""",
                    (sha256, played_at, index, f"failed-lamp-{index}", played_at),
                )

        record = next(
            value
            for value in SQLiteRecommendationRepository(conn).load_input(
                ProfileContext()
            ).candidates
            if value["sha256"] == sha256
        )
        assert record["recent_successes"] == 0
        assert record["recent_failures"] == 2
    finally:
        conn.close()


def test_collector_and_daily_snapshot_copy_count_as_one_recent_play(
    tmp_path,
) -> None:
    conn = _assistant(tmp_path)
    sha256 = f"{4:064x}"
    played_at = NOW - 60
    try:
        with conn:
            for generation, source in ((0, "collector"), (-2, "daily_snapshot")):
                conn.execute(
                    """INSERT INTO plays(
                        sha256, mode, played_at, playcount, source_generation,
                        source, clear, completed, bp_rate, is_course,
                        payload_hash, ingested_at
                       ) VALUES (?, 0, ?, 1, ?, ?, 4, 1, 0.02, 0, ?, ?)""",
                    (
                        sha256,
                        played_at,
                        generation,
                        source,
                        "same-play",
                        played_at,
                    ),
                )

        record = next(
            value
            for value in SQLiteRecommendationRepository(conn).load_input(
                ProfileContext()
            ).candidates
            if value["sha256"] == sha256
        )
        assert record["recent_successes"] == 1
        assert record["recent_failures"] == 0
        assert record["recent_played_at"] == played_at
        assert record["recent_second_played_at"] == 0
    finally:
        conn.close()


def test_missing_pattern_analysis_gets_a_penalty_and_visible_warning() -> None:
    rows = list(_warmup_records())
    safe = next(row for row in rows if row["sha256"] == f"{4:064x}")
    safe["micro_rate"] = None
    missing_core = next(row for row in rows if row["sha256"] == f"{5:064x}")
    missing_core["density_p90"] = None
    candidates, _, _, frontiers = _load_candidates(tuple(rows), None)

    items = _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(1),
        readiness="normal",
        adjustment=0,
    )
    selected = {item.sha256 for item in items}
    assert f"{5:064x}" not in selected
    candidate = next(value for value in candidates if value.sha256 == f"{4:064x}")
    penalized = [
        candidate.warmup_feature_scores[feature]
        if candidate.warmup_features[feature] is not None else 0.55
        for feature in WARMUP_FEATURES
    ]
    assert candidate.warmup_load == pytest.approx(
        0.65 * max(penalized) + 0.35 * sum(penalized) / len(penalized)
    )

    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            tuple(rows),
            table_sources=({"table_id": "satellite", "last_error": None},),
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )
    assert any("pattern analysis missing" in warning for warning in session.table_warnings)


def test_satellite_warmup_uses_hard_sl3_to_sl5_not_single_easy_sl10_to_sl11() -> None:
    rows = [
        _record(index, level=f"sl{index}", clear=6 if index <= 5 else 1)
        for index in range(1, 12)
    ]
    rows.extend(
        _record(200 + index, level=f"sl{level}", clear=4, playcount=1)
        for index, level in enumerate((10, 10, 11, 11))
    )
    candidates, _, _, frontiers = _load_candidates(tuple(rows), None)

    items = _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(2),
        readiness="normal",
        adjustment=0,
    )

    levels = [int(item.source_level.removeprefix("sl")) for item in items]
    assert levels
    assert set(levels).issubset({3, 4, 5})
    assert levels == sorted(levels)


@pytest.mark.parametrize(
    ("feature", "value"),
    (
        ("density_p90", 100.0),
        ("end_density", 100.0),
        ("burst_max", 100.0),
        ("scratch_rate", 1.0),
        ("scratch_combo_rate", 1.0),
        ("ln_rate", 1.0),
        ("soflan_var", 100.0),
        ("soflan_changes", 10),
        ("stop_count", 1),
        ("micro_rate", 1.0),
        ("long_jack_rate", 1.0),
        ("avg_chord", 8.0),
        ("chord_ge3", 1.0),
        ("grid_bpm", 260.0),
        ("stream_sec", 60.0),
        ("last_kill", 2.5),
        ("chart_seconds", 300.0),
    ),
)
def test_each_extreme_pattern_feature_is_excluded_from_warmup(
    feature: str, value: float
) -> None:
    rows = list(_warmup_records())
    candidate = next(row for row in rows if row["sha256"] == f"{4:064x}")
    candidate[feature] = value
    candidates, _, _, frontiers = _load_candidates(tuple(rows), None)

    items = _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(1),
        readiness="normal",
        adjustment=0,
    )

    assert f"{4:064x}" not in {item.sha256 for item in items}


@pytest.mark.parametrize(
    ("feature", "value"),
    (("end_density", 19.0), ("burst_max", 22.0)),
)
def test_non_flat_density_shape_is_excluded_even_when_every_chart_ties(
    feature: str, value: float
) -> None:
    rows = [
        _record(index, level=index, clear=6 if index <= 7 else 1)
        for index in range(16)
    ]
    for row in rows:
        row[feature] = value
    candidates, _, _, frontiers = _load_candidates(tuple(rows), None)

    assert _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(1),
        readiness="normal",
        adjustment=0,
    ) == []


def test_tired_and_previous_result_shift_warmup_one_level_lower() -> None:
    candidates, _, _, frontiers = _load_candidates(_warmup_records(), None)

    def selected_levels(*, readiness: str, adjustment: int) -> list[float]:
        items = _select_warmup(
            candidates,
            set(),
            quota=100_000,
            frontiers=frontiers,
            now=NOW,
            rng=random.Random(2),
            readiness=readiness,
            adjustment=adjustment,
        )
        return [float(item.source_level) for item in items]

    normal = selected_levels(readiness="normal", adjustment=0)
    tired = selected_levels(readiness="tired", adjustment=0)
    poor_previous = selected_levels(readiness="normal", adjustment=-1)
    assert max(tired) == max(normal) - 1
    assert max(poor_previous) == max(normal) - 1


def test_warmup_does_not_fallback_when_no_chart_has_safe_evidence() -> None:
    rows = tuple(
        _record(index, level=index, clear=4, playcount=1)
        for index in range(12)
    )
    candidates, _, _, frontiers = _load_candidates(rows, None)

    assert _select_warmup(
        candidates,
        set(),
        quota=100_000,
        frontiers=frontiers,
        now=NOW,
        rng=random.Random(3),
        readiness="normal",
        adjustment=0,
    ) == []
    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            rows,
            table_sources=({"table_id": "satellite", "last_error": None},),
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )
    assert (
        "warmup: no chart met safe lamp-evidence and feature criteria"
        in session.table_warnings
    )


def test_table_frontiers_are_independent_and_all_memberships_are_exported(
    tmp_path,
) -> None:
    scales = {
        "satellite": -5,
        "genocide": 10,
        "stella": 30,
        "overjoy": 100,
    }
    rows: list[dict[str, object]] = []
    index = 1_000
    for table_id, start in scales.items():
        for offset in range(10):
            rows.append(
                _record(
                    index,
                    table_id=table_id,
                    level=start + offset,
                    clear=6 if offset <= 4 else 1,
                )
            )
            index += 1
    shared_sha = "f" * 64
    for table_id, start in scales.items():
        rows.append(
            _record(
                index,
                table_id=table_id,
                level=start + 2,
                sha256=shared_sha,
            )
        )
        index += 1
    sources = tuple(
        {"table_id": table_id, "last_error": None}
        for table_id in ("genocide", "overjoy", "satellite", "stella")
    )
    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            tuple(rows),
            table_sources=sources,
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )

    frontiers = {item.table_id: item.hard for item in session.table_frontiers}
    assert (
        frontiers["satellite"]
        < frontiers["genocide"]
        < frontiers["stella"]
        < frontiers["overjoy"]
    )
    shared = next(item for item in session.personal if item.sha256 == shared_sha)
    assert {rating.table_id for rating in shared.ratings} == set(scales)
    assert session.table_warnings == ()

    output = tmp_path / "all-ratings"
    write_export(session, output)
    personal = json.loads((output / "table/recommend/score.json").read_text())
    comment = next(item["comment"] for item in personal if item["sha256"] == shared_sha)
    assert all(table_id in comment for table_id in scales)


def test_table_refresh_failures_are_exposed_in_session_warnings() -> None:
    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            _warmup_records(),
            table_sources=(
                {"table_id": "satellite", "last_error": None},
                {"table_id": "genocide", "last_error": "offline"},
            ),
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )

    assert "genocide: refresh failed (offline)" in session.table_warnings
    assert any(warning.startswith("overjoy:") for warning in session.table_warnings)
    assert any(warning.startswith("stella:") for warning in session.table_warnings)
    assert not any(warning.startswith("satellite:") for warning in session.table_warnings)


def test_low_table_match_coverage_is_visible() -> None:
    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            _warmup_records(),
            table_sources=(
                {
                    "table_id": "satellite",
                    "last_error": None,
                    "entry_count": 1_000,
                    "matched_count": 10,
                },
            ),
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )

    assert "satellite: low owned-chart match coverage (10/1000)" in (
        session.table_warnings
    )


def test_partial_genocide_title_match_coverage_is_visible() -> None:
    session = build_session_from_input(
        RecommendationInput(
            ProfileContext(),
            1,
            0,
            _warmup_records(),
            table_sources=(
                {
                    "table_id": "genocide",
                    "last_error": None,
                    "entry_count": 1_035,
                    "matched_count": 550,
                },
            ),
        ),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )

    assert "genocide: low owned-chart match coverage (550/1035)" in (
        session.table_warnings
    )


def test_unknown_play_date_is_not_selected_as_overdue_review() -> None:
    rows = tuple(
        {**_record(index, level=index, clear=6), "last_played": 0}
        for index in range(12)
    )
    session = build_session_from_input(
        RecommendationInput(ProfileContext(), 1, 0, rows),
        menu_date="2026-08-15",
        target_judged=100,
        reserve_judged=0,
        clock=lambda: NOW,
    )

    assert not any(
        item.category in {"01 WARMUP", "07 REVIEW"} for item in session.queue
    )
    satellite = next(
        frontier for frontier in session.table_frontiers
        if frontier.table_id == "satellite"
    )
    assert satellite.observations == 0


def test_previous_warmup_results_produce_a_bounded_adjustment(tmp_path) -> None:
    def adjustment(
        name: str,
        results: list[tuple[int, float] | tuple[int, int, float]],
        *,
        queue_size: int | None = None,
        played_at_base: int = 4 * 3_600 + 1,
        session_created_at: int = 100,
    ) -> int:
        size = len(results) if queue_size is None else queue_size
        warmup_hashes = [f"{index:064x}" for index in range(size)]
        payload = {
            "menu_date": "1970-01-01",
            "profile": {"timezone": "UTC"},
            "queue": [
                {"category": "01 WARMUP", "sha256": sha256}
                for sha256 in warmup_hashes
            ]
        }
        conn = store.init(tmp_path / name)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO sessions(created_at, arm, slots_json) "
                    "VALUES (?, 'model', ?)",
                    (session_created_at, json.dumps(payload)),
                )
                for index, result in enumerate(results):
                    if len(result) == 2:
                        completed, bp_rate = result
                        clear = 4 if completed else 1
                    else:
                        clear, completed, bp_rate = result
                    conn.execute(
                        """INSERT INTO plays(
                            sha256, mode, played_at, playcount, source_generation,
                            source, clear, completed, bp_rate, is_course,
                            payload_hash, ingested_at
                           ) VALUES (?, 0, ?, 1, 0, 'collector', ?, ?, ?, 0, ?, ?)""",
                        (
                            warmup_hashes[index],
                            played_at_base + index,
                            clear,
                            completed,
                            bp_rate,
                            f"payload-{index}",
                            played_at_base + index,
                        ),
                    )
            return SQLiteRecommendationRepository(conn).load_input(
                ProfileContext()
            ).warmup_adjustment
        finally:
            conn.close()

    assert adjustment("poor.db", [(0, 0.12), (0, 0.12), (1, 0.03)]) == -1
    assert adjustment("comfortable.db", [(1, 0.03)] * 3) == 1
    assert adjustment(
        "failed.db", [(4, 1, 0.03), (1, 1, 0.03)]
    ) == -1
    assert adjustment(
        "truncated.db", [(1, 0.03), (1, 0.03)], queue_size=4
    ) == -1
    assert adjustment(
        "late.db", [(0, 0.12), (0, 0.12)], played_at_base=2 * 86_400
    ) == 0
    assert adjustment(
        "before-selection.db",
        [(0, 0.12), (0, 0.12)],
        played_at_base=4 * 3_600 + 1,
        session_created_at=5 * 3_600,
    ) == 0
