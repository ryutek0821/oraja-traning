"""Cloud/container boundary for the database-independent Python core.

The modules in this package are deliberately adapter code.  They may open a
SQLite snapshot and write an assistant-owned database, but the feature,
model, and recommendation algorithms remain behind the public ports in
``oraja_training.features``, ``oraja_training.model``, and
``oraja_training.plan``.
"""

from .adapter import (
    ArtifactRecord,
    ArtifactStore,
    CancellationToken,
    ContainerAdapter,
    ContainerError,
    FileArtifactStore,
    JobLimits,
    JobResult,
    MemoryArtifactStore,
    PassthroughDecryptor,
)
from .manifest import (
    InputManifest,
    ManifestError,
    build_output_manifest,
    canonical_json,
    manifest_sha256,
    parse_json,
    validate_input_manifest,
    validate_output_manifest,
    validate_upload_manifest,
)

__all__ = [
    "ArtifactRecord",
    "ArtifactStore",
    "CancellationToken",
    "ContainerAdapter",
    "ContainerError",
    "FileArtifactStore",
    "InputManifest",
    "JobLimits",
    "JobResult",
    "ManifestError",
    "MemoryArtifactStore",
    "PassthroughDecryptor",
    "build_output_manifest",
    "canonical_json",
    "manifest_sha256",
    "parse_json",
    "validate_input_manifest",
    "validate_output_manifest",
    "validate_upload_manifest",
]
