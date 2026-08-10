from __future__ import annotations

import math
import random

from oraja_training.db import store
from oraja_training.model import fit_latest, predict_latest


def _populate(conn, *, density_signal: bool, count: int = 250) -> None:
    generator = random.Random(7)
    with conn:
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
