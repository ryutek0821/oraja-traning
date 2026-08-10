"""Decoders for the compact strings stored by beatoraja SongInformation."""

from __future__ import annotations

import math


class SongInfoDecodeError(ValueError):
    """A songinfo value is syntactically invalid."""


def decode_distribution(value: str | None) -> list[list[int]]:
    """Decode ``#`` + (one-second x seven-column x base36-pair) data.

    The returned outer list is ordered by second and each inner list contains
    ``LN scratch, scratch density, scratch notes, LN keys, key density, key
    notes, mines``.  Empty/NULL values represent an empty distribution.
    """

    if value is None or value == "":
        return []
    if not isinstance(value, str):
        raise SongInfoDecodeError("distribution must be text")
    encoded = value.strip()
    if encoded.startswith("#"):
        encoded = encoded[1:]
    if not encoded:
        return []
    bucket_width = 7 * 2
    if len(encoded) % bucket_width:
        raise SongInfoDecodeError(
            f"distribution length {len(encoded)} is not divisible by {bucket_width}"
        )
    lowered = encoded.lower()
    if any(char not in "0123456789abcdefghijklmnopqrstuvwxyz" for char in lowered):
        raise SongInfoDecodeError("distribution contains a non-base36 character")
    return [
        [int(lowered[offset + column : offset + column + 2], 36)
         for column in range(0, bucket_width, 2)]
        for offset in range(0, len(lowered), bucket_width)
    ]


def _csv_tokens(value: str | None, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if not isinstance(value, str):
        raise SongInfoDecodeError(f"{field} must be text")
    if value.strip() == "":
        return []
    tokens = [token.strip() for token in value.split(",")]
    if any(token == "" for token in tokens):
        raise SongInfoDecodeError(f"{field} contains an empty value")
    return tokens


def decode_speedchange(value: str | None) -> list[tuple[float, float]]:
    """Decode repeating ``speed,time_ms`` pairs."""

    tokens = _csv_tokens(value, "speedchange")
    if len(tokens) % 2:
        raise SongInfoDecodeError("speedchange must contain speed,time_ms pairs")
    result: list[tuple[float, float]] = []
    for offset in range(0, len(tokens), 2):
        try:
            speed, time_ms = float(tokens[offset]), float(tokens[offset + 1])
        except ValueError as exc:
            raise SongInfoDecodeError("speedchange contains a non-number") from exc
        if not math.isfinite(speed) or not math.isfinite(time_ms):
            raise SongInfoDecodeError("speedchange contains a non-finite number")
        if time_ms < 0:
            raise SongInfoDecodeError("speedchange contains a negative time")
        if result and time_ms < result[-1][1]:
            raise SongInfoDecodeError("speedchange times are not monotonic")
        result.append((speed, time_ms))
    return result


def decode_lanenotes(value: str | None) -> list[tuple[int, int, int]]:
    """Decode repeating per-lane ``normal,long,mine`` counts."""

    tokens = _csv_tokens(value, "lanenotes")
    if len(tokens) % 3:
        raise SongInfoDecodeError("lanenotes must contain normal,long,mine triples")
    result: list[tuple[int, int, int]] = []
    for offset in range(0, len(tokens), 3):
        try:
            lane = tuple(int(token, 10) for token in tokens[offset : offset + 3])
        except ValueError as exc:
            raise SongInfoDecodeError("lanenotes contains a non-integer") from exc
        if any(count < 0 for count in lane):
            raise SongInfoDecodeError("lanenotes contains a negative count")
        result.append(lane)  # type: ignore[arg-type]
    return result
