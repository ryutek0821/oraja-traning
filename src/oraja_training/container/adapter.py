"""Profile-scoped Container adapter for the public Python core.

This module owns all filesystem, SQLite, bundle, and artifact I/O.  The
feature/model/menu functions it calls receive repository ports or domain
values only.  A production Worker can implement ``ObjectStore`` and
``StreamDecryptor`` around R2 and its envelope-key service; local tests use
the small in-memory/file implementations below.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from typing import Any, BinaryIO, Protocol
import zipfile

from oraja_training.collect import backfill, snapshot
from oraja_training.collect.source import source_signature
from oraja_training.db import readers, store
from oraja_training.db.feature_adapter import SQLiteFeatureRepository
from oraja_training.db.model_adapter import (
    SQLiteModelRepository,
    SQLiteUnitOfWork,
)
from oraja_training.db.recommendation_adapter import SQLiteRecommendationRepository
from oraja_training.domain import ProfileContext, ProfileSettings, RecommendationInput
from oraja_training.features import build_from_repository
from oraja_training.model import FitResult, fit_from_port
from oraja_training.plan import (
    MenuBuildError,
    build_session_from_input,
    build_session_from_repository,
    recommendation_output,
    table_payloads,
)

from .manifest import (
    DATABASE_NAMES,
    InputManifest,
    ManifestError,
    build_output_manifest,
    canonical_json,
    manifest_sha256,
    validate_input_manifest,
    validate_output_manifest,
    validate_upload_manifest,
)


class ContainerError(RuntimeError):
    """Sanitized, structured failure from a Container job."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "temporary_unavailable",
        retryable: bool = False,
        status: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


class JobCancelled(ContainerError):
    def __init__(self) -> None:
        super().__init__("job cancelled", code="job_cancelled", retryable=False, status=409)


class ResourceLimitExceeded(ContainerError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            f"job {resource} limit exceeded",
            code="resource_limit_exceeded",
            retryable=False,
            status=413,
        )


class InputIntegrityError(ContainerError):
    def __init__(self, code: str = "invalid_input") -> None:
        super().__init__(
            "input bundle failed integrity or schema validation",
            code=code,
            retryable=False,
            status=422,
        )


class ArtifactConflict(ContainerError):
    def __init__(self) -> None:
        super().__init__(
            "immutable artifact key already contains different content",
            code="idempotency_conflict",
            retryable=False,
            status=409,
        )


class ObjectStore(Protocol):
    """Minimal R2-compatible source object port."""

    def get(self, object_key: str) -> Any: ...


class StreamDecryptor(Protocol):
    """Envelope-v1 decryption port; implementations must yield plaintext chunks."""

    def decrypt(
        self,
        source: BinaryIO,
        *,
        profile_id: str,
        key_ref: str,
    ) -> Iterable[bytes]: ...


class ArtifactStore(Protocol):
    """Immutable artifact writer used in place of a Cloudflare R2 binding."""

    def put(self, object_key: str, content: bytes, *, content_type: str) -> "ArtifactRecord": ...


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    kind: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str = "application/json"

    def as_manifest(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "object_key": self.object_key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


class MemoryArtifactStore:
    """Small deterministic store for tests and dry local adapter runs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, content: bytes, *, content_type: str) -> ArtifactRecord:
        previous = self.objects.get(object_key)
        if previous is not None and previous != content:
            raise ArtifactConflict()
        self.objects.setdefault(object_key, bytes(content))
        return ArtifactRecord(
            kind=PurePosixPath(object_key).stem,
            object_key=object_key,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type=content_type,
        )


class FileArtifactStore:
    """Local R2-like artifact store with atomic immutable writes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def put(self, object_key: str, content: bytes, *, content_type: str) -> ArtifactRecord:
        relative = PurePosixPath(object_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ContainerError("invalid artifact partition", code="invalid_contract", status=422)
        destination = (self.root / Path(*relative.parts)).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise ContainerError("invalid artifact partition", code="invalid_contract", status=422) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != content:
                raise ArtifactConflict()
        else:
            temporary = destination.with_name(f".{destination.name}.{threading.get_ident()}.tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        return ArtifactRecord(
            kind=relative.stem,
            object_key=object_key,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type=content_type,
        )


class PassthroughDecryptor:
    """Explicit test/local decryptor for an already-decrypted fixture archive."""

    def decrypt(self, source: BinaryIO, *, profile_id: str, key_ref: str) -> Iterable[bytes]:
        del profile_id, key_ref
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return
            yield chunk


@dataclass(frozen=True, slots=True)
class JobLimits:
    """Hard limits checked by the adapter before and during processing."""

    max_input_bytes: int = 5 * 1024 * 1024 * 1024
    max_database_bytes: int = 5 * 1024 * 1024 * 1024
    max_temp_bytes: int = 10 * 1024 * 1024 * 1024
    max_output_bytes: int = 512 * 1024 * 1024
    max_runtime_seconds: float = 15 * 60
    max_memory_bytes: int | None = None
    max_cpu_seconds: int | None = None

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_input_bytes,
                self.max_database_bytes,
                self.max_temp_bytes,
                self.max_output_bytes,
            )
        ):
            raise ValueError("Container byte limits must be positive")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")


class CancellationToken:
    """Thread-safe cancellation signal checked between and during phases."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise JobCancelled()


@dataclass(frozen=True, slots=True)
class JobResult:
    output_manifest: Mapping[str, Any]
    output_manifest_sha256: str
    artifacts: tuple[ArtifactRecord, ...]
    counters: Mapping[str, int]
    fit_result: FitResult
    recommendation: Any | None = None
    source_manifest_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_manifest": dict(self.output_manifest),
            "output_manifest_sha256": self.output_manifest_sha256,
            "artifacts": [artifact.as_manifest() for artifact in self.artifacts],
            "counters": dict(self.counters),
            "recommendation": None if self.recommendation is None else asdict(self.recommendation),
        }


def _iter_chunks(source: Any, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        yield bytes(source)
        return
    if isinstance(source, (str, Path)):
        with Path(source).open("rb") as handle:
            yield from _iter_chunks(handle, chunk_size=chunk_size)
        return
    if hasattr(source, "read"):
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                return
            if not isinstance(chunk, bytes):
                raise InputIntegrityError()
            yield chunk
        return
    try:
        iterator = iter(source)
    except TypeError as exc:
        raise InputIntegrityError() from exc
    for chunk in iterator:
        if not isinstance(chunk, bytes):
            raise InputIntegrityError()
        if chunk:
            yield chunk


def _copy_limited(
    source: Any,
    destination: BinaryIO,
    *,
    limit: int,
    token: CancellationToken,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for chunk in _iter_chunks(source):
        token.check()
        total += len(chunk)
        if total > limit:
            raise ResourceLimitExceeded("input")
        digest.update(chunk)
        destination.write(chunk)
    return digest.hexdigest(), total


def _iso_timestamp(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch_seconds), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _safe_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise InputIntegrityError("invalid_input")
    rendered = path.as_posix()
    if rendered not in DATABASE_NAMES:
        raise InputIntegrityError("invalid_input")
    return rendered


def _copy_member(source: BinaryIO, destination: Path, *, limits: JobLimits, token: CancellationToken) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as target:
        _copy_limited(source, target, limit=limits.max_database_bytes, token=token)


def _extract_archive(archive_path: Path, destination: Path, *, limits: JobLimits, token: CancellationToken) -> None:
    names: set[str] = set()
    if zipfile.is_zipfile(archive_path):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    name = _safe_member_name(info.filename)
                    if info.is_dir() or name in names:
                        raise InputIntegrityError()
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise InputIntegrityError()
                    if info.file_size > limits.max_database_bytes:
                        raise ResourceLimitExceeded("database")
                    names.add(name)
                    with archive.open(info, "r") as source:
                        _copy_member(source, destination / name, limits=limits, token=token)
        except (zipfile.BadZipFile, OSError) as exc:
            raise InputIntegrityError() from exc
    else:
        try:
            with tarfile.open(archive_path, mode="r:*") as archive:
                for member in archive.getmembers():
                    name = _safe_member_name(member.name)
                    if not member.isfile() or name in names:
                        raise InputIntegrityError()
                    if member.size > limits.max_database_bytes:
                        raise ResourceLimitExceeded("database")
                    names.add(name)
                    source = archive.extractfile(member)
                    if source is None:
                        raise InputIntegrityError()
                    with source:
                        _copy_member(source, destination / name, limits=limits, token=token)
        except (tarfile.TarError, OSError) as exc:
            raise InputIntegrityError() from exc
    if names != set(DATABASE_NAMES):
        raise InputIntegrityError()


def _verify_bundle_directory(
    source_dir: Path,
    *,
    limits: JobLimits,
    upload_manifest: Mapping[str, Any] | None,
) -> None:
    if not source_dir.is_dir():
        raise InputIntegrityError()
    if any(
        source_dir.joinpath(f"{name}{suffix}").exists()
        for name in DATABASE_NAMES
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise InputIntegrityError("invalid_input")
    if any(path.is_dir() for path in source_dir.iterdir()):
        raise InputIntegrityError("invalid_input")
    if set(path.name for path in source_dir.iterdir() if path.is_file()) - set(DATABASE_NAMES):
        raise InputIntegrityError("invalid_input")
    expected: dict[str, Mapping[str, Any]] = {}
    if upload_manifest is not None:
        checked = validate_upload_manifest(upload_manifest)
        expected = {str(item["file_name"]): item for item in checked["files"]}
    for name in DATABASE_NAMES:
        path = source_dir / name
        if not path.is_file():
            raise InputIntegrityError()
        size = path.stat().st_size
        if size <= 0:
            raise InputIntegrityError()
        if size > limits.max_database_bytes:
            raise ResourceLimitExceeded("database")
        if name in expected:
            item = expected[name]
            if size != int(item["size_bytes"]):
                raise InputIntegrityError()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                raise InputIntegrityError()


def _source_paths(source_dir: Path) -> tuple[Path, ...]:
    return tuple(source_dir / name for name in DATABASE_NAMES)


def _source_signatures(source_dir: Path) -> dict[Path, Any]:
    try:
        return {path: source_signature(path) for path in _source_paths(source_dir)}
    except OSError as exc:
        raise InputIntegrityError() from exc


def _preflight_sources(source_dir: Path, *, token: CancellationToken) -> None:
    """Re-open all five databases and check SQLite/schema integrity first."""

    before = _source_signatures(source_dir)
    try:
        with closing(readers.open_snapshot(source_dir / "score.db")) as conn:
            token.check()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InputIntegrityError()
            readers.read_score(conn)
            readers.read_player(conn)
        with closing(readers.open_snapshot(source_dir / "scoredatalog.db")) as conn:
            token.check()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InputIntegrityError()
            readers.read_scoredatalog(conn)
        with closing(readers.open_snapshot(source_dir / "scorelog.db")) as conn:
            token.check()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InputIntegrityError()
            readers.read_scorelog(conn)
        with closing(readers.open_snapshot(source_dir / "songdata.db")) as conn:
            token.check()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InputIntegrityError()
            readers.read_songs(conn)
        with closing(readers.open_snapshot(source_dir / "songinfo.db")) as conn:
            token.check()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InputIntegrityError()
            readers.read_songinfo(conn)
    except (sqlite3.DatabaseError, readers.ReaderSchemaError, OSError) as exc:
        raise InputIntegrityError() from exc
    if _source_signatures(source_dir) != before:
        raise InputIntegrityError()


def _model_dict(model: Any) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "target": model.target,
        "version": model.version,
        "trained_at": model.trained_at,
        "n_train": model.n_train,
        "weights": list(model.weights),
        "means": list(model.means),
        "scales": list(model.scales),
        "feature_names": list(model.feature_names),
        "metrics": dict(model.metrics),
        "description": model.description,
    }


def _json_artifact(value: Any, *, limits: JobLimits) -> bytes:
    content = canonical_json(value).encode("utf-8")
    if len(content) > limits.max_output_bytes:
        raise ResourceLimitExceeded("output")
    return content


def _table_settings(context: Mapping[str, Any] | None) -> ProfileSettings | None:
    if context is None:
        return None
    settings = context.get("settings")
    if not isinstance(settings, Mapping):
        raise ContainerError("table settings are missing", code="table_input_unavailable", status=422)
    try:
        revision = int(context.get("settings_revision", 0))
        if revision < 1:
            raise ValueError
        parsed = ProfileSettings(
            timezone=str(settings["timezone"]),
            target_judged=int(settings["target_judged"]),
            reserve_judged=int(settings["reserve_judged"]),
            readiness=str(settings["readiness"]),
        )
        if not 10_000 <= parsed.target_judged <= 1_000_000:
            raise ValueError
        if not 0 <= parsed.reserve_judged <= min(500_000, parsed.target_judged):
            raise ValueError
        return parsed
    except (KeyError, TypeError, ValueError) as exc:
        raise ContainerError("table settings are invalid", code="invalid_contract", status=422) from exc


def _seed_table_catalog(
    connection: sqlite3.Connection,
    context: Mapping[str, Any],
    fetched_at: int,
) -> None:
    rows = context.get("catalog_entries")
    manifest_hash = context.get("catalog_manifest_sha256")
    if (
        not isinstance(rows, list)
        or not 1 <= len(rows) <= 20_000
        or not isinstance(manifest_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None
    ):
        raise ContainerError("table catalog is unavailable", code="table_input_unavailable", status=422)
    if hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest() != manifest_hash:
        raise ContainerError("table catalog digest is invalid", code="invalid_contract", status=422)
    accepted: list[tuple[str, str, str, str | None, str, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContainerError("table catalog is invalid", code="invalid_contract", status=422)
        table_id = row.get("table_id")
        level = row.get("level")
        sha256 = row.get("sha256")
        md5 = row.get("md5")
        title = row.get("title")
        if (
            not isinstance(table_id, str) or not 1 <= len(table_id) <= 64
            or not isinstance(level, str) or not 1 <= len(level) <= 64
            or not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or (md5 is not None and (not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None))
            or not isinstance(title, str) or len(title) > 512
        ):
            raise ContainerError("table catalog is invalid", code="invalid_contract", status=422)
        accepted.append((table_id, level, sha256, md5, title, fetched_at))
    with connection:
        connection.execute("DELETE FROM table_entries")
        connection.executemany(
            "INSERT INTO table_entries(table_id, level, sha256, md5, title, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            accepted,
        )


class ContainerAdapter:
    """Run one validated input manifest in an isolated temporary workspace."""

    def __init__(
        self,
        *,
        object_store: ObjectStore | None = None,
        artifact_store: ArtifactStore | None = None,
        decryptor: StreamDecryptor | None = None,
        limits: JobLimits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.object_store = object_store
        self.artifact_store = artifact_store or MemoryArtifactStore()
        self.decryptor = decryptor
        self.limits = limits or JobLimits()
        self.clock = clock

    def run_backfill(self, manifest: Mapping[str, Any], **kwargs: Any) -> JobResult:
        return self.run(manifest, _entrypoint="five_db_backfill", **kwargs)

    def run_monthly_audit(self, manifest: Mapping[str, Any], **kwargs: Any) -> JobResult:
        return self.run(manifest, _entrypoint="monthly_audit", **kwargs)

    def run_single_play_incremental(self, manifest: Mapping[str, Any], **kwargs: Any) -> JobResult:
        return self.run(manifest, _entrypoint="single_play_incremental", **kwargs)

    def run(
        self,
        manifest: Mapping[str, Any],
        *,
        bundle: Any | None = None,
        upload_manifest: Mapping[str, Any] | None = None,
        assistant_db: str | Path | None = None,
        profile_display_name: str = "Personal",
        timezone_name: str = "Asia/Tokyo",
        menu_date: str | date | None = None,
        readiness: str = "normal",
        recommendation_input: RecommendationInput | None = None,
        recommendation_repository: Any | None = None,
        table_context: Mapping[str, Any] | None = None,
        revision: int = 1,
        generated_at: str | None = None,
        cancellation: CancellationToken | None = None,
        _entrypoint: str | None = None,
    ) -> JobResult:
        started = time.monotonic()
        token = cancellation or CancellationToken()
        try:
            input_manifest = validate_input_manifest(manifest)
        except ManifestError as exc:
            raise ContainerError(str(exc), code=exc.code, status=422) from exc
        entrypoint = _entrypoint or "five_db_backfill"
        if upload_manifest is not None:
            try:
                accepted_upload = validate_upload_manifest(upload_manifest)
            except ManifestError as exc:
                raise ContainerError(str(exc), code=exc.code, status=422) from exc
            if (
                accepted_upload["profile_id"] != input_manifest.profile_id
                or accepted_upload["account_id"] != input_manifest.account_id
                or accepted_upload["trust_domain"] != input_manifest.trust_domain
                or manifest_sha256(accepted_upload) != input_manifest["source_manifest_sha256"]
            ):
                raise ContainerError("input is not bound to the accepted upload", code="invalid_contract", status=422)
        rendered_menu_date = (
            None
            if menu_date is None
            else menu_date.isoformat() if isinstance(menu_date, date) else str(menu_date)
        )
        if table_context is not None and table_context.get("profile_id") != input_manifest.profile_id:
            raise ContainerError("table context crosses profile partition", code="invalid_contract", status=422)
        settings = _table_settings(table_context)
        profile = ProfileContext(
            profile_id=input_manifest.profile_id,
            display_name=str(table_context.get("display_name", profile_display_name)) if table_context else profile_display_name,
            timezone=settings.timezone if settings else timezone_name,
            settings=settings,
        )
        if recommendation_input is not None and recommendation_input.profile.profile_id != profile.profile_id:
            raise ContainerError("recommendation input crosses profile partition", code="invalid_contract", status=422)
        if recommendation_input is not None and recommendation_repository is not None:
            raise ContainerError("choose one recommendation input source", code="invalid_contract", status=422)
        if revision < 1:
            raise ContainerError("revision must be positive", code="invalid_contract", status=422)

        with tempfile.TemporaryDirectory(prefix="oraja-training-container-") as temporary_root:
            work = Path(temporary_root)
            source_dir = self._materialize_bundle(
                input_manifest,
                bundle=bundle,
                work=work,
                upload_manifest=upload_manifest,
                token=token,
            )
            self._check_deadline(started, token)
            _preflight_sources(source_dir, token=token)
            source_signals = _source_signatures(source_dir)
            self._check_deadline(started, token)

            assistant_path = (
                Path(assistant_db).expanduser().resolve()
                if assistant_db is not None
                else work / "assistant.db"
            )
            if assistant_path in _source_paths(source_dir):
                raise ContainerError("assistant database crosses source partition", code="invalid_contract", status=422)
            fit_result, recommendation, rendered_tables = self._run_core(
                input_manifest,
                source_dir=source_dir,
                assistant_path=assistant_path,
                profile=profile,
                menu_date=rendered_menu_date,
                readiness=readiness,
                recommendation_input=recommendation_input,
                recommendation_repository=recommendation_repository,
                table_context=table_context,
                entrypoint=entrypoint,
                token=token,
                started=started,
            )
            self._check_deadline(started, token)
            if _source_signatures(source_dir) != source_signals:
                raise InputIntegrityError()
            counters = self._read_counters(assistant_path)
            records = self._artifact_payloads(
                assistant_path, fit_result, counters, rendered_tables
            )
            artifact_records: list[ArtifactRecord] = []
            for kind, relative_name, payload in records:
                token.check()
                key = (
                    f"profiles/{input_manifest.profile_id}/jobs/{input_manifest.job_id}/"
                    f"revisions/{revision}/{relative_name}"
                )
                content = _json_artifact(payload, limits=self.limits)
                stored = self.artifact_store.put(key, content, content_type="application/json")
                artifact_records.append(
                    ArtifactRecord(
                        kind=kind,
                        object_key=stored.object_key,
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        content_type=stored.content_type,
                    )
                )
            rendered_generated_at = generated_at or _iso_timestamp(self.clock())
            output = build_output_manifest(
                input_manifest,
                input_manifest_sha256=manifest_sha256(input_manifest.as_dict()),
                output_revision=revision,
                generated_at=rendered_generated_at,
                counters={
                    "accepted_events": counters["accepted_events"],
                    "rejected_events": counters["rejected_events"],
                },
                artifacts=[artifact.as_manifest() for artifact in artifact_records],
            )
            validate_output_manifest(output, input_manifest=input_manifest)
            return JobResult(
                output_manifest=output,
                output_manifest_sha256=manifest_sha256(output),
                artifacts=tuple(artifact_records),
                counters=counters,
                fit_result=fit_result,
                recommendation=recommendation,
                source_manifest_sha256=input_manifest["source_manifest_sha256"],
            )

    def _check_deadline(self, started: float, token: CancellationToken) -> None:
        token.check()
        if time.monotonic() - started > self.limits.max_runtime_seconds:
            raise ResourceLimitExceeded("time")

    def _source_from_store(self, input_manifest: InputManifest) -> Any:
        if self.object_store is None:
            raise ContainerError(
                "input object is unavailable",
                code="temporary_unavailable",
                retryable=True,
                status=503,
            )
        try:
            return self.object_store.get(str(input_manifest.input_bundle["object_key"]))
        except Exception as exc:
            raise ContainerError(
                "input object is unavailable",
                code="temporary_unavailable",
                retryable=True,
                status=503,
            ) from exc

    def _materialize_bundle(
        self,
        input_manifest: InputManifest,
        *,
        bundle: Any | None,
        work: Path,
        upload_manifest: Mapping[str, Any] | None,
        token: CancellationToken,
    ) -> Path:
        if isinstance(bundle, (str, Path)) and Path(bundle).expanduser().is_dir():
            source_dir = Path(bundle).expanduser().resolve()
            _verify_bundle_directory(
                source_dir,
                limits=self.limits,
                upload_manifest=upload_manifest,
            )
            return source_dir
        source = bundle if bundle is not None else self._source_from_store(input_manifest)
        encrypted_path = work / "input.enc"
        expected_size = int(input_manifest.input_bundle["size_bytes"])
        expected_digest = str(input_manifest.input_bundle["sha256"])
        with encrypted_path.open("wb") as destination:
            digest, size = _copy_limited(
                source,
                destination,
                limit=min(expected_size, self.limits.max_input_bytes),
                token=token,
            )
        if size != expected_size or digest != expected_digest:
            raise InputIntegrityError()
        if self.decryptor is None:
            raise ContainerError(
                "envelope decryptor is not configured",
                code="temporary_unavailable",
                retryable=True,
                status=503,
            )
        decrypted_path = work / "input.bundle"
        try:
            with encrypted_path.open("rb") as encrypted, decrypted_path.open("wb") as decrypted:
                plaintext = self.decryptor.decrypt(
                    encrypted,
                    profile_id=input_manifest.profile_id,
                    key_ref=str(input_manifest.input_bundle["key_ref"]),
                )
                _copy_limited(
                    plaintext,
                    decrypted,
                    limit=self.limits.max_temp_bytes,
                    token=token,
                )
        except ContainerError:
            raise
        except Exception as exc:
            raise ContainerError(
                "input decryption failed",
                code="invalid_input",
                retryable=False,
                status=422,
            ) from exc
        source_dir = work / "source"
        source_dir.mkdir()
        _extract_archive(decrypted_path, source_dir, limits=self.limits, token=token)
        _verify_bundle_directory(
            source_dir,
            limits=self.limits,
            upload_manifest=upload_manifest,
        )
        return source_dir

    def _run_core(
        self,
        input_manifest: InputManifest,
        *,
        source_dir: Path,
        assistant_path: Path,
        profile: ProfileContext,
        menu_date: str | None,
        readiness: str,
        recommendation_input: RecommendationInput | None,
        recommendation_repository: Any | None,
        table_context: Mapping[str, Any] | None,
        entrypoint: str,
        token: CancellationToken,
        started: float,
    ) -> tuple[FitResult, Any, dict[str, Any]]:
        try:
            if entrypoint == "five_db_backfill":
                backfill.run(source_dir, assistant_path, clock=self.clock)
                token.check()
                snapshot.run(source_dir / "score.db", source_dir / "scoredatalog.db", assistant_path, clock=self.clock)
            elif entrypoint in {"monthly_audit", "single_play_incremental"}:
                snapshot.run(source_dir / "score.db", source_dir / "scoredatalog.db", assistant_path, clock=self.clock)
            else:
                raise ContainerError("unsupported container job type", code="unsupported_job_type", status=422)
            self._check_deadline(started, token)

            with closing(readers.open_snapshot(source_dir / "songinfo.db")) as songinfo, closing(store.init(assistant_path)) as assistant:
                feature_repository = SQLiteFeatureRepository(songinfo, assistant)
                build_from_repository(feature_repository)
                token.check()
                model_repository = SQLiteModelRepository(assistant)
                fit_result = fit_from_port(
                    model_repository,
                    unit_of_work=SQLiteUnitOfWork(assistant),
                    trained_at=int(self.clock()),
                )
                if table_context is not None:
                    _seed_table_catalog(assistant, table_context, int(self.clock()))
                    recommendation_repository = SQLiteRecommendationRepository(assistant)
                recommendation: Any | None = None
                session: Any | None = None
                if recommendation_input is not None:
                    session = build_session_from_input(
                        recommendation_input,
                        menu_date=menu_date,
                        readiness=readiness,
                        clock=self.clock,
                    )
                    recommendation = recommendation_output(session)
                elif recommendation_repository is not None:
                    session = build_session_from_repository(
                        recommendation_repository,
                        profile=profile,
                        menu_date=menu_date,
                        readiness=readiness,
                        clock=self.clock,
                    )
                    recommendation = recommendation_output(session)
                    recommendation_repository.save_output(recommendation)
            if recommendation is None or session is None:
                raise ContainerError(
                    "table catalog and profile settings are required",
                    code="table_input_unavailable",
                    retryable=False,
                    status=422,
                )
            return fit_result, recommendation, table_payloads(session)
        except (ContainerError, ManifestError):
            raise
        except MenuBuildError as exc:
            raise ContainerError(
                "table input cannot produce a bounded recommendation",
                code="table_input_unavailable",
                retryable=False,
                status=422,
            ) from exc
        except (sqlite3.DatabaseError, readers.ReaderSchemaError, OSError) as exc:
            raise InputIntegrityError() from exc

    def _read_counters(self, assistant_path: Path) -> dict[str, int]:
        conn = store.init(assistant_path)
        try:
            accepted, courses = conn.execute(
                "SELECT count(*), COALESCE(sum(is_course), 0) FROM plays"
            ).fetchone()
            return {
                "accepted_events": int(accepted),
                "rejected_events": 0,
                "course_events": int(courses),
            }
        finally:
            conn.close()

    def _artifact_payloads(
        self,
        assistant_path: Path,
        fit_result: FitResult,
        counters: Mapping[str, int],
        rendered_tables: Mapping[str, Any],
    ) -> list[tuple[str, str, Any]]:
        conn = store.init(assistant_path)
        try:
            play_columns = (
                "sha256", "mode", "played_at", "playcount", "source_generation",
                "source", "clear", "ex", "minbp", "notes", "judged", "empty_poor",
                "survival", "completed", "bp_rate", "credited_gauge_kind",
                "selected_gauge_kind", "is_course", "lost_events", "payload_hash",
            )
            plays = [
                dict(zip(play_columns, row))
                for row in conn.execute(
                    "SELECT " + ", ".join(play_columns) + " FROM plays "
                    "ORDER BY source_generation, sha256, mode, playcount"
                )
            ]
            feature_columns = (
                "sha256", "feature_version", "density_mean", "density_p90", "density_p99",
                "end_density", "burst_max", "scratch_rate", "scratch_p90",
                "scratch_combo_rate", "ln_rate", "soflan_var", "soflan_changes",
                "stop_count", "chart_seconds", "total_notes",
            )
            features = [
                dict(zip(feature_columns, row))
                for row in conn.execute(
                    "SELECT " + ", ".join(feature_columns) + " FROM chart_features ORDER BY sha256"
                )
            ]
            model = fit_result.model
            model_payload = {
                "status": fit_result.status,
                "n_results": fit_result.n_results,
                "n_sessions": fit_result.n_sessions,
                "n_train": fit_result.n_train,
                "n_holdout": fit_result.n_holdout,
                "gate_fraction": fit_result.gate_fraction,
                "version": fit_result.version,
                "metrics": fit_result.metrics,
                "model": _model_dict(model),
                "raw_db_exported": False,
            }
            records: list[tuple[str, str, Any]] = [
                ("normalized_events", "normalized_events.json", {
                    "contract": "container-artifact",
                    "schema_version": "1",
                    "kind": "normalized_events",
                    "counters": dict(counters),
                    "events": plays,
                }),
                ("feature_input", "feature_input.json", {
                    "contract": "container-artifact",
                    "schema_version": "1",
                    "kind": "feature_input",
                    "features": features,
                }),
                ("model_input", "model_input.json", {
                    "contract": "container-artifact",
                    "schema_version": "1",
                    "kind": "model_input",
                    "model": model_payload,
                }),
            ]
            table_kinds = {
                "table/recommend/header.json": "recommend_header",
                "table/recommend/score.json": "recommend_score",
                "table/today/header.json": "daily_menu_header",
                "table/today/score.json": "daily_menu_score",
            }
            for relative_name, kind in table_kinds.items():
                if relative_name not in rendered_tables:
                    raise ContainerError("table renderer omitted an artifact", code="invalid_contract", status=422)
                records.append((kind, relative_name, rendered_tables[relative_name]))
            return records
        finally:
            conn.close()
