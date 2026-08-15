from __future__ import annotations

import math
import random

from oraja_training.db import store
from oraja_training.db.model_adapter import _observations
from oraja_training.model import fit_latest, predict_latest
from oraja_training.model.core import fit_difficulty_frontier


def _populate(conn, *, density_signal: bool, count: int = 250) -> None:
    generator = random.Random(7)
    with conn:
        import_id = conn.execute(
            """
            INSERT INTO daily_imports(
              imported_at, effective_date, score_sha256, scoredatalog_sha256,
              baseline_judged, cumulative_playcount
            ) VALUES (1, '2026-08-01', 'score', 'log', 0, 0)
            """
        ).lastrowid
        for index in range(count):
            sha = f"{index:064x}"
            level = float(index % 12)
            density = 5.0 + generator.random() * 25.0
            scratch = generator.random()
            signal = (18.0 - density) if density_signal else (6.0 - level)
            probability = 1.0 / (1.0 + math.exp(-signal / 2.5))
            completed = int(generator.random() < probability)
            played_at = 1_700_000_000 + (index // 20) * 86_400 + index
            conn.execute(
                "INSERT INTO table_entries VALUES ('test', ?, ?, NULL, ?, ?)",
                (str(level), sha, f"Chart {index}", played_at),
            )
            conn.execute(
                """INSERT INTO chart_features(
                    sha256, feature_version, density_p99, scratch_rate
                   ) VALUES (?, 1, ?, ?)""",
                (sha, density, scratch),
            )
            conn.execute(
                """INSERT INTO plays(
                    sha256, mode, played_at, playcount, source_generation, source,
                    clear, completed, is_course, payload_hash, ingested_at
                   ) VALUES (?, 0, ?, 1, 0, 'collector', 1, ?, 0, ?, ?)""",
                (sha, played_at, completed, f"payload-{index}", played_at),
            )
            conn.execute(
                """INSERT INTO score_state(
                    sha256, mode, clear, playcount, played_at, import_id, payload_hash
                   ) VALUES (?, 0, ?, 1, ?, ?, ?)""",
                (
                    sha,
                    6 if level <= 5 else 1,
                    played_at,
                    import_id,
                    f"state-{index}",
                ),
            )


def test_collects_until_minimum_data(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        _populate(conn, density_signal=True, count=180)
        result = fit_latest(conn)
        assert result.status == "collecting"
        assert predict_latest(conn, 5, 15, 0.1) is None
    finally:
        conn.close()


def test_passed_model_is_saved_predicted_and_idempotent(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        _populate(conn, density_signal=True)
        result = fit_latest(conn, trained_at=123)
        assert result.status == "passed"
        assert result.gate_fraction is not None and result.gate_fraction >= 0.8
        assert predict_latest(conn, 5, 8, 0.1) > predict_latest(conn, 5, 28, 0.1)
        again = fit_latest(conn, trained_at=456)
        assert again.status == "unchanged"
        assert conn.execute("SELECT count(*) FROM model_state").fetchone()[0] == 1
        params = conn.execute("SELECT params_json FROM model_state").fetchone()[0]
        assert "habitual settings" in params
    finally:
        conn.close()


def test_rejected_model_is_not_saved(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        _populate(conn, density_signal=False)
        result = fit_latest(conn)
        assert result.status == "rejected"
        assert conn.execute("SELECT count(*) FROM model_state").fetchone()[0] == 0
    finally:
        conn.close()


def test_multiple_table_memberships_create_one_normalized_observation(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        _populate(conn, density_signal=True, count=24)
        with conn:
            for index in range(24):
                conn.execute(
                    "INSERT INTO table_entries VALUES ('alt', ?, ?, NULL, ?, 1)",
                    (str(100 + index % 12), f"{index:064x}", f"Chart {index}"),
                )

        observations = _observations(conn)
        expected_frontier = fit_difficulty_frontier(
            [(float(index % 12), index % 12 <= 5) for index in range(24)]
        )
    finally:
        conn.close()

    assert len(observations) == 24
    assert math.isclose(
        observations[0].features[0], expected_frontier / 1.5, abs_tol=1e-9
    )
    assert abs(observations[0].features[0]) < 10  # not the raw alt level 100
