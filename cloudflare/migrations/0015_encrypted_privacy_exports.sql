-- Issue #16: authenticated encrypted portable exports. The plaintext data key
-- is never persisted; only an AES-GCM KEK-wrapped copy is retained until the
-- owner claims the one-time download.

PRAGMA foreign_keys = OFF;

CREATE TABLE privacy_export_jobs_v2 (
  export_id TEXT PRIMARY KEY REFERENCES exports(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'snapshotting', 'ready', 'failed', 'expired', 'deleted')),
  schema_version TEXT NOT NULL CHECK (schema_version IN ('oraja.profile-export.v1', 'oraja.profile-export.v2')),
  requested_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER,
  failure_code TEXT,
  archive_sha256 TEXT,
  encryption_algorithm TEXT CHECK (encryption_algorithm IS NULL OR encryption_algorithm = 'AES-256-GCM'),
  content_iv_b64 TEXT,
  wrapped_key_b64 TEXT,
  wrap_iv_b64 TEXT,
  plaintext_sha256 TEXT,
  archive_bytes INTEGER CHECK (archive_bytes IS NULL OR archive_bytes BETWEEN 17 AND 16777232),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

INSERT INTO privacy_export_jobs_v2(
  export_id, account_id, profile_id, state, schema_version, requested_at,
  started_at, completed_at, failure_code, archive_sha256
)
SELECT export_id, account_id, profile_id, state, schema_version, requested_at,
       started_at, completed_at, failure_code, archive_sha256
  FROM privacy_export_jobs;

DROP TABLE privacy_export_jobs;
ALTER TABLE privacy_export_jobs_v2 RENAME TO privacy_export_jobs;

CREATE INDEX privacy_exports_by_owner_state
  ON privacy_export_jobs(account_id, profile_id, state, requested_at);

PRAGMA foreign_keys = ON;

CREATE TRIGGER privacy_export_ready_requires_encryption
BEFORE UPDATE OF state ON privacy_export_jobs
WHEN NEW.state = 'ready' AND (
  NEW.encryption_algorithm <> 'AES-256-GCM'
  OR length(NEW.content_iv_b64) <> 16
  OR length(NEW.wrap_iv_b64) <> 16
  OR length(NEW.wrapped_key_b64) <> 64
  OR length(NEW.plaintext_sha256) <> 64
  OR NEW.archive_bytes IS NULL
)
BEGIN
  SELECT RAISE(ABORT, 'privacy_export_encryption_required');
END;
