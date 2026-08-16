"""Filesystem change signals for a SQLite database and its sidecars."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileSignal:
    name: str
    exists: bool
    device: int | None
    inode: int | None
    mtime_ns: int | None
    size: int


@dataclass(frozen=True, slots=True)
class SourceSignature:
    files: tuple[FileSignal, ...]

    @property
    def last_mtime(self) -> float | None:
        mtimes = [item.mtime_ns for item in self.files if item.mtime_ns is not None]
        return max(mtimes) / 1_000_000_000 if mtimes else None

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def main_identity(self) -> tuple[int | None, int | None]:
        main = self.files[0]
        return main.device, main.inode


def source_signature(db_path: str | Path) -> SourceSignature:
    """Stat a SQLite database and each live sidecar independently."""

    main = Path(db_path)
    signals: list[FileSignal] = []
    for path in (
        main,
        Path(f"{main}-wal"),
        Path(f"{main}-shm"),
        Path(f"{main}-journal"),
    ):
        try:
            stat = path.stat()
        except FileNotFoundError:
            if path == main:
                raise
            signals.append(FileSignal(path.name, False, None, None, None, 0))
        else:
            signals.append(
                FileSignal(
                    path.name,
                    True,
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )
    return SourceSignature(tuple(signals))
