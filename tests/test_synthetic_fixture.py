from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from oraja_training.collect import snapshot
from oraja_training.db import readers
from tests.fixtures.synthetic_beatoraja import (
    CATALOG_FILENAME,
    DATABASE_NAMES,
    REQUIRED_CASES,
    SIDECAR_SUFFIXES,
    apply_coherent_update,
    build_corrupt_sqlite_fixture,
    build_counter_rollback_fixture,
    build_partial_update_fixture,
    build_sidecar_fixture,
    build_synthetic_fixture,
)


def test_fixture_manifest_is_synthetic_and_complete(synthetic_db_dir) -> None:
    manifest = json.loads((synthetic_db_dir / "manifest.json").read_text())
    assert manifest["fixture_version"] == 2
    assert manifest["source"] == "synthetic"
    assert manifest["databases"] == list(DATABASE_NAMES)
    assert set(REQUIRED_CASES) <= set(manifest["cases"])
    assert manifest["catalog"]["file"] == CATALOG_FILENAME
    assert manifest["privacy"]["contains_bms_body"] is False

    conn = sqlite3.connect(synthetic_db_dir / "songdata.db")
    try:
        paths = [row[0] for row in conn.execute("SELECT path FROM song")]
        titles = [row[0] for row in conn.execute("SELECT title FROM song")]
    finally:
        conn.close()

    assert all(path.startswith("synthetic/") for path in paths)
    assert all("Ryuhei" not in title and "ryuhei" not in title for title in titles)


def test_catalog_has_sp7_ownership_and_hash_edge_cases(synthetic_catalog) -> None:
    assert synthetic_catalog["source"] == "synthetic"
    assert synthetic_catalog["game_mode"] == "SP7"
    assert synthetic_catalog["owned"]
    assert synthetic_catalog["unowned"]
    assert all(row["game_mode"] == "SP7" for row in synthetic_catalog["owned"])
    assert all(not row["owned"] for row in synthetic_catalog["unowned"])

    collision = synthetic_catalog["hash_cases"]
    assert len(collision["collision_sha256"]) == 2
    matching = [
        row for row in synthetic_catalog["owned"]
        if row["md5"] == collision["collision_md5"]
    ]
    assert {row["sha256"] for row in matching} == set(collision["collision_sha256"])
    missing = [row for row in synthetic_catalog["table_entries"] if row["sha256"] is None]
    assert len(missing) == collision["missing_sha256_entries"] == 1


@pytest.mark.parametrize("suffix", SIDECAR_SUFFIXES)
def test_sidecar_variants_are_isolated_and_rejected(tmp_path: Path, suffix: str) -> None:
    fixture = build_sidecar_fixture(tmp_path / suffix[1:], suffix)
    assert (fixture / f"score.db{suffix}").is_file()
    with pytest.raises(snapshot.SnapshotPairMismatch, match="sidecar"):
        snapshot.run(fixture / "score.db", fixture / "scoredatalog.db", tmp_path / "assistant.db")


def test_corrupt_and_partial_update_variants_are_reproducible(tmp_path: Path) -> None:
    corrupt = build_corrupt_sqlite_fixture(tmp_path / "corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        with readers.open_snapshot(corrupt / "score.db") as conn:
            readers.read_score(conn)

    partial = build_partial_update_fixture(tmp_path / "partial")
    with pytest.raises(snapshot.SnapshotPairMismatch, match="newer"):
        snapshot.run(partial / "score.db", partial / "scoredatalog.db", tmp_path / "partial.db")


def test_counter_rollback_variant_is_rejected_and_coherent_update_is_accepted(
    tmp_path: Path,
) -> None:
    rollback = build_counter_rollback_fixture(tmp_path / "rollback")
    with pytest.raises(snapshot.SnapshotPairMismatch, match="backwards"):
        snapshot.run(rollback / "score.db", rollback / "scoredatalog.db", tmp_path / "rollback.db")

    coherent = build_synthetic_fixture(tmp_path / "coherent")
    assistant = tmp_path / "coherent.db"
    first = snapshot.run(coherent / "score.db", coherent / "scoredatalog.db", assistant)
    assert first.new_play_rows == 0
    apply_coherent_update(coherent)
    second = snapshot.run(coherent / "score.db", coherent / "scoredatalog.db", assistant)
    assert second.new_play_rows == 1


def test_collection_manifest_freezes_the_original_56_cases() -> None:
    path = Path(__file__).parent / "golden" / "collection_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["test_files"]
    assert sum(files.values()) == manifest["test_functions"] == 42
    parameterized = manifest["parametrized_functions"]
    assert len(parameterized) == 2
    assert sum(item["cases"] for item in parameterized) == 16
    assert manifest["collected_cases"] == 42 - 2 + 16 == 56


def test_fixture_and_golden_assets_contain_no_machine_or_personal_paths() -> None:
    root = Path(__file__).parent
    assets = tuple(root.joinpath("fixtures").glob("*.py")) + tuple(
        root.joinpath("golden").glob("*.json")
    ) + tuple(root.joinpath("golden").glob("*.py"))
    forbidden = ("/Users/", "/home/", "Ryuhei", "ryuhei", "C:\\Users\\")
    assert not list(root.rglob("*.bms"))
    for path in assets:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path
