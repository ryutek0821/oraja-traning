from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAYER_DB_DIR = PROJECT_ROOT / "player-file"


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecars() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for pattern in ("*.db-wal", "*.db-shm", "*.db-journal")
            for path in PLAYER_DB_DIR.glob(pattern)
        )
    )


@pytest.fixture(scope="session")
def player_db_dir() -> Path:
    return PLAYER_DB_DIR


@pytest.fixture(scope="session", autouse=True)
def source_databases_are_unchanged() -> None:
    paths = sorted(PLAYER_DB_DIR.glob("*.db"))
    assert paths, "player-file contains no source databases"
    assert not _sidecars(), "player-file must be a static snapshot without sidecars"
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _md5(path))
        for path in paths
    }
    yield
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _md5(path))
        for path in paths
    }
    assert after == before, "a player-file database changed during tests"
    assert not _sidecars(), "a player-file SQLite sidecar was generated during tests"
