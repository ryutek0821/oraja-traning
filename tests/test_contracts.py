"""Contract fixtures for the Phase 0 service boundary definitions.

The production project intentionally has no JSON Schema dependency yet.  This
small validator covers the JSON Schema features used by the checked-in
contracts so the normal/reject fixtures can be tested with the standard
library only.  A later Worker/Container adapter may replace it with a full
validator, but these invariants must remain true.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
EXAMPLES = CONTRACTS / "examples"
COMMON = CONTRACTS / "common.schema.json"
COMMON_URI = "https://oraja-training.dev/contracts/common.schema.json"


class SchemaViolation(AssertionError):
    """Raised when a fixture does not satisfy the contract schema."""


def _pointer(document: Any, fragment: str) -> Any:
    value = document
    for part in fragment.removeprefix("#/").split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]


def _schema_document(
    uri: str,
    *,
    document: dict[str, Any],
    common: dict[str, Any],
) -> dict[str, Any]:
    if not uri or uri == document.get("$id"):
        return document
    if uri in {COMMON_URI, "common.schema.json"}:
        return common
    for schema_path in CONTRACTS.glob("*.schema.json"):
        candidate = json.loads(schema_path.read_text())
        if candidate.get("$id") == uri:
            return candidate
    raise SchemaViolation(f"unresolved schema reference: {uri}")


def _validate(
    schema: dict[str, Any],
    value: Any,
    *,
    document: dict[str, Any],
    common: dict[str, Any],
    path: str = "$",
) -> None:
    ref = schema.get("$ref")
    if ref is not None:
        uri, _, fragment = ref.partition("#")
        target_document = _schema_document(uri, document=document, common=common)
        target = _pointer(target_document, "#" + fragment) if fragment else target_document
        _validate(target, value, document=target_document, common=common, path=path)
        return

    if "allOf" in schema:
        for index, branch in enumerate(schema["allOf"]):
            _validate(branch, value, document=document, common=common, path=f"{path}.allOf[{index}]")

    if "anyOf" in schema:
        failures: list[str] = []
        for branch in schema["anyOf"]:
            try:
                _validate(branch, value, document=document, common=common, path=path)
            except SchemaViolation as exc:
                failures.append(str(exc))
            else:
                break
        else:
            raise SchemaViolation(f"{path}: no anyOf branch matched: {failures}")

    if "if" in schema:
        try:
            _validate(schema["if"], value, document=document, common=common, path=path)
        except SchemaViolation:
            pass
        else:
            if "then" in schema:
                _validate(schema["then"], value, document=document, common=common, path=path)
    if "not" in schema:
        try:
            _validate(schema["not"], value, document=document, common=common, path=path)
        except SchemaViolation:
            pass
        else:
            raise SchemaViolation(f"{path}: not-schema matched")

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not any(_type_matches(value, item) for item in expected_types):
            raise SchemaViolation(f"{path}: expected {expected_types}, got {type(value).__name__}")

    if "const" in schema and value != schema["const"]:
        raise SchemaViolation(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaViolation(f"{path}: {value!r} is not in enum")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaViolation(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = set(value).difference(properties)
            if unexpected:
                raise SchemaViolation(f"{path}: unexpected keys {sorted(unexpected)!r}")
        for key, subschema in properties.items():
            if key in value:
                _validate(subschema, value[key], document=document, common=common, path=f"{path}.{key}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaViolation(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaViolation(f"{path}: more than maxItems")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise SchemaViolation(f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(schema["items"], item, document=document, common=common, path=f"{path}[{index}]")
        if "contains" in schema and not any(
            _matches(schema["contains"], item, document=document, common=common)
            for item in value
        ):
            raise SchemaViolation(f"{path}: contains condition did not match")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaViolation(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaViolation(f"{path}: longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise SchemaViolation(f"{path}: pattern mismatch")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SchemaViolation(f"{path}: invalid date-time") from exc
            if parsed.tzinfo is None:
                raise SchemaViolation(f"{path}: date-time must include timezone")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaViolation(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaViolation(f"{path}: above maximum")


def _matches(
    schema: dict[str, Any],
    value: Any,
    *,
    document: dict[str, Any],
    common: dict[str, Any],
) -> bool:
    try:
        _validate(schema, value, document=document, common=common)
    except SchemaViolation:
        return False
    return True


def _load(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    schema_path = CONTRACTS / name
    return json.loads(schema_path.read_text()), json.loads(COMMON.read_text())


def _refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        found = [value["$ref"]] if "$ref" in value else []
        return found + [ref for child in value.values() for ref in _refs(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in _refs(child)]
    return []


def _validate_five_db_semantics(payload: dict[str, Any], rules: dict[str, Any]) -> None:
    if sum(item["size_bytes"] for item in payload["files"]) > rules["max_total_bytes"]:
        raise SchemaViolation("$.files: total size exceeds max_total_bytes")


def _validate_artifact_semantics(payload: dict[str, Any], rules: dict[str, Any]) -> None:
    kinds = {item["kind"] for item in payload["artifacts"]}
    if not set(rules["required_kinds"]).issubset(kinds):
        raise SchemaViolation("$.artifacts: required publish bundle is incomplete")
    pointer = payload["latest_pointer"]
    if pointer["profile_id"] != payload["profile_id"]:
        raise SchemaViolation("$.latest_pointer.profile_id: root mismatch")
    if pointer["revision"] != payload["revision"]:
        raise SchemaViolation("$.latest_pointer.revision: root mismatch")
    if pointer["recommendation_version_id"] != payload["recommendation_version_id"]:
        raise SchemaViolation("$.latest_pointer.recommendation_version_id: root mismatch")
    if payload["previous_revision"] >= payload["revision"]:
        raise SchemaViolation("$.previous_revision: must be lower than revision")


SCHEMA_FIXTURES = (
    ("ir-submission.v1.schema.json", "ir-submission.valid.json", "ir-submission.invalid-owner.json"),
    ("play-event.v1.schema.json", "play-event.valid.json", "play-event.invalid-backfill-provenance.json"),
    ("aggregate-eligibility.v1.schema.json", "aggregate-eligibility.valid.json", "aggregate-eligibility.invalid-boolean.json"),
    ("upload-manifest.v1.schema.json", "upload-manifest.valid.json", "upload-manifest.invalid-sidecar.json"),
    ("container-input-manifest.v1.schema.json", "container-input.valid.json", "container-input.invalid-self-hosted-aggregate.json"),
    ("container-output-manifest.v1.schema.json", "container-output.valid.json", "container-output.invalid-self-hosted-aggregate.json"),
    ("artifact-manifest.v1.schema.json", "artifact-manifest.valid.json", "artifact-manifest.invalid-revision.json"),
)


@pytest.mark.parametrize("schema_name,valid_name,invalid_name", SCHEMA_FIXTURES)
def test_valid_contract_examples(schema_name: str, valid_name: str, invalid_name: str) -> None:
    schema, common = _load(schema_name)
    valid = json.loads((EXAMPLES / valid_name).read_text())
    _validate(schema, valid, document=schema, common=common)


@pytest.mark.parametrize("schema_name,valid_name,invalid_name", SCHEMA_FIXTURES)
def test_rejected_contract_examples(schema_name: str, valid_name: str, invalid_name: str) -> None:
    schema, common = _load(schema_name)
    invalid = json.loads((EXAMPLES / invalid_name).read_text())
    with pytest.raises(SchemaViolation):
        _validate(schema, invalid, document=schema, common=common)


def test_every_contract_schema_is_json_and_versioned() -> None:
    for schema_path in CONTRACTS.glob("*.schema.json"):
        schema = json.loads(schema_path.read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        if schema_path != COMMON:
            assert "/v1/" in schema["$id"]
            assert schema["additionalProperties"] is False


def test_common_schema_refs_resolve_from_the_declared_schema_ids() -> None:
    for schema_path in CONTRACTS.glob("*.schema.json"):
        schema = json.loads(schema_path.read_text())
        common_refs = [ref for ref in _refs(schema) if "common.schema.json" in ref]
        assert all(ref.startswith(f"{COMMON_URI}#") for ref in common_refs)


def test_domain_entity_catalog_fixes_profile_limit_and_ownership() -> None:
    catalog = json.loads((CONTRACTS / "domain-entities.v1.json").read_text())
    assert catalog["profile_limit"]["default"] == 1
    entities = {entity["name"]: entity for entity in catalog["entities"]}
    assert entities["Profile"]["owner"] == "account_id"
    assert entities["Device"]["owner"] == "profile_id"
    assert all(entity["id_format"] == "uuidv7" for entity in entities.values())
    assert entities["PlayEvent"]["immutable_fields"][:2] == ["event_id", "profile_id"]
    assert catalog["profile_limit"]["slot_reusable_after_profile_deletion"] is True


def test_upload_manifest_has_exact_five_database_names() -> None:
    manifest = json.loads((EXAMPLES / "upload-manifest.valid.json").read_text())
    assert [item["file_name"] for item in manifest["files"]] == [
        "score.db",
        "scoredatalog.db",
        "scorelog.db",
        "songdata.db",
        "songinfo.db",
    ]
    assert all("-wal" not in item["file_name"] and "-shm" not in item["file_name"] for item in manifest["files"])


def test_object_key_rejects_path_traversal() -> None:
    schema, common = _load("container-input-manifest.v1.schema.json")
    payload = json.loads((EXAMPLES / "container-input.valid.json").read_text())
    payload["input_bundle"]["object_key"] = "profiles/../outside/input.enc"
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)


@pytest.mark.parametrize("field", ["account_id", "profile_id", "device_id", "provenance", "eligibility", "course_id", "game_mode", "title", "path", "values", "replay"])
def test_external_ir_rejects_server_fields_and_out_of_scope_data(field: str) -> None:
    schema, common = _load("ir-submission.v1.schema.json")
    payload = json.loads((EXAMPLES / "ir-submission.valid.json").read_text())
    payload[field] = {} if field in {"provenance", "eligibility", "values", "replay"} else "forbidden"
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)


def test_course_fixture_is_rejected_unconditionally() -> None:
    schema, common = _load("ir-submission.v1.schema.json")
    payload = json.loads((EXAMPLES / "ir-submission.invalid-course.json").read_text())
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)


def test_five_db_backfill_is_internal_personal_only_play_event() -> None:
    schema, common = _load("play-event.v1.schema.json")
    payload = json.loads((EXAMPLES / "play-event.valid-backfill.json").read_text())
    _validate(schema, payload, document=schema, common=common)
    assert payload["device_id"] is None
    assert payload["eligibility"] == {
        "status": "ineligible",
        "reason_code": "five_db_backfill",
        "policy_version": "2026-07-01",
    }


def test_five_db_size_and_operational_rules() -> None:
    schema, common = _load("upload-manifest.v1.schema.json")
    payload = json.loads((EXAMPLES / "upload-manifest.valid.json").read_text())
    payload["files"][0]["size_bytes"] = 2147483649
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())["five_db"]
    assert rules["beatoraja_must_be_stopped"] is True
    assert rules["max_total_bytes"] == 5 * 1024**3
    assert rules["history_authority"] == rules["conflict_winner"] == "live_ir"
    assert rules["backfill_event_id"] == "server-generated-uuidv7"
    assert rules["backfill_job_count"] == rules["backfill_revision_count"] == 1
    _validate_five_db_semantics(payload, rules)

    over_total = json.loads((EXAMPLES / "upload-manifest.valid.json").read_text())
    for item in over_total["files"]:
        item["size_bytes"] = 1024**3
    over_total["files"][0]["size_bytes"] += 1
    _validate(schema, over_total, document=schema, common=common)
    with pytest.raises(SchemaViolation):
        _validate_five_db_semantics(over_total, rules)


def test_envelope_encrypted_container_input_requires_key_reference() -> None:
    schema, common = _load("container-input-manifest.v1.schema.json")
    payload = json.loads((EXAMPLES / "container-input.valid.json").read_text())
    payload["input_bundle"].pop("key_ref")
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)


def test_ingest_idempotency_queue_and_revision_rules() -> None:
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())
    ingest = rules["ir_ingest"]
    assert ingest["atomic_accept"] == ["play_event", "job", "revision", "outbox", "alarm"]
    assert ingest["responses"] == {"new": 202, "same_event_same_payload": 200, "same_event_conflict": 409}
    assert ingest["retry"]["attempts"] == 5 and ingest["retry"]["exhausted"] == "dlq"
    assert rules["revision"]["latest_update"] == "candidate>current"
    assert rules["revision"]["queue_delivery"] == "duplicate-and-out-of-order-safe"


@pytest.mark.parametrize(
    "current,candidate,expected",
    [(0, 1, 1), (12, 13, 13), (12, 12, 12), (12, 11, 12)],
)
def test_latest_revision_compare_and_set_is_monotonic(current: int, candidate: int, expected: int) -> None:
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())
    assert rules["revision"]["latest_update"] == "candidate>current"
    actual = candidate if candidate > current else current
    assert actual == expected


def test_artifact_bundle_visibility_and_pointer_consistency() -> None:
    schema, common = _load("artifact-manifest.v1.schema.json")
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())["artifact_publish"]
    valid = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
    _validate(schema, valid, document=schema, common=common)
    _validate_artifact_semantics(valid, rules)
    assert rules["manifest_digest"] == {
        "canonicalization": "RFC8785",
        "excluded_member": "latest_pointer",
        "algorithm": "sha256",
    }

    missing_capability = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
    missing_capability["artifacts"][0]["capability_ref"] = None
    with pytest.raises(SchemaViolation):
        _validate(schema, missing_capability, document=schema, common=common)

    public_model = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
    public_model["artifacts"].append({
        "kind": "model_snapshot",
        "object_key": "profiles/018f0f0f-0f01-7f0f-8f0f-0f0f0f0f0f0f/artifacts/12/model.json",
        "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "size_bytes": 900,
        "content_type": "application/json",
        "visibility": "capability_readonly",
        "capability_ref": "018f0f0f-0f34-7f0f-8f0f-0f0f0f0f0f0f",
    })
    with pytest.raises(SchemaViolation):
        _validate(schema, public_model, document=schema, common=common)

    for field, value in (
        ("profile_id", "018f0f0f-0f99-7f0f-8f0f-0f0f0f0f0f99"),
        ("revision", 13),
        ("recommendation_version_id", "018f0f0f-0f98-7f0f-8f0f-0f0f0f0f0f98"),
    ):
        mismatch = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
        mismatch["latest_pointer"][field] = value
        with pytest.raises(SchemaViolation):
            _validate_artifact_semantics(mismatch, rules)

    broken_chain = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
    broken_chain["previous_revision"] = 12
    with pytest.raises(SchemaViolation):
        _validate_artifact_semantics(broken_chain, rules)


def test_aggregate_privacy_gate_is_fail_closed() -> None:
    gate = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())["aggregate"]
    assert gate["minimum_profiles"] == 100
    assert gate["max_profile_contribution"] == 0.01
    assert gate["membership_inference_auc_95_upper"] == 0.55
    assert gate["tpr_at_1pct_fpr_max"] == 0.05
    assert gate["known_record_exact_extractions_max"] == 0
    assert gate["failure"].startswith("unpublish-latest")


def test_aggregate_uses_only_live_ir_and_excludes_five_db_backfill() -> None:
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())
    assert rules["aggregate"]["input"] == "quality-gated-live-ir-only"
    assert rules["aggregate"]["input_provenance"] == {
        "allowed_sources": ["live_ir"],
        "forbidden_sources": ["five_db", "five_db_backfill"],
    }
    assert rules["five_db"]["aggregate_input"] is False


def test_retention_auth_and_repository_change_gates_are_machine_readable() -> None:
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())
    assert rules["retention"] == {
        "profile_delete_cancellation_days": 7,
        "backup_max_days": 30,
        "superseded_artifact_body_days": 90,
        "failed_job_diagnostic_days": 30,
        "security_log_days": 30,
        "audit_log_days": 365,
        "profile_key_destroyed_on_confirmed_delete": True,
    }
    assert rules["mcp"]["dcr"] is True
    assert rules["mcp"]["discovery_metadata"] is True
    assert rules["credentials"]["account"] == {
        "user_id": {"normalization": "lowercase", "min_length": 3, "max_length": 32, "unique_after_normalization": True},
        "password": {"min_length": 12, "max_length": 128, "stored": "argon2id-hash"},
        "recovery_codes": {"count": 10, "single_use": True, "stored": "hash"},
        "email": {"optional": True, "verification_state_required": True},
        "password_reset": {"minutes": 15, "single_use": True, "stored": "hash"},
    }
    assert rules["repositories"] == {
        "phase0_mutation": False,
        "split_and_visibility_execution_issues": [22, 23],
        "license_and_visibility_decision": "deferred-to-issues-22-and-23",
    }


def test_all_local_contract_references_resolve() -> None:
    ids = {json.loads(path.read_text())["$id"] for path in CONTRACTS.glob("*.schema.json")}
    for path in CONTRACTS.glob("*.schema.json"):
        for ref in _refs(json.loads(path.read_text())):
            base = ref.partition("#")[0]
            if base:
                assert base in ids, f"{path.name}: unresolved {base}"
