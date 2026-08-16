from __future__ import annotations

import pytest

from oraja_training.collect.normalize import derive_play, payload_hash
from oraja_training.domain.types import DomainValidationError, OFFICIAL_IR_GENERATION


def _row() -> dict:
    return {
        "sha256": "f" * 64,
        "mode": 1,
        "clear": 6,
        "epg": 60,
        "lpg": 40,
        "egr": 0,
        "lgr": 0,
        "egd": 0,
        "lgd": 0,
        "ebd": 0,
        "lbd": 0,
        "epr": 0,
        "lpr": 0,
        "ems": 7,
        "lms": 8,
        "notes": 100,
        "minbp": 4,
        "playcount": 2,
        "date": 123,
        "option": 1,
        "seed": 2,
        "random": 3,
        "trophy": "r",
    }


def test_derived_values_exclude_empty_poor_and_split_gauge_semantics() -> None:
    row = _row()
    play = derive_play(row, source="collector", source_generation=0)
    assert play["judged"] == 100
    assert play["empty_poor"] == 15
    assert play["survival"] == 1.0
    assert play["survival"] <= 1.0
    assert play["completed"] == 1
    assert play["bp_rate"] == 0.04
    assert play["credited_gauge_kind"] == "HARD"
    assert play["selected_gauge_kind"] is None


def test_ir_import_keeps_raw_values_but_nulls_judgement_derivations() -> None:
    row = {**_row(), "minbp": 101}
    play = derive_play(row, source="ir_import", source_generation=-1)
    assert play["clear"] == 6
    assert play["ex"] == 200
    assert play["minbp"] == 101
    assert play["notes"] == 100
    assert play["playcount"] == 2
    assert play["bp_rate"] == 1.01
    for column in ("judged", "empty_poor", "survival", "completed"):
        assert play[column] is None


def test_official_ir_has_a_reserved_scorable_generation() -> None:
    play = derive_play(
        _row(), source="official_ir", source_generation=OFFICIAL_IR_GENERATION
    )
    assert OFFICIAL_IR_GENERATION == -3
    assert play["source_generation"] == -3
    assert play["completed"] == 1

    with pytest.raises(DomainValidationError):
        derive_play(_row(), source="official_ir", source_generation=-4)


@pytest.mark.parametrize(
    ("clear", "expected"),
    [
        (0, None),
        (1, None),
        (2, None),
        (3, None),
        (4, "EASY"),
        (5, "NORMAL"),
        (6, "HARD"),
        (7, "EXHARD"),
        (8, None),
        (10, None),
    ],
)
def test_clear_only_identifies_unambiguous_credited_gauges(
    clear: int, expected: str | None
) -> None:
    play = derive_play(
        {**_row(), "clear": clear}, source="collector", source_generation=0
    )
    assert play["credited_gauge_kind"] == expected
    assert play["selected_gauge_kind"] is None


def test_payload_hash_is_order_independent_and_covers_unknown_columns() -> None:
    row = _row()
    reordered = dict(reversed(list(row.items())))
    assert payload_hash(row) == payload_hash(reordered)
    assert payload_hash(row) != payload_hash({**row, "future_column": 1})
