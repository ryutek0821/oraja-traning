from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.fixtures.synthetic_beatoraja import (
    CATALOG_FILENAME,
    DATABASE_NAMES,
    build_synthetic_fixture,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecars(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for pattern in ("*.db-wal", "*.db-shm", "*.db-journal")
            for path in root.glob(pattern)
        )
    )


def _fixture_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            [root / name for name in DATABASE_NAMES]
            + [root / "manifest.json", root / CATALOG_FILENAME]
        )
    )


@pytest.fixture(scope="session")
def synthetic_db_dir(tmp_path_factory) -> Path:
    return build_synthetic_fixture(tmp_path_factory.mktemp("synthetic-beatoraja"))


@pytest.fixture(scope="session")
def player_db_dir(synthetic_db_dir: Path) -> Path:
    """Compatibility alias; it never resolves to the removed real snapshot."""

    return synthetic_db_dir


@pytest.fixture(scope="session")
def synthetic_manifest(synthetic_db_dir: Path) -> dict:
    return json.loads((synthetic_db_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def synthetic_catalog(synthetic_db_dir: Path) -> dict:
    return json.loads(
        (synthetic_db_dir / CATALOG_FILENAME).read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session", autouse=True)
def source_databases_are_unchanged(synthetic_db_dir: Path) -> None:
    paths = _fixture_artifacts(synthetic_db_dir)
    assert all(path.is_file() for path in paths)
    assert not _sidecars(synthetic_db_dir), "synthetic fixture must have no sidecars"
    before = {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _sha256(path))
        for path in paths
    }
    yield
    after = {
        path: (path.stat().st_mtime_ns, path.stat().st_size, _sha256(path))
        for path in paths
    }
    assert after == before, "a synthetic source database changed during tests"
    assert not _sidecars(synthetic_db_dir), "a synthetic SQLite sidecar was generated during tests"
