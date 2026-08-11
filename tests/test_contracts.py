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
        if ref.startswith("https://oraja-training.dev/contracts/common.schema.json") or ref.startswith("common.schema.json"):
            target_document = common
            fragment = ref.partition("#")[2]
        else:
            target_document = document
            fragment = ref.partition("#")[2]
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


def _assert_latest_pointer_matches(manifest: dict[str, Any]) -> None:
    pointer = manifest["latest_pointer"]
    for field in ("profile_id", "trust_domain", "revision", "recommendation_version_id"):
        if pointer[field] != manifest[field]:
            raise SchemaViolation(f"latest_pointer.{field} does not match outer manifest")


SCHEMA_FIXTURES = (
    ("ir-event.v1.schema.json", "ir-event.valid.json", "ir-event.invalid-extra-field.json"),
    (
        "aggregate-eligibility.v1.schema.json",
        "aggregate-eligibility.valid.json",
        "aggregate-eligibility.invalid-self-hosted.json",
    ),
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
    for schema_name, _, _ in SCHEMA_FIXTURES:
        schema = json.loads((CONTRACTS / schema_name).read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "/v1/" in schema["$id"]
        assert schema["additionalProperties"] is False


def test_common_schema_refs_resolve_from_the_declared_schema_ids() -> None:
    for schema_name, _, _ in SCHEMA_FIXTURES:
        schema = json.loads((CONTRACTS / schema_name).read_text())
        common_refs = [ref for ref in _refs(schema) if "common.schema.json" in ref]
        assert common_refs
        assert all(ref.startswith(f"{COMMON_URI}#") for ref in common_refs)


def test_domain_entity_catalog_fixes_profile_limit_and_ownership() -> None:
    catalog = json.loads((CONTRACTS / "domain-entities.v1.json").read_text())
    assert catalog["profile_limit"]["default"] == 1
    assert catalog["profile_limit"]["occupying_lifecycles"] == [
        "active",
        "suspended",
        "pending_delete",
    ]
    assert catalog["profile_limit"]["released_lifecycle"] == "deleted"
    entities = {entity["name"]: entity for entity in catalog["entities"]}
    assert entities["Profile"]["owner"] == "account_id"
    assert entities["Device"]["owner"] == "profile_id"
    assert entities["PlayEvent"]["immutable_fields"][:3] == [
        "event_id",
        "profile_id",
        "trust_domain",
    ]
    self_hosted = next(item for item in catalog["trust_domains"] if item["name"] == "self_hosted")
    assert self_hosted["aggregate_policy"] == "never"


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


def test_ir_event_rejects_dp_game_mode_fixture() -> None:
    schema, common = _load("ir-event.v1.schema.json")
    invalid = json.loads((EXAMPLES / "ir-event.invalid-dp.json").read_text())
    with pytest.raises(SchemaViolation):
        _validate(schema, invalid, document=schema, common=common)


def test_ir_event_course_id_is_required_only_for_course_events() -> None:
    schema, common = _load("ir-event.v1.schema.json")
    regular = json.loads((EXAMPLES / "ir-event.valid.json").read_text())
    regular["is_course"] = True
    with pytest.raises(SchemaViolation):
        _validate(schema, regular, document=schema, common=common)

    regular["course_id"] = "018f0f0f-0f04-7f0f-8f0f-0f0f0f0f0f0f"
    _validate(schema, regular, document=schema, common=common)


def test_ir_client_cannot_self_attest_aggregate_eligibility() -> None:
    schema, common = _load("ir-event.v1.schema.json")
    payload = json.loads((EXAMPLES / "ir-event.valid.json").read_text())
    assert payload["provenance"]["aggregate_eligible"] is False

    payload["provenance"]["aggregate_eligible"] = True
    with pytest.raises(SchemaViolation):
        _validate(schema, payload, document=schema, common=common)


def test_aggregate_eligibility_requires_server_attested_consent() -> None:
    schema, common = _load("aggregate-eligibility.v1.schema.json")
    valid = json.loads((EXAMPLES / "aggregate-eligibility.valid.json").read_text())
    _validate(schema, valid, document=schema, common=common)

    without_consent = json.loads(json.dumps(valid))
    without_consent["consent"]["aggregate_training"] = False
    with pytest.raises(SchemaViolation):
        _validate(schema, without_consent, document=schema, common=common)

    revoked = json.loads(json.dumps(valid))
    revoked["consent"]["revoked_at"] = "2026-08-11T04:00:00Z"
    with pytest.raises(SchemaViolation):
        _validate(schema, revoked, document=schema, common=common)

    client_claim = json.loads(json.dumps(valid))
    client_claim["attestation"]["issuer"] = "ir_client"
    with pytest.raises(SchemaViolation):
        _validate(schema, client_claim, document=schema, common=common)


def test_self_hosted_cannot_become_aggregate_input() -> None:
    for name in (
        "container-input.invalid-self-hosted-aggregate.json",
        "container-output.invalid-self-hosted-aggregate.json",
    ):
        payload = json.loads((EXAMPLES / name).read_text())
        assert payload["trust_domain"] == "self_hosted"
        aggregate_eligible = payload["aggregate_eligible"] if "aggregate_eligible" in payload else payload["policy"]["aggregate_eligible"]
        assert aggregate_eligible is True


def test_semantic_rules_fix_digest_and_latest_pointer_boundaries() -> None:
    rules = json.loads((CONTRACTS / "semantic-rules.v1.json").read_text())
    canonicalization = rules["canonicalization"]
    assert canonicalization["algorithm"] == "RFC8785-JCS"
    assert canonicalization["encoding"] == "UTF-8"
    assert canonicalization["digest"] == "SHA-256"
    assert canonicalization["digest_is_lowercase_hex"] is True
    assert set(canonicalization["reject_before_canonicalization"]) == {
        "duplicate_object_names",
        "NaN",
        "Infinity",
        "unpaired_surrogates",
    }
    assert canonicalization["preimages"]["manifest_sha256"].startswith("JCS of the persisted manifest")
    assert canonicalization["preimages"]["job_idempotency_key"] == "job:<profile_id>:<input_digest>"
    rule_ids = {rule["id"] for rule in rules["rules"]}
    assert {"owner-chain", "digest-chain", "latest-pointer-cas", "aggregate-gate"} <= rule_ids


def test_job_input_digest_is_explicitly_bound_to_the_idempotency_key() -> None:
    for name in ("container-input.valid.json", "container-output.valid.json"):
        payload = json.loads((EXAMPLES / name).read_text())
        assert payload["job_type"]
        assert payload["input_digest"]
        assert payload["idempotency_key"] == (
            f"job:{payload['profile_id']}:{payload['input_digest']}"
        )


def test_latest_pointer_keeps_profile_and_trust_partition() -> None:
    schema, common = _load("artifact-manifest.v1.schema.json")
    payload = json.loads((EXAMPLES / "artifact-manifest.valid.json").read_text())
    _validate(schema, payload, document=schema, common=common)
    _assert_latest_pointer_matches(payload)
    pointer = payload["latest_pointer"]
    assert pointer["profile_id"] == payload["profile_id"]
    assert pointer["trust_domain"] == payload["trust_domain"]
    assert pointer["revision"] == payload["revision"]
    assert pointer["recommendation_version_id"] == payload["recommendation_version_id"]

    # JSON Schema can validate the field shape, while equality is a
    # cross-record semantic rule enforced by the publisher transaction.
    pointer["trust_domain"] = "self_hosted"
    with pytest.raises(SchemaViolation):
        _assert_latest_pointer_matches(payload)
