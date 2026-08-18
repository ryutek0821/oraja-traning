from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import zipfile
import sqlite3

import pytest

from oraja_training.container import (
    CancellationToken,
    ContainerAdapter,
    ContainerError,
    InputManifest,
    MemoryArtifactStore,
    PassthroughDecryptor,
    validate_input_manifest,
    validate_output_manifest,
)
from oraja_training.container.manifest import ManifestError
from tests.fixtures.synthetic_beatoraja import build_synthetic_fixture, load_catalog


PROFILE_ID = "018f0f0f-0f01-7f0f-8f0f-0f0f0f0f0f0f"
ACCOUNT_ID = "018f0f0f-0f00-7f0f-8f0f-0f0f0f0f0f0f"
JOB_ID = "018f0f0f-0f20-7f0f-8f0f-0f0f0f0f0f0f"


def _manifest(bundle: Path) -> dict[str, object]:
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    input_digest = "1" * 64
    return {
        "contract": "container-input-manifest",
        "schema_version": "1",
        "job_id": JOB_ID,
        "idempotency_key": f"job:{PROFILE_ID}:{input_digest}",
        "account_id": ACCOUNT_ID,
        "profile_id": PROFILE_ID,
        "trust_domain": "official",
        "source_manifest_sha256": "2" * 64,
        "input_bundle": {
            "object_key": f"profiles/{PROFILE_ID}/jobs/{JOB_ID}/input.enc",
            "sha256": digest,
            "size_bytes": bundle.stat().st_size,
            "encryption": "envelope-v1",
            "key_ref": f"profile-key:{PROFILE_ID}",
        },
        "requested_at": "2026-08-11T03:02:00Z",
        "game_mode": "SP7",
        "policy": {
            "eligibility_status": "eligible",
            "eligibility_reason_code": None,
            "eligibility_policy_version": "2026-07-01",
            "include_raw_db_in_model": False,
        },
    }


def _bundle(root: Path) -> Path:
    bundle = root / "input.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in (
            "score.db",
            "scoredatalog.db",
            "scorelog.db",
            "songdata.db",
            "songinfo.db",
        ):
            archive.write(root / "source" / name, name)
    return bundle


def _table_context(source: Path) -> dict[str, object]:
    catalog = load_catalog(source)
    connection = sqlite3.connect(source / "songdata.db")
    try:
        with connection:
            for row in catalog["owned"]:
                connection.execute(
                    "INSERT INTO song VALUES (?, ?, ?, ?, ?, 7, ?)",
                    (row["sha256"], row["md5"], row["title"], row["artist"], row["notes"], f"synthetic/{row['sha256']}.bms"),
                )
    finally:
        connection.close()
    entries = [row for row in catalog["table_entries"] if row["sha256"] is not None]
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    owned = catalog["owned"][0]
    classification_sources = [
        {
            "source_id": "slst-delay",
            "family": "delay",
            "page_url": "https://example.test/slst-delay/",
            "header_url": "https://example.test/slst-delay/header.json",
            "data_url": "https://example.test/slst-delay/data.json",
            "content_digest": "7" * 64,
        }
    ]
    classification_entries = [
        {
            "source_id": "slst-delay",
            "sha256": owned["sha256"],
            "md5": owned["md5"],
            "base_scale": "sl",
            "base_level": 10,
            "classification_scale": scale,
            "classification_level": level,
            "raw_level": "sl10,dl-2,///10",
            "title": owned["title"],
        }
        for scale, level in (("dl", -2), ("///", 10))
    ]
    classification_payload = {
        "sources": classification_sources,
        "entries": classification_entries,
    }
    classification_encoded = json.dumps(
        classification_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "profile_id": PROFILE_ID,
        "display_name": "Synthetic",
        "settings_revision": 3,
        "settings": {
            "timezone": "Asia/Tokyo",
            "target_judged": 100_000,
            "reserve_judged": 10_000,
            "readiness": "normal",
        },
        "catalog_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "catalog_entries": entries,
        "classification_manifest_sha256": hashlib.sha256(
            classification_encoded
        ).hexdigest(),
        "classification_sources": classification_sources,
        "classification_entries": classification_entries,
    }


def _resign_classifications(context: dict[str, object]) -> None:
    payload = {
        "sources": context["classification_sources"],
        "entries": context["classification_entries"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    context["classification_manifest_sha256"] = hashlib.sha256(encoded).hexdigest()


def test_input_manifest_enforces_profile_partition_and_official_trust_domain() -> None:
    valid = {
        "contract": "container-input-manifest",
        "schema_version": "1",
        "job_id": JOB_ID,
        "idempotency_key": f"job:{PROFILE_ID}:{'a' * 64}",
        "account_id": ACCOUNT_ID,
        "profile_id": PROFILE_ID,
        "trust_domain": "official",
        "source_manifest_sha256": "b" * 64,
        "input_bundle": {
            "object_key": f"profiles/{PROFILE_ID}/jobs/{JOB_ID}/input.enc",
            "sha256": "c" * 64,
            "size_bytes": 1,
            "encryption": "envelope-v1",
            "key_ref": f"profile-key:{PROFILE_ID}",
        },
        "requested_at": "2026-08-11T03:02:00Z",
        "game_mode": "SP7",
        "policy": {
            "eligibility_status": "eligible",
            "eligibility_reason_code": None,
            "eligibility_policy_version": "2026-07-01",
            "include_raw_db_in_model": False,
        },
    }
    result = validate_input_manifest(valid)
    assert isinstance(result, InputManifest)
    assert result.eligibility_status == "eligible"

    self_hosted = {**valid, "trust_domain": "self_hosted"}
    with pytest.raises(ManifestError):
        validate_input_manifest(self_hosted)

    wrong_partition = {
        **valid,
        "input_bundle": {**valid["input_bundle"], "object_key": "profiles/other/input.enc"},
    }
    with pytest.raises(ManifestError):
        validate_input_manifest(wrong_partition)


def test_container_pipeline_uses_core_and_replays_immutable_artifacts() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        build_synthetic_fixture(root / "source")
        table_context = _table_context(root / "source")
        bundle = _bundle(root)
        manifest = _manifest(bundle)
        artifacts = MemoryArtifactStore()
        assistant_db = root / "assistant.db"
        adapter = ContainerAdapter(
            artifact_store=artifacts,
            decryptor=PassthroughDecryptor(),
            clock=lambda: 1_786_400_000,
        )

        first = adapter.run(
            manifest,
            bundle=bundle,
            assistant_db=assistant_db,
            revision=1,
            table_context=table_context,
        )
        second = adapter.run(
            manifest,
            bundle=bundle,
            assistant_db=assistant_db,
            revision=1,
            table_context=table_context,
        )

        assert first.output_manifest == second.output_manifest
        assert first.output_manifest_sha256 == second.output_manifest_sha256
        assert first.output_manifest["counters"] == {
            "accepted_events": 6,
            "rejected_events": 0,
        }
        assert first.counters["course_events"] == 2
        assert {item.kind for item in first.artifacts} == {
            "normalized_events",
            "feature_input",
            "model_input",
            "recommend_header",
            "recommend_score",
            "daily_menu_header",
            "daily_menu_score",
        }
        assert len(artifacts.objects) == 7
        table_documents = [
            json.loads(content)
            for key, content in artifacts.objects.items()
            if "/table/" in key
        ]
        assert len(table_documents) == 4
        assert all(PROFILE_ID not in json.dumps(document) for document in table_documents)
        validate_output_manifest(
            first.output_manifest,
            input_manifest=validate_input_manifest(manifest),
        )
        assert first.output_manifest["raw_db_exported"] is False
        with sqlite3.connect(assistant_db) as connection:
            assert connection.execute(
                "SELECT classification_scale, classification_level, match_status "
                "FROM chart_classifications ORDER BY classification_scale"
            ).fetchall() == [("///", 10, "sha256"), ("dl", -2, "sha256")]


def test_container_job_dispatch_applies_one_incremental_ir_play(tmp_path: Path) -> None:
    source = build_synthetic_fixture(tmp_path / "source")
    catalog = load_catalog(source)
    context = _table_context(source)
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    chart = catalog["owned"][0]
    event = {
        "contract": "container-play-event",
        "schema_version": 1,
        "event_id": "018f0f0f-0f30-7f0f-8f0f-0f0f0f0f0f0f",
        "profile_id": PROFILE_ID,
        "payload_digest": "3" * 64,
        "row": {
            "sha256": chart["sha256"],
            "mode": 0,
            "date": 1_786_400_000,
            "playcount": 999,
            "clear": 4,
            "notes": 1000,
            "passnotes": 1000,
            "minbp": 10,
            "epg": 500,
            "lpg": 0,
            "egr": 0,
            "lgr": 0,
            "egd": 0,
            "lgd": 0,
            "ebd": 0,
            "lbd": 0,
            "epr": 0,
            "lpr": 0,
            "ems": 0,
            "lms": 0,
        },
    }
    incremental = ContainerAdapter(
        decryptor=PassthroughDecryptor(),
        clock=lambda: 1_786_400_000,
    ).run_job("play", manifest, bundle=bundle, table_context=context, play_event=event)
    assert incremental.counters["accepted_events"] == 7

    with pytest.raises(ContainerError, match="unsupported"):
        ContainerAdapter().run_job("delete", manifest)


def test_container_refuses_to_publish_tables_without_owned_catalog_input(tmp_path: Path) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(_manifest(bundle), bundle=bundle)
    assert failure.value.code == "table_input_unavailable"


def test_container_rejects_table_context_with_mismatched_catalog_digest(tmp_path: Path) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    context = _table_context(tmp_path / "source")
    context["catalog_entries"][0]["level"] = "tampered"
    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(
            _manifest(bundle), bundle=bundle, table_context=context
        )
    assert failure.value.code == "invalid_contract"


def test_container_rejects_mismatched_classification_digest(tmp_path: Path) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    context = _table_context(tmp_path / "source")
    context["classification_entries"][0]["classification_level"] = 99
    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(
            _manifest(bundle), bundle=bundle, table_context=context
        )
    assert failure.value.code == "invalid_contract"


def test_container_rejects_resigned_unknown_classification_axis(tmp_path: Path) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    context = _table_context(tmp_path / "source")
    row = context["classification_entries"][0]
    row["classification_scale"] = "unknown"
    row["raw_level"] = "sl10,unknown-2,///10"
    _resign_classifications(context)

    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(
            _manifest(bundle), bundle=bundle, table_context=context
        )
    assert failure.value.code == "invalid_contract"


def test_container_rejects_resigned_inconsistent_classification_row(
    tmp_path: Path,
) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    context = _table_context(tmp_path / "source")
    context["classification_entries"][0]["base_level"] = 9
    _resign_classifications(context)

    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(
            _manifest(bundle), bundle=bundle, table_context=context
        )
    assert failure.value.code == "invalid_contract"


def test_container_rejects_resigned_split_classification_identity(
    tmp_path: Path,
) -> None:
    build_synthetic_fixture(tmp_path / "source")
    bundle = _bundle(tmp_path)
    context = _table_context(tmp_path / "source")
    context["classification_entries"][1]["md5"] = "8" * 32
    _resign_classifications(context)

    with pytest.raises(ContainerError) as failure:
        ContainerAdapter(decryptor=PassthroughDecryptor()).run(
            _manifest(bundle), bundle=bundle, table_context=context
        )
    assert failure.value.code == "invalid_contract"


def test_container_rejects_cancelled_or_tampered_input() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        build_synthetic_fixture(root / "source")
        bundle = _bundle(root)
        manifest = _manifest(bundle)
        cancelled = CancellationToken()
        cancelled.cancel()
        adapter = ContainerAdapter(decryptor=PassthroughDecryptor())
        with pytest.raises(ContainerError, match="job cancelled"):
            adapter.run(manifest, bundle=bundle, cancellation=cancelled)

        tampered = {**manifest, "input_bundle": {**manifest["input_bundle"], "sha256": "e" * 64}}
        with pytest.raises(ContainerError, match="integrity"):
            adapter.run(tampered, bundle=bundle)


def test_container_rejects_sidecars_before_opening_source(tmp_path: Path) -> None:
    source = build_synthetic_fixture(tmp_path / "source")
    (source / "score.db-wal").write_bytes(b"sidecar")
    adapter = ContainerAdapter(decryptor=PassthroughDecryptor())
    manifest = {
        "contract": "container-input-manifest",
        "schema_version": "1",
        "job_id": JOB_ID,
        "idempotency_key": f"job:{PROFILE_ID}:{'f' * 64}",
        "account_id": ACCOUNT_ID,
        "profile_id": PROFILE_ID,
        "trust_domain": "official",
        "source_manifest_sha256": "a" * 64,
        "input_bundle": {
            "object_key": f"profiles/{PROFILE_ID}/jobs/{JOB_ID}/input.enc",
            "sha256": "b" * 64,
            "size_bytes": 1,
            "encryption": "envelope-v1",
            "key_ref": f"profile-key:{PROFILE_ID}",
        },
        "requested_at": "2026-08-11T03:02:00Z",
        "game_mode": "SP7",
        "policy": {
            "eligibility_status": "eligible",
            "eligibility_reason_code": None,
            "eligibility_policy_version": "2026-07-01",
            "include_raw_db_in_model": False,
        },
    }
    with pytest.raises(ContainerError, match="integrity"):
        adapter.run(manifest, bundle=source)


def test_documented_container_fixtures_are_accepted_by_python_runtime() -> None:
    examples = Path(__file__).parents[1] / "docs" / "contracts" / "examples"
    input_value = json.loads((examples / "container-input.valid.json").read_text())
    output_value = json.loads((examples / "container-output.valid.json").read_text())

    validated_input = validate_input_manifest(input_value)
    assert validated_input.as_dict() == input_value
    assert validate_output_manifest(output_value) == output_value


def test_worker_framed_transport_materializes_exact_verified_five_db(tmp_path: Path) -> None:
    container_dir = Path(__file__).parents[1] / "cloudflare" / "container"
    sys.path.insert(0, str(container_dir))
    try:
        spec = importlib.util.spec_from_file_location("oraja_container_entrypoint", container_dir / "entrypoint.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    source = build_synthetic_fixture(tmp_path / "source")
    framed = bytearray(module.FRAMED_MAGIC)
    for name in sorted(module.DATABASE_NAMES):
        content = (source / name).read_bytes()
        header = json.dumps(
            {"file_name": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        framed.extend(struct.pack(">I", len(header)))
        framed.extend(header)
        framed.extend(content)
    destination = tmp_path / "materialized"
    destination.mkdir()
    source_body = module._BoundedBody(io.BytesIO(framed), len(framed))
    module._materialize_framed_input(source_body, destination)
    assert source_body.remaining == 0
    assert {path.name for path in destination.iterdir()} == module.DATABASE_NAMES
    assert all((destination / name).read_bytes() == (source / name).read_bytes() for name in module.DATABASE_NAMES)

    context = json.dumps(_table_context(source), separators=(",", ":"), sort_keys=True).encode()
    framed_v2 = bytearray(module.FRAMED_MAGIC_V2)
    framed_v2.extend(framed[len(module.FRAMED_MAGIC):])
    context_header = json.dumps(
        {
            "file_name": "table-context.json",
            "sha256": hashlib.sha256(context).hexdigest(),
            "size_bytes": len(context),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    framed_v2.extend(struct.pack(">I", len(context_header)))
    framed_v2.extend(context_header)
    framed_v2.extend(context)
    destination_v2 = tmp_path / "materialized-v2"
    destination_v2.mkdir()
    source_v2 = module._BoundedBody(io.BytesIO(framed_v2), len(framed_v2))
    assert module._materialize_framed_input(source_v2, destination_v2) == _table_context(source)
    assert source_v2.remaining == 0

    framed[-1] ^= 1
    corrupt = module._BoundedBody(io.BytesIO(framed), len(framed))
    (tmp_path / "corrupt").mkdir()
    with pytest.raises(ManifestError, match="digest"):
        module._materialize_framed_input(corrupt, tmp_path / "corrupt")
