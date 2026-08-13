"""HTTP job entrypoint for the Cloudflare Python Container.

The Worker can stream the encrypted R2 object as the request body and send a
base64url JSON input manifest in ``X-Container-Input-Manifest``.  A local
fixture mode accepts a bundle filename below ``INPUT_ROOT``; it is useful for
smoke tests and does not permit arbitrary filesystem paths.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
import os
from pathlib import Path
import re
from typing import Any
import uuid

from oraja_training.container import (
    ContainerAdapter,
    ContainerError,
    PassthroughDecryptor,
)
from oraja_training.container.manifest import ManifestError, parse_json

try:  # direct execution from the container directory
    from health import HealthHandler, json_response, serve
except ImportError:  # pragma: no cover - package/module execution fallback
    from .health import HealthHandler, json_response, serve


MAX_MANIFEST_HEADER_BYTES = 64 * 1024
SAFE_BUNDLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class _BoundedBody:
    """Expose exactly Content-Length bytes and then EOF on keep-alive sockets."""

    def __init__(self, source: Any, length: int) -> None:
        self._source = source
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._source.read(requested)
        self._remaining -= len(chunk)
        return chunk


def _error_payload(error: ContainerError | ManifestError) -> dict[str, Any]:
    code = getattr(error, "code", "invalid_contract")
    messages = {
        "invalid_json": "request JSON is invalid",
        "invalid_contract": "request contract is invalid",
        "invalid_input": "input bundle is invalid",
        "unsupported_game_mode": "only SP7 jobs are supported",
        "unsupported_job_type": "job type is not supported",
        "payload_too_large": "request exceeds the permitted size",
        "resource_limit_exceeded": "job resource limit exceeded",
        "job_cancelled": "job cancelled",
        "temporary_unavailable": "processor is temporarily unavailable",
        "idempotency_conflict": "immutable job output conflicts with an existing object",
    }
    return {
        "error": {
            "code": code,
            "message": messages.get(code, "request could not be processed"),
            "request_id": str(uuid.uuid4()),
            "retryable": bool(getattr(error, "retryable", False)),
            "retry_after_seconds": 5 if bool(getattr(error, "retryable", False)) else None,
        }
    }


def _decode_manifest_header(value: str) -> Mapping[str, Any]:
    if len(value.encode("ascii", errors="ignore")) > MAX_MANIFEST_HEADER_BYTES:
        raise ManifestError("manifest header is too large", code="payload_too_large")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        manifest = parse_json(decoded)
    except (ValueError, UnicodeError) as exc:
        raise ManifestError("manifest header is invalid", code="invalid_json") from exc
    if not isinstance(manifest, Mapping):
        raise ManifestError("manifest header must contain an object", code="invalid_contract")
    return manifest


def _safe_fixture_path(root: Path, name: str) -> Path:
    if SAFE_BUNDLE_NAME.fullmatch(name) is None:
        raise ManifestError("bundle name is invalid", code="invalid_contract")
    base = root.expanduser().resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ManifestError("bundle name crosses input partition", code="invalid_contract") from exc
    return candidate


class ProcessorHandler(HealthHandler):
    """Handle one streamed encrypted bundle at a time."""

    adapter: ContainerAdapter
    input_root: Path

    def _read_json_request(self) -> Mapping[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ManifestError("content length is invalid", code="invalid_json") from exc
        if length <= 0 or length > MAX_MANIFEST_HEADER_BYTES * 4:
            raise ManifestError("JSON request is too large", code="payload_too_large")
        body = self.rfile.read(length)
        value = parse_json(body)
        if not isinstance(value, Mapping):
            raise ManifestError("request must contain an object", code="invalid_contract")
        return value

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/jobs":
            json_response(self, {"error": {"code": "not_found"}}, status=404)
            return
        try:
            header_manifest = self.headers.get("X-Container-Input-Manifest")
            if header_manifest:
                manifest = _decode_manifest_header(header_manifest)
                input_bundle = manifest.get("input_bundle")
                expected_size = input_bundle.get("size_bytes") if isinstance(input_bundle, Mapping) else None
                raw_length = self.headers.get("Content-Length")
                try:
                    content_length = int(raw_length or "-1")
                except ValueError as exc:
                    raise ManifestError("content length is invalid", code="invalid_contract") from exc
                if not isinstance(expected_size, int) or content_length != expected_size:
                    raise ManifestError("content length does not match manifest", code="invalid_contract")
                result = self.adapter.run(
                    manifest,
                    bundle=_BoundedBody(self.rfile, content_length),
                )
            else:
                request = self._read_json_request()
                manifest = request.get("manifest")
                if not isinstance(manifest, Mapping):
                    raise ManifestError("manifest is required", code="invalid_contract")
                bundle_name = request.get("bundle_name")
                if not isinstance(bundle_name, str):
                    raise ManifestError("bundle_name is required", code="invalid_contract")
                result = self.adapter.run(
                    manifest,
                    bundle=_safe_fixture_path(self.input_root, bundle_name),
                )
            json_response(
                self,
                {
                    "status": "succeeded",
                    "output_manifest": result.output_manifest,
                    "output_manifest_sha256": result.output_manifest_sha256,
                },
                status=200,
            )
        except (ContainerError, ManifestError) as exc:
            json_response(self, _error_payload(exc), status=getattr(exc, "status", 422))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            # Do not serialize exception text: it may contain a source path or
            # SQLite payload.  The job is still visible through its digest at
            # the Worker boundary.
            error = ContainerError(
                "processor failure",
                code="temporary_unavailable",
                retryable=True,
                status=503,
            )
            json_response(self, _error_payload(error), status=503)


def _build_handler() -> type[ProcessorHandler]:
    allow_fixture = os.environ.get("ORAJA_ALLOW_PLAINTEXT_FIXTURE", "0") == "1"
    # The production decryptor is supplied by the deployment image/key
    # provider.  Passthrough is opt-in and only for non-production fixtures.
    adapter = ContainerAdapter(
        decryptor=PassthroughDecryptor() if allow_fixture else None,
    )
    input_root = Path(os.environ.get("INPUT_ROOT", "/tmp/oraja-inputs"))

    class BoundProcessorHandler(ProcessorHandler):
        pass

    BoundProcessorHandler.adapter = adapter
    BoundProcessorHandler.input_root = input_root
    return BoundProcessorHandler


if __name__ == "__main__":
    serve(_build_handler())
