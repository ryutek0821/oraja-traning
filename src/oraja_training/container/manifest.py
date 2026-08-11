"""Validation and deterministic serialization for Container job manifests.

The checked-in JSON Schemas describe the wire shape.  This module keeps the
runtime boundary dependency-free and adds the cross-record checks that a
Container must enforce before it opens an uploaded database.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any


UUIDV7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
OBJECT_KEY = re.compile(
    r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$"
)
KEY_REF = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T")

JOB_TYPES = frozenset(
    {"five_db_backfill", "monthly_audit", "single_play_incremental"}
)
DATABASE_NAMES = (
    "score.db",
    "scoredatalog.db",
    "scorelog.db",
    "songdata.db",
    "songinfo.db",
)
INPUT_KEYS = frozenset(
    {
        "contract",
        "schema_version",
        "job_id",
        "idempotency_key",
        "job_type",
        "input_digest",
        "account_id",
        "profile_id",
        "trust_domain",
        "source_manifest_sha256",
        "input_bundle",
        "requested_at",
        "game_mode",
        "policy",
    }
)
OUTPUT_KEYS = frozenset(
    {
        "contract",
        "schema_version",
        "job_id",
        "idempotency_key",
        "job_type",
        "input_digest",
        "account_id",
        "profile_id",
        "trust_domain",
        "aggregate_eligible",
        "input_manifest_sha256",
        "output_revision",
        "generated_at",
        "normalization",
        "counters",
        "artifacts",
        "raw_db_exported",
    }
)


class ManifestError(ValueError):
    """Raised when a manifest is not safe to hand to the processor."""

    def __init__(self, message: str, *, code: str = "invalid_contract") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InputManifest(Mapping[str, Any]):
    """Validated immutable view of a ``container-input-manifest.v1`` object."""

    value: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)

    @property
    def job_id(self) -> str:
        return str(self.value["job_id"])

    @property
    def job_type(self) -> str:
        return str(self.value["job_type"])

    @property
    def profile_id(self) -> str:
        return str(self.value["profile_id"])

    @property
    def account_id(self) -> str:
        return str(self.value["account_id"])

    @property
    def trust_domain(self) -> str:
        return str(self.value["trust_domain"])

    @property
    def input_digest(self) -> str:
        return str(self.value["input_digest"])

    @property
    def input_bundle(self) -> Mapping[str, Any]:
        return self.value["input_bundle"]

    @property
    def aggregate_eligible(self) -> bool:
        return bool(self.value["policy"]["aggregate_eligible"])

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible copy suitable for persistence."""

        return json.loads(canonical_json(self.value))


def _duplicate_key_check(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(
                "duplicate object member",
                code="invalid_json",
            )
        result[key] = value
    return result


def parse_json(payload: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite numbers."""

    try:
        return json.loads(
            payload,
            object_pairs_hook=_duplicate_key_check,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(f"non-finite JSON number: {value}", code="invalid_json")
            ),
        )
    except ManifestError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestError("invalid JSON", code="invalid_json") from exc


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ManifestError("manifest contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ManifestError("manifest object member names must be strings")
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def canonical_json(value: Any) -> str:
    """Serialize a manifest using the JCS-compatible subset used by v1.

    Container manifests contain only strings, booleans, integers, arrays and
    objects.  For that subset, sorted compact JSON with UTF-8 output is the
    RFC 8785 representation.  Rejecting non-finite values before encoding is
    important because Python's encoder otherwise emits non-standard tokens.
    """

    _reject_non_finite(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ManifestError("manifest is not canonicalizable") from exc


def manifest_sha256(value: Mapping[str, Any]) -> str:
    """Return the lower-case SHA-256 digest of a persisted manifest."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], name: str) -> None:
    missing = expected.difference(value)
    extra = set(value).difference(expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)!r}")
        if extra:
            details.append(f"unexpected={sorted(extra)!r}")
        raise ManifestError(f"{name} keys are invalid ({', '.join(details)})")


def _required(value: Mapping[str, Any], keys: set[str] | frozenset[str], name: str) -> None:
    missing = keys.difference(value)
    if missing:
        raise ManifestError(f"{name} is missing required members: {sorted(missing)!r}")


def _string(value: Any, name: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ManifestError(f"{name} has an invalid format")
    return value


def _uuid(value: Any, name: str) -> str:
    return _string(value, name, pattern=UUIDV7)


def _sha(value: Any, name: str) -> str:
    return _string(value, name, pattern=SHA256)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: Any, name: str) -> str:
    rendered = _string(value, name)
    if TIMESTAMP.match(rendered) is None:
        raise ManifestError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{name} must contain a timezone")
    return rendered


def _validate_object_key(value: Any, name: str) -> str:
    return _string(value, name, pattern=OBJECT_KEY)


def _validate_profile_prefix(object_key: str, profile_id: str, name: str) -> None:
    prefix = f"profiles/{profile_id}/"
    if not object_key.startswith(prefix):
        raise ManifestError(f"{name} must stay inside the profile partition")


def validate_input_manifest(value: Mapping[str, Any]) -> InputManifest:
    """Validate a container input and its trust/idempotency partition."""

    _reject_non_finite(value)
    manifest = _object(value, "input manifest")
    _exact_keys(manifest, INPUT_KEYS, "input manifest")
    if manifest["contract"] != "container-input-manifest" or manifest["schema_version"] != "1":
        raise ManifestError("unsupported container input contract")
    job_id = _uuid(manifest["job_id"], "job_id")
    profile_id = _uuid(manifest["profile_id"], "profile_id")
    _uuid(manifest["account_id"], "account_id")
    input_digest = _sha(manifest["input_digest"], "input_digest")
    idempotency_key = _string(manifest["idempotency_key"], "idempotency_key", pattern=IDEMPOTENCY_KEY)
    expected_key = f"job:{profile_id}:{input_digest}"
    if idempotency_key != expected_key:
        raise ManifestError("idempotency_key is not bound to profile and input_digest")
    job_type = _string(manifest["job_type"], "job_type")
    if job_type not in JOB_TYPES:
        raise ManifestError("unsupported container job type", code="unsupported_job_type")
    trust_domain = manifest["trust_domain"]
    if trust_domain not in {"official", "self_hosted"}:
        raise ManifestError("trust_domain is invalid")
    _sha(manifest["source_manifest_sha256"], "source_manifest_sha256")
    _timestamp(manifest["requested_at"], "requested_at")
    if manifest["game_mode"] != "SP7":
        raise ManifestError("only SP7 jobs are supported", code="unsupported_game_mode")

    bundle = _object(manifest["input_bundle"], "input_bundle")
    _exact_keys(bundle, {"object_key", "sha256", "size_bytes", "encryption", "key_ref"}, "input_bundle")
    object_key = _validate_object_key(bundle["object_key"], "input_bundle.object_key")
    _validate_profile_prefix(object_key, profile_id, "input_bundle.object_key")
    _sha(bundle["sha256"], "input_bundle.sha256")
    size_bytes = _integer(bundle["size_bytes"], "input_bundle.size_bytes", minimum=1)
    if size_bytes > 5 * 1024 * 1024 * 1024:
        raise ManifestError("input bundle is too large", code="payload_too_large")
    if bundle["encryption"] != "envelope-v1":
        raise ManifestError("unsupported input encryption")
    key_ref = _string(bundle["key_ref"], "input_bundle.key_ref", pattern=KEY_REF)
    if key_ref.startswith("profile-key:") and key_ref.removeprefix("profile-key:") != profile_id:
        raise ManifestError("input key reference crosses the profile partition")

    policy = _object(manifest["policy"], "policy")
    _exact_keys(policy, {"aggregate_eligible", "include_raw_db_in_model"}, "policy")
    if not isinstance(policy["aggregate_eligible"], bool):
        raise ManifestError("policy.aggregate_eligible must be boolean")
    if policy["include_raw_db_in_model"] is not False:
        raise ManifestError("raw databases cannot be passed directly to the model")
    if trust_domain == "self_hosted" and policy["aggregate_eligible"] is not False:
        raise ManifestError("self-hosted input cannot be aggregate eligible")

    # Keep this field in the error path so an invalid UUID cannot be used as
    # an object partition even when this function is called outside Worker.
    if not job_id:
        raise ManifestError("job_id is invalid")
    return InputManifest(dict(manifest))


def validate_upload_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the accepted five-DB source manifest used by a job input."""

    manifest = _object(value, "upload manifest")
    required = {
        "contract", "schema_version", "manifest_id", "account_id", "profile_id",
        "created_at", "game_mode", "trust_domain", "files", "snapshot",
        "encryption", "privacy",
    }
    _exact_keys(manifest, required, "upload manifest")
    if manifest["contract"] != "five-db-upload-manifest" or manifest["schema_version"] != "1":
        raise ManifestError("unsupported upload contract")
    _uuid(manifest["manifest_id"], "manifest_id")
    account_id = _uuid(manifest["account_id"], "account_id")
    profile_id = _uuid(manifest["profile_id"], "profile_id")
    _timestamp(manifest["created_at"], "created_at")
    if manifest["game_mode"] != "SP7":
        raise ManifestError("only SP7 uploads are supported", code="unsupported_game_mode")
    if manifest["trust_domain"] not in {"official", "self_hosted"}:
        raise ManifestError("upload trust_domain is invalid")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(DATABASE_NAMES):
        raise ManifestError("upload must contain exactly five database files")
    names: list[str] = []
    for index, item in enumerate(files):
        entry = _object(item, f"files[{index}]")
        _exact_keys(entry, {"file_name", "sha256", "size_bytes", "object_key"}, f"files[{index}]")
        name = _string(entry["file_name"], f"files[{index}].file_name")
        if name not in DATABASE_NAMES or name in names:
            raise ManifestError("upload database file allowlist is invalid")
        names.append(name)
        _sha(entry["sha256"], f"files[{index}].sha256")
        size = _integer(entry["size_bytes"], f"files[{index}].size_bytes", minimum=1)
        if size > 5 * 1024 * 1024 * 1024:
            raise ManifestError("upload database file is too large", code="payload_too_large")
        key = _validate_object_key(entry["object_key"], f"files[{index}].object_key")
        _validate_profile_prefix(key, profile_id, f"files[{index}].object_key")
    if set(names) != set(DATABASE_NAMES):
        raise ManifestError("upload must contain every required database exactly once")

    snapshot = _object(manifest["snapshot"], "snapshot")
    _exact_keys(snapshot, {"static_snapshot", "wal_present", "shm_present", "source_generation"}, "snapshot")
    if snapshot["static_snapshot"] is not True or snapshot["wal_present"] is not False or snapshot["shm_present"] is not False:
        raise ManifestError("upload must be a static sidecar-free snapshot")
    _uuid(snapshot["source_generation"], "snapshot.source_generation")
    encryption = _object(manifest["encryption"], "encryption")
    _exact_keys(encryption, {"scheme", "scope"}, "encryption")
    if encryption != {"scheme": "envelope-v1", "scope": "profile"}:
        raise ManifestError("unsupported upload encryption")
    privacy = _object(manifest["privacy"], "privacy")
    _exact_keys(privacy, {"contains_bms", "contains_replay", "aggregate_eligible"}, "privacy")
    if any(privacy[key] is not False for key in ("contains_bms", "contains_replay", "aggregate_eligible")):
        raise ManifestError("upload privacy flags are not allowed")
    # Avoid accidentally accepting an object with a non-string account value
    # after the structural checks above; this also documents the owner pair.
    if not account_id or not profile_id:
        raise ManifestError("upload owner is invalid")
    return dict(manifest)


def build_output_manifest(
    input_manifest: InputManifest | Mapping[str, Any],
    *,
    input_manifest_sha256: str,
    output_revision: int,
    generated_at: str,
    counters: Mapping[str, int],
    artifacts: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a strict ``container-output-manifest.v1`` object."""

    source = input_manifest.value if isinstance(input_manifest, InputManifest) else input_manifest
    validated = validate_input_manifest(source)
    _sha(input_manifest_sha256, "input_manifest_sha256")
    _integer(output_revision, "output_revision", minimum=1)
    _timestamp(generated_at, "generated_at")
    _exact_keys(counters, {"accepted_events", "rejected_events", "course_events"}, "counters")
    normalized_counters: dict[str, int] = {}
    for key in ("accepted_events", "rejected_events", "course_events"):
        normalized_counters[key] = _integer(counters[key], f"counters.{key}")
    if not artifacts:
        raise ManifestError("output must contain at least one artifact")
    normalized_artifacts: list[dict[str, Any]] = []
    for index, item in enumerate(artifacts):
        artifact = _object(item, f"artifacts[{index}]")
        _exact_keys(artifact, {"kind", "object_key", "sha256", "size_bytes", "content_type"}, f"artifacts[{index}]")
        if artifact["kind"] not in {"normalized_events", "feature_input", "model_input"}:
            raise ManifestError("output artifact kind is not allowed")
        key = _validate_object_key(artifact["object_key"], f"artifacts[{index}].object_key")
        _validate_profile_prefix(key, validated.profile_id, f"artifacts[{index}].object_key")
        _sha(artifact["sha256"], f"artifacts[{index}].sha256")
        _integer(artifact["size_bytes"], f"artifacts[{index}].size_bytes")
        if artifact["content_type"] not in {"application/json", "application/zstd"}:
            raise ManifestError("output artifact content type is not allowed")
        normalized_artifacts.append(dict(artifact))
    aggregate_eligible = validated.aggregate_eligible
    if validated.trust_domain == "self_hosted":
        aggregate_eligible = False
    result = {
        "contract": "container-output-manifest",
        "schema_version": "1",
        "job_id": validated.job_id,
        "idempotency_key": validated["idempotency_key"],
        "job_type": validated.job_type,
        "input_digest": validated.input_digest,
        "account_id": validated.account_id,
        "profile_id": validated.profile_id,
        "trust_domain": validated.trust_domain,
        "aggregate_eligible": aggregate_eligible,
        "input_manifest_sha256": input_manifest_sha256,
        "output_revision": output_revision,
        "generated_at": generated_at,
        "normalization": {"version": "normalization-v1", "event_contract": "ir-event.v1"},
        "counters": normalized_counters,
        "artifacts": normalized_artifacts,
        "raw_db_exported": False,
    }
    validate_output_manifest(result, input_manifest=validated)
    return result


def validate_output_manifest(
    value: Mapping[str, Any], *, input_manifest: InputManifest | None = None
) -> Mapping[str, Any]:
    """Validate output shape and, when supplied, its input digest chain."""

    manifest = _object(value, "output manifest")
    _exact_keys(manifest, OUTPUT_KEYS, "output manifest")
    if manifest["contract"] != "container-output-manifest" or manifest["schema_version"] != "1":
        raise ManifestError("unsupported container output contract")
    _uuid(manifest["job_id"], "job_id")
    _string(manifest["idempotency_key"], "idempotency_key", pattern=IDEMPOTENCY_KEY)
    _string(manifest["job_type"], "job_type")
    _sha(manifest["input_digest"], "input_digest")
    _uuid(manifest["account_id"], "account_id")
    profile_id = _uuid(manifest["profile_id"], "profile_id")
    _sha(manifest["input_manifest_sha256"], "input_manifest_sha256")
    _integer(manifest["output_revision"], "output_revision", minimum=1)
    _timestamp(manifest["generated_at"], "generated_at")
    if manifest["trust_domain"] not in {"official", "self_hosted"}:
        raise ManifestError("output trust_domain is invalid")
    if not isinstance(manifest["aggregate_eligible"], bool):
        raise ManifestError("output aggregate_eligible must be boolean")
    if manifest["trust_domain"] == "self_hosted" and manifest["aggregate_eligible"] is not False:
        raise ManifestError("self-hosted output cannot be aggregate eligible")
    normalization = _object(manifest["normalization"], "normalization")
    _exact_keys(normalization, {"version", "event_contract"}, "normalization")
    if normalization != {"version": "normalization-v1", "event_contract": "ir-event.v1"}:
        raise ManifestError("unsupported normalization contract")
    counters = _object(manifest["counters"], "counters")
    _exact_keys(counters, {"accepted_events", "rejected_events", "course_events"}, "counters")
    for key, item in counters.items():
        _integer(item, f"counters.{key}")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ManifestError("output artifacts must be a non-empty array")
    for index, item in enumerate(artifacts):
        artifact = _object(item, f"artifacts[{index}]")
        _exact_keys(artifact, {"kind", "object_key", "sha256", "size_bytes", "content_type"}, f"artifacts[{index}]")
        if artifact["kind"] not in {"normalized_events", "feature_input", "model_input"}:
            raise ManifestError("output artifact kind is not allowed")
        key = _validate_object_key(artifact["object_key"], f"artifacts[{index}].object_key")
        _validate_profile_prefix(key, profile_id, f"artifacts[{index}].object_key")
        _sha(artifact["sha256"], f"artifacts[{index}].sha256")
        _integer(artifact["size_bytes"], f"artifacts[{index}].size_bytes")
        if artifact["content_type"] not in {"application/json", "application/zstd"}:
            raise ManifestError("output artifact content type is not allowed")
    if manifest["raw_db_exported"] is not False:
        raise ManifestError("raw database export is forbidden")
    if input_manifest is not None:
        for key in ("job_id", "idempotency_key", "job_type", "input_digest", "account_id", "profile_id", "trust_domain"):
            if manifest[key] != input_manifest[key]:
                raise ManifestError(f"output {key} does not match input manifest")
        if manifest["idempotency_key"] != f"job:{profile_id}:{manifest['input_digest']}":
            raise ManifestError("output idempotency key is not bound to input digest")
    return dict(manifest)
