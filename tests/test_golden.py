from __future__ import annotations

import json
from pathlib import Path

from tests.golden.update_synthetic import build_golden_payload


GOLDEN = Path(__file__).parent / "golden" / "synthetic_outputs.json"


def test_synthetic_outputs_match_committed_golden(tmp_path: Path) -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = build_golden_payload(tmp_path)
    assert actual == expected


def test_golden_covers_normalized_plays_and_daily_budget() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    normalized = golden["normalized_plays"]
    assert normalized["result"] == {
        "charts": 4,
        "legacy_plays": 4,
        "ir_imports": 2,
        "song_rows_seen": 5,
        "skipped_ir_noplay": 2,
    }
    assert "payload_hash" in normalized["columns"]
    assert all("id" not in row for row in normalized["rows"])

    menu = golden["daily_menu"]
    assert menu["target_judged"] == 100_000
    assert menu["reserve_target"] == 10_000
    assert menu["core_expected_judged"] >= menu["target_judged"]
    assert menu["reserve_expected_judged"] >= menu["reserve_target"]
    assert golden["model_state"]["status"] == "validated_model"
