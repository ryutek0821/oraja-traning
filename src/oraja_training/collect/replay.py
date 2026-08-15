"""Bounded, metadata-only reader for beatoraja ``.brd`` replay slots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Any


MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REPLAY_FILES = 128
MAX_ARRAY_ITEMS = 4_096

GAUGE_KINDS = {
    0: "ASSIST_EASY",
    1: "EASY",
    2: "NORMAL",
    3: "HARD",
    4: "EXHARD",
    5: "HAZARD",
}


class ReplayReadError(ValueError):
    """The replay cannot be safely interpreted as metadata."""


class ReplayChangedDuringRead(ReplayReadError):
    """The replay slot was replaced while it was being read."""


@dataclass(frozen=True, slots=True)
class ReplayMeta:
    path: Path
    device: int
    inode: int
    content_hash: str
    compressed_size: int
    mtime_ns: int
    sha256: str
    mode: int
    played_at: int
    gauge: int
    selected_gauge_kind: str
    randomoption: int | None
    randomoptionseed: int | None
    randomoption2: int | None
    randomoption2seed: int | None
    doubleoption: int | None
    seven_to_nine_pattern: int | None
    lane_shuffle_pattern: tuple[tuple[int, ...], ...] | None
    rand: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class ReplayScanResult:
    metadata: tuple[ReplayMeta, ...]
    invalid: int = 0
    unstable: int = 0
    invalid_paths: tuple[Path, ...] = ()
    unstable_paths: tuple[Path, ...] = ()


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayReadError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ReplayReadError(f"{field} must be >= {minimum}")
    return value


def _optional_integer(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    return None if value is None else _integer(value, field)


def _integer_array(value: Any, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
        raise ReplayReadError(f"{field} must be a bounded integer array")
    return tuple(_integer(item, field) for item in value)


def _lane_pattern(payload: dict[str, Any]) -> tuple[tuple[int, ...], ...] | None:
    value = payload.get("laneShufflePattern")
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_ARRAY_ITEMS:
        raise ReplayReadError("laneShufflePattern must be a bounded integer matrix")
    rows = tuple(_integer_array(row, "laneShufflePattern") for row in value)
    if sum(len(row) for row in rows) > MAX_ARRAY_ITEMS:
        raise ReplayReadError("laneShufflePattern contains too many items")
    return rows


def read(path: str | Path) -> ReplayMeta:
    """Read one stable GZIP JSON object without decoding or retaining key input."""

    replay_path = Path(path)
    before = replay_path.stat()
    if before.st_size > MAX_COMPRESSED_BYTES:
        raise ReplayReadError("compressed replay exceeds size limit")

    digest = hashlib.sha256()
    with replay_path.open("rb") as source:
        compressed = source.read(MAX_COMPRESSED_BYTES + 1)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ReplayReadError("compressed replay exceeds size limit")
    digest.update(compressed)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
            payload_bytes = archive.read(MAX_JSON_BYTES + 1)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ReplayReadError("invalid or truncated gzip replay") from exc
    if len(payload_bytes) > MAX_JSON_BYTES:
        raise ReplayReadError("decompressed replay exceeds size limit")

    after = replay_path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise ReplayChangedDuringRead("replay changed during read")

    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayReadError("replay is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ReplayReadError("replay root must be an object")

    sha256 = payload.get("sha256")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in sha256)
    ):
        raise ReplayReadError("sha256 must contain 64 hexadecimal characters")
    gauge = _integer(payload.get("gauge"), "gauge")
    if gauge not in GAUGE_KINDS:
        raise ReplayReadError("gauge is outside the supported range 0..5")
    rand_value = payload.get("rand")

    return ReplayMeta(
        path=replay_path,
        device=before.st_dev,
        inode=before.st_ino,
        content_hash=digest.hexdigest(),
        compressed_size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        sha256=sha256.lower(),
        mode=_integer(payload.get("mode"), "mode", minimum=0),
        played_at=_integer(payload.get("date"), "date", minimum=1),
        gauge=gauge,
        selected_gauge_kind=GAUGE_KINDS[gauge],
        randomoption=_optional_integer(payload, "randomoption"),
        randomoptionseed=_optional_integer(payload, "randomoptionseed"),
        randomoption2=_optional_integer(payload, "randomoption2"),
        randomoption2seed=_optional_integer(payload, "randomoption2seed"),
        doubleoption=_optional_integer(payload, "doubleoption"),
        seven_to_nine_pattern=_optional_integer(payload, "sevenToNinePattern"),
        lane_shuffle_pattern=_lane_pattern(payload),
        rand=None if rand_value is None else _integer_array(rand_value, "rand"),
    )


def scan(replay_dir: str | Path) -> list[ReplayMeta]:
    """Read a bounded number of replay files in deterministic path order."""

    directory = Path(replay_dir)
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*.brd"))
    if len(paths) > MAX_REPLAY_FILES:
        raise ReplayReadError(f"replay directory exceeds {MAX_REPLAY_FILES} files")
    return [read(path) for path in paths]


def scan_report(
    replay_dir: str | Path,
    *,
    candidates: Iterable[str | Path] | None = None,
) -> ReplayScanResult:
    """Scan one bounded batch so one corrupt file cannot hide other slots."""

    directory = Path(replay_dir)
    if not directory.is_dir():
        return ReplayScanResult(())
    paths = sorted(
        directory.glob("*.brd")
        if candidates is None
        else (Path(path) for path in candidates)
    )[:MAX_REPLAY_FILES]
    invalid_paths: list[Path] = []
    unstable_paths: list[Path] = []
    metadata: list[ReplayMeta] = []
    for path in paths:
        try:
            metadata.append(read(path))
        except ReplayChangedDuringRead:
            unstable_paths.append(path)
        except (ReplayReadError, OSError):
            invalid_paths.append(path)
    return ReplayScanResult(
        tuple(metadata),
        invalid=len(invalid_paths),
        unstable=len(unstable_paths),
        invalid_paths=tuple(invalid_paths),
        unstable_paths=tuple(unstable_paths),
    )
