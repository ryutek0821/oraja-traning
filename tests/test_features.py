from __future__ import annotations

import sqlite3

import pytest

from oraja_training.features import (
    SongInfoDecodeError,
    build_all,
    decode_distribution,
    decode_lanenotes,
    decode_speedchange,
    extract_features,
)


SHA = "a" * 64


def _base36_pair(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return alphabet[value // 36] + alphabet[value % 36]


def test_decode_songinfo_formats() -> None:
    assert decode_distribution("#00010203040506") == [[0, 1, 2, 3, 4, 5, 6]]
    assert decode_distribution("0000000000000z") == [[0, 0, 0, 0, 0, 0, 35]]
    assert decode_speedchange("180,0, 0,1000, 180,1500") == [
        (180.0, 0.0),
        (0.0, 1000.0),
        (180.0, 1500.0),
    ]
    assert decode_lanenotes("10,2,1, 20,0,0") == [(10, 2, 1), (20, 0, 0)]


@pytest.mark.parametrize(
    ("decoder", "value"),
    [
        (decode_distribution, "#0000"),
        (decode_distribution, "#0000000000000!"),
        (decode_speedchange, "180,0,120"),
        (decode_speedchange, "180,100,180,50"),
        (decode_lanenotes, "1,2"),
        (decode_lanenotes, "1,-2,0"),
    ],
)
def test_decoders_reject_corrupt_values(decoder, value) -> None:
    with pytest.raises(SongInfoDecodeError):
        decoder(value)


def test_extract_features() -> None:
    # Densities: [10, 20, 30, 40, 50, 0], scratches: [0, 2, 3, 0, 5, 0].
    buckets = [
        (0, 0, 0, 0, 0, 10, 0),
        (0, 0, 2, 0, 0, 18, 0),
        (0, 0, 3, 0, 0, 27, 0),
        (0, 0, 0, 0, 0, 40, 0),
        (0, 0, 5, 0, 0, 45, 0),
        (0, 0, 0, 0, 0, 0, 0),
    ]
    encoded = "#" + "".join(
        "".join(_base36_pair(value) for value in bucket) for bucket in buckets
    )
    feature = extract_features(
        {
            "sha256": SHA,
            "distribution": encoded,
            "speedchange": "180,0,0,2000,180,5000",
            "lanenotes": "90,20,0,30,10,1",
        }
    )
    assert feature.density_mean == 25
    assert feature.density_p90 == 50
    assert feature.density_p99 == 50
    assert feature.end_density == 30
    assert feature.burst_max == 50
    assert feature.scratch_rate == pytest.approx(0.1 / 1.5)
    assert feature.scratch_p90 == 5
    assert feature.scratch_combo_rate == 1
    assert feature.ln_rate == pytest.approx(30 / 150)
    assert feature.soflan_var == 7200
    assert feature.soflan_changes == 2
    assert feature.stop_count == 1
    assert feature.chart_seconds == 5
    assert feature.total_notes == 150


def test_long_notes_contribute_to_density_and_total() -> None:
    feature = extract_features(
        {
            "sha256": SHA,
            # LN scratch=2, LN key=3, normal key=5.
            "distribution": "#02000003000500",
            "speedchange": "120,0,120,1000",
            "lanenotes": "5,3,0,0,2,0",
        }
    )
    assert feature.total_notes == 10
    assert feature.density_mean == 10
    assert feature.scratch_rate == 0.2
    assert feature.ln_rate == 0.5


def test_build_all_reads_source_and_upserts_destination() -> None:
    source = sqlite3.connect(":memory:")
    destination = sqlite3.connect(":memory:")
    source.execute(
        "CREATE TABLE information (sha256 TEXT, distribution TEXT, "
        "speedchange TEXT, lanenotes TEXT)"
    )
    source.execute(
        "INSERT INTO information VALUES (?, ?, ?, ?)",
        (SHA, "#00000000000500", "120,0,120,1000", "5,0,0"),
    )
    destination.execute(
        "CREATE TABLE chart_features (sha256 TEXT PRIMARY KEY, feature_version INTEGER, "
        "density_mean REAL, density_p90 REAL, density_p99 REAL, end_density REAL, "
        "burst_max REAL, scratch_rate REAL, scratch_p90 REAL, scratch_combo_rate REAL, "
        "ln_rate REAL, soflan_var REAL, soflan_changes INTEGER, stop_count INTEGER, "
        "chart_seconds REAL, total_notes INTEGER)"
    )
    source.execute("PRAGMA query_only=ON")

    assert build_all(source, destination) == 1
    assert build_all(source, destination) == 1
    assert destination.execute(
        "SELECT count(*), total_notes FROM chart_features"
    ).fetchone() == (1, 5)
