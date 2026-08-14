from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oraja_training.collect import replay


def _write(path: Path, payload: object) -> None:
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))


def _payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "sha256": "a" * 64,
        "mode": 0,
        "date": 1_700_000_000,
        "gauge": 3,
        "randomoption": 1,
        "randomoptionseed": 42,
        "laneShufflePattern": [[0, 2, 1]],
        "rand": [2, 1],
        "keyinput": "must-not-be-retained",
        "unknownFutureField": {"ignored": True},
    }
    value.update(changes)
    return value


def test_read_keeps_only_allowlisted_metadata(tmp_path) -> None:
    path = tmp_path / "slot.brd"
    _write(path, _payload())
    before = (path.stat().st_mtime_ns, path.read_bytes())

    meta = replay.read(path)

    assert meta.sha256 == "a" * 64
    assert meta.selected_gauge_kind == "HARD"
    assert meta.randomoptionseed == 42
    assert meta.lane_shuffle_pattern == ((0, 2, 1),)
    assert "keyinput" not in meta.__slots__
    assert "unknownFutureField" not in meta.__slots__
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


@pytest.mark.parametrize("payload", [[], [ _payload() ]])
def test_read_rejects_course_or_other_array_roots(tmp_path, payload) -> None:
    path = tmp_path / "course.brd"
    _write(path, payload)
    with pytest.raises(replay.ReplayReadError, match="root must be an object"):
        replay.read(path)


@pytest.mark.parametrize("gauge", [-1, 6, "3", True])
def test_read_rejects_invalid_gauge(tmp_path, gauge) -> None:
    path = tmp_path / "gauge.brd"
    _write(path, _payload(gauge=gauge))
    with pytest.raises(replay.ReplayReadError, match="gauge"):
        replay.read(path)


def test_read_rejects_truncated_and_decompressed_oversize(tmp_path, monkeypatch) -> None:
    truncated = tmp_path / "truncated.brd"
    truncated.write_bytes(gzip.compress(b"{}")[:-2])
    with pytest.raises(replay.ReplayReadError, match="truncated"):
        replay.read(truncated)

    monkeypatch.setattr(replay, "MAX_JSON_BYTES", 64)
    oversized = tmp_path / "oversized.brd"
    _write(oversized, _payload(keyinput="x" * 10_000))
    with pytest.raises(replay.ReplayReadError, match="decompressed"):
        replay.read(oversized)


def test_read_rejects_compressed_oversize_and_unstable_stat(tmp_path, monkeypatch) -> None:
    path = tmp_path / "slot.brd"
    _write(path, _payload())
    monkeypatch.setattr(replay, "MAX_COMPRESSED_BYTES", 10)
    with pytest.raises(replay.ReplayReadError, match="compressed"):
        replay.read(path)

    monkeypatch.setattr(replay, "MAX_COMPRESSED_BYTES", 2 * 1024 * 1024)
    original_stat = Path.stat
    target_stats = 0

    def changed_stat(candidate: Path, *args, **kwargs):
        nonlocal target_stats
        value = original_stat(candidate, *args, **kwargs)
        if candidate != path:
            return value
        target_stats += 1
        if target_stats < 2:
            return value
        return SimpleNamespace(
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns + 1,
        )

    monkeypatch.setattr(Path, "stat", changed_stat)
    with pytest.raises(replay.ReplayChangedDuringRead):
        replay.read(path)


def test_scan_is_deterministic_and_bounded(tmp_path, monkeypatch) -> None:
    _write(tmp_path / "b.brd", _payload(sha256="b" * 64))
    _write(tmp_path / "a.brd", _payload(sha256="a" * 64))
    assert [item.path.name for item in replay.scan(tmp_path)] == ["a.brd", "b.brd"]
    monkeypatch.setattr(replay, "MAX_REPLAY_FILES", 1)
    with pytest.raises(replay.ReplayReadError, match="exceeds 1"):
        replay.scan(tmp_path)
