-- Issue #16: fail-closed export and deletion lifecycle.
-- Destructive DO/R2 work is represented by an owner-scoped durable outbox;
-- a deletion can only complete after every required target is verified.

PRAGMA foreign_keys = ON;

ALTER TABLE deletion_requests ADD COLUMN started_at INTEGER;
ALTER TABLE deletion_requests ADD COLUMN completed_at INTEGER;
ALTER TABLE deletion_requests ADD COLUMN failure_code TEXT;

ALTER TABLE exports ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'oraja.profile-export.v1';
ALTER TABLE exports ADD COLUMN ready_at INTEGER;
ALTER TABLE exports ADD COLUMN downloaded_at INTEGER;

CREATE TRIGGER deletion_requests_require_active_owner
BEFORE INSERT ON deletion_requests
WHEN NEW.profile_id IS NULL OR NOT EXISTS (
  SELECT 1 FROM profiles p JOIN accounts a ON a.id = p.account_id
   WHERE p.id = NEW.profile_id AND p.account_id = NEW.account_id
     AND p.status = 'active' AND a.status = 'active'
)
BEGIN
  SELECT RAISE(ABORT, 'deletion_owner_not_active');
END;

CREATE TABLE privacy_export_jobs (
  export_id TEXT PRIMARY KEY REFERENCES exports(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'snapshotting', 'ready', 'failed', 'expired', 'deleted')),
  schema_version TEXT NOT NULL CHECK (schema_version = 'oraja.profile-export.v1'),
  requested_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER,
  failure_code TEXT,
  archive_sha256 TEXT,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX privacy_exports_by_owner_state
  ON privacy_export_jobs(account_id, profile_id, state, requested_at);

CREATE TABLE privacy_tasks (
  id TEXT PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  deletion_id TEXT REFERENCES deletion_requests(id) ON DELETE CASCADE,
  export_id TEXT REFERENCES exports(id) ON DELETE CASCADE,
  task_kind TEXT NOT NULL CHECK (task_kind IN ('export_snapshot', 'do_purge', 'r2_delete')),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('profile_do', 'raw_r2', 'artifact_r2', 'export_r2')),
  target_key TEXT NOT NULL,
  target_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_until INTEGER,
  next_attempt_at INTEGER,
  last_error_code TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  completed_at INTEGER,
  CHECK ((deletion_id IS NOT NULL) <> (export_id IS NOT NULL)),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX privacy_tasks_ready
  ON privacy_tasks(status, next_attempt_at, lease_until, created_at);

CREATE INDEX privacy_tasks_by_deletion
  ON privacy_tasks(deletion_id, status, target_kind);

CREATE INDEX privacy_tasks_by_export
  ON privacy_tasks(export_id, status);

CREATE TRIGGER privacy_tasks_owner_insert
BEFORE INSERT ON privacy_tasks
WHEN NOT EXISTS (
  SELECT 1 FROM profiles
   WHERE id = NEW.profile_id AND account_id = NEW.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'privacy_task_owner_mismatch');
END;

CREATE TABLE privacy_completion_audits (
  id TEXT PRIMARY KEY,
  deletion_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  task_count INTEGER NOT NULL CHECK (task_count > 0),
  verification_digest TEXT NOT NULL,
  completed_at INTEGER NOT NULL
);

CREATE TRIGGER privacy_completion_audits_append_only_update
BEFORE UPDATE ON privacy_completion_audits
BEGIN
  SELECT RAISE(ABORT, 'privacy_completion_audits_are_append_only');
END;

CREATE TRIGGER privacy_completion_audits_append_only_delete
BEFORE DELETE ON privacy_completion_audits
BEGIN
  SELECT RAISE(ABORT, 'privacy_completion_audits_are_append_only');
END;
