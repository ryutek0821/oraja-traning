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
from tests.fixtures.synthetic_beatoraja import build_synthetic_fixture


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
        bundle = _bundle(root)
        manifest = _manifest(bundle)
        artifacts = MemoryArtifactStore()
        adapter = ContainerAdapter(
            artifact_store=artifacts,
            decryptor=PassthroughDecryptor(),
            clock=lambda: 1_786_400_000,
        )

        first = adapter.run(manifest, bundle=bundle, revision=1)
        second = adapter.run(manifest, bundle=bundle, revision=1)

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
        }
        assert len(artifacts.objects) == 3
        validate_output_manifest(
            first.output_manifest,
            input_manifest=validate_input_manifest(manifest),
        )
        assert first.output_manifest["raw_db_exported"] is False


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

    framed[-1] ^= 1
    corrupt = module._BoundedBody(io.BytesIO(framed), len(framed))
    (tmp_path / "corrupt").mkdir()
    with pytest.raises(ManifestError, match="digest"):
        module._materialize_framed_input(corrupt, tmp_path / "corrupt")
