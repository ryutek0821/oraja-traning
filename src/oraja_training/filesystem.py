"""Portable filesystem identifiers for SQLite persistence."""

from __future__ import annotations

import hashlib


SQLITE_INT64_MIN = -(1 << 63)
SQLITE_INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1
UINT128_MAX = (1 << 128) - 1


def _require_integer(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"filesystem {field} must be an integer")


def _signed_limb(value: int) -> int:
    return value if value <= SQLITE_INT64_MAX else value - (1 << 64)


def sqlite_file_identity(device: int, inode: int) -> tuple[int, int]:
    """Map one OS file identity to two SQLite signed 64-bit integers.

    Existing signed pairs remain byte-for-byte compatible.  Windows can expose
    an unsigned 64-bit ``st_dev`` and an ``st_ino`` as wide as 128 bits.  When
    either value exceeds SQLite's signed domain, hash the complete pair into
    the two existing INTEGER columns.  The 128-bit, domain-separated mapping
    avoids lossy masking and is deterministic across collector restarts.
    """

    _require_integer(device, "device")
    _require_integer(inode, "inode")
    if (
        SQLITE_INT64_MIN <= device <= SQLITE_INT64_MAX
        and SQLITE_INT64_MIN <= inode <= SQLITE_INT64_MAX
    ):
        return device, inode
    if not 0 <= device <= UINT64_MAX:
        raise OverflowError("filesystem device is outside the unsigned 64-bit range")
    if not 0 <= inode <= UINT128_MAX:
        raise OverflowError("filesystem inode is outside the unsigned 128-bit range")

    payload = device.to_bytes(8, "big") + inode.to_bytes(16, "big")
    digest = hashlib.blake2b(
        payload,
        digest_size=16,
        person=b"oraja-fsid-v1",
    ).digest()
    return (
        _signed_limb(int.from_bytes(digest[:8], "big")),
        _signed_limb(int.from_bytes(digest[8:], "big")),
    )
