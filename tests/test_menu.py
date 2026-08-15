from __future__ import annotations

import json
import time
from datetime import datetime

from oraja_training.db import store
from oraja_training.domain import ProfileContext
from oraja_training.plan.menu import build_session, next_training_date, training_day, write_export


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
