-- Issue #9: profile-scoped five-DB upload sessions.
-- Only encrypted envelope metadata and digests are persisted here. The raw
-- profile identifier, local path, plaintext DB, DEK, and auth tokens are not.

PRAGMA foreign_keys = ON;

CREATE TABLE upload_profile_envelopes (
  profile_scope TEXT PRIMARY KEY,
  key_version TEXT NOT NULL,
  wrap_nonce TEXT NOT NULL,
  wrapped_dek TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE upload_sessions (
  upload_id TEXT PRIMARY KEY,
  profile_scope TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  month TEXT NOT NULL,
  submission_kind TEXT NOT NULL
    CHECK (submission_kind IN ('initial', 'backfill', 'monthly', 'audit')),
  source_generation TEXT NOT NULL,
  envelope_key_version TEXT NOT NULL,
  envelope_wrap_nonce TEXT NOT NULL,
  envelope_wrapped_dek TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active'
    CHECK (state IN ('active', 'completed', 'aborted', 'expired')),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  completed_at INTEGER
);

CREATE INDEX upload_sessions_by_owner_state
  ON upload_sessions(profile_scope, state, created_at);

CREATE INDEX upload_sessions_by_manifest
  ON upload_sessions(profile_scope, manifest_sha256, created_at);

CREATE UNIQUE INDEX upload_live_manifest_once
  ON upload_sessions(profile_scope, manifest_sha256)
  WHERE state IN ('active', 'completed');

CREATE TABLE upload_files (
  upload_id TEXT NOT NULL REFERENCES upload_sessions(upload_id) ON DELETE CASCADE,
  file_name TEXT NOT NULL CHECK (
    file_name IN ('score.db', 'scoredatalog.db', 'scorelog.db', 'songdata.db', 'songinfo.db')
  ),
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 5368709120),
  state TEXT NOT NULL CHECK (state IN ('upload_required', 'deduplicated', 'completed')),
  object_key TEXT NOT NULL,
  created_object_key TEXT,
  r2_upload_id TEXT,
  encryption_json TEXT,
  dedup_json TEXT,
  PRIMARY KEY(upload_id, file_name)
);

CREATE TABLE upload_parts (
  upload_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  part_number INTEGER NOT NULL CHECK (part_number BETWEEN 1 AND 10000),
  etag TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 5368709120),
  PRIMARY KEY(upload_id, file_name, part_number),
  FOREIGN KEY(upload_id, file_name)
    REFERENCES upload_files(upload_id, file_name) ON DELETE CASCADE
);

CREATE TABLE upload_dedup (
  profile_scope TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 5368709120),
  object_key TEXT NOT NULL,
  key_version TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(profile_scope, sha256, size_bytes)
);

CREATE INDEX upload_dedup_by_object ON upload_dedup(profile_scope, object_key);
