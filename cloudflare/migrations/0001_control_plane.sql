-- Shared D1 control plane for issue #6.
-- IDs are internal opaque values. Public IDs are not used as authorization.

PRAGMA foreign_keys = ON;

CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  username_normalized TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'deletion_pending', 'deleted')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE profile_limits (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  max_profiles INTEGER NOT NULL DEFAULT 1 CHECK (max_profiles > 0),
  updated_at INTEGER NOT NULL
);

CREATE TABLE profiles (
  id TEXT PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  display_name TEXT NOT NULL,
  timezone TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended', 'deletion_pending', 'deleted')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER,
  UNIQUE (account_id, id)
);

CREATE INDEX profiles_by_account_status
  ON profiles(account_id, status, created_at);

-- The limit row and this trigger are both inside the profile-create batch.
-- A future paid tier can raise max_profiles without changing profile storage.
CREATE TRIGGER profiles_enforce_account_limit
BEFORE INSERT ON profiles
BEGIN
  SELECT CASE
    WHEN (
      SELECT COUNT(*) FROM profiles
       WHERE account_id = NEW.account_id
         AND status IN ('active', 'suspended', 'deletion_pending')
    ) >= COALESCE((
      SELECT max_profiles FROM profile_limits
       WHERE account_id = NEW.account_id
    ), 0)
    THEN RAISE(ABORT, 'profile_limit_reached')
  END;
END;

CREATE TRIGGER profiles_enforce_account_limit_update
BEFORE UPDATE OF account_id, status ON profiles
WHEN NEW.status IN ('active', 'suspended', 'deletion_pending')
 AND (OLD.status = 'deleted' OR OLD.account_id <> NEW.account_id)
BEGIN
  SELECT CASE
    WHEN (
      SELECT COUNT(*) FROM profiles
       WHERE account_id = NEW.account_id
         AND status IN ('active', 'suspended', 'deletion_pending')
         AND id <> OLD.id
    ) >= COALESCE((
      SELECT max_profiles FROM profile_limits
       WHERE account_id = NEW.account_id
    ), 0)
    THEN RAISE(ABORT, 'profile_limit_reached')
  END;
END;

CREATE TABLE credentials (
  account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  password_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  session_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE INDEX sessions_by_account ON sessions(account_id, expires_at, revoked_at);

CREATE TABLE recovery_codes (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  used_at INTEGER,
  UNIQUE(account_id, code_hash)
);

CREATE TABLE emails (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  email_hash TEXT NOT NULL UNIQUE,
  encrypted_email TEXT,
  verified_at INTEGER,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE email_tokens (
  id TEXT PRIMARY KEY,
  email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  purpose TEXT NOT NULL CHECK (purpose IN ('verify', 'password_reset')),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER
);

CREATE TABLE terms_versions (
  version TEXT PRIMARY KEY,
  document_kind TEXT NOT NULL CHECK (document_kind IN ('terms', 'privacy')),
  content_hash TEXT NOT NULL,
  published_at INTEGER NOT NULL
);

CREATE TABLE consents (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
  scope TEXT NOT NULL CHECK (scope IN ('terms', 'privacy', 'personal_recommendation', 'aggregate_training')),
  terms_version TEXT NOT NULL REFERENCES terms_versions(version),
  granted_at INTEGER NOT NULL,
  revoked_at INTEGER,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX consents_by_owner ON consents(account_id, profile_id, scope, granted_at);

CREATE TRIGGER consents_append_only_update
BEFORE UPDATE ON consents
BEGIN
  SELECT RAISE(ABORT, 'consents_are_append_only');
END;

CREATE TRIGGER consents_append_only_delete
BEFORE DELETE ON consents
BEGIN
  SELECT RAISE(ABORT, 'consents_are_append_only');
END;

CREATE TABLE devices (
  id TEXT PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  label TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER,
  UNIQUE(account_id, profile_id, id),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX devices_by_profile ON devices(account_id, profile_id, revoked_at);

CREATE TABLE device_token_hashes (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at INTEGER
);

CREATE TABLE capability_hashes (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  capability_kind TEXT NOT NULL CHECK (capability_kind IN ('recommend', 'today', 'export')),
  secret_hash TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  revoked_at INTEGER,
  UNIQUE(profile_id, capability_kind, id),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX capabilities_by_profile
  ON capability_hashes(account_id, profile_id, capability_kind, revoked_at);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  job_kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  UNIQUE(profile_id, idempotency_key),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX jobs_by_owner ON jobs(account_id, profile_id, status, created_at);

CREATE TABLE job_attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  error_code TEXT,
  UNIQUE(job_id, attempt)
);

CREATE TABLE audit_events (
  id TEXT PRIMARY KEY,
  account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
  profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('account', 'device', 'service', 'oauth')),
  event_type TEXT NOT NULL,
  reason_code TEXT,
  request_id TEXT,
  resource_hash TEXT,
  input_hash TEXT,
  status TEXT NOT NULL,
  occurred_at INTEGER NOT NULL
);

CREATE INDEX audit_by_owner ON audit_events(account_id, profile_id, occurred_at);

CREATE TRIGGER audit_append_only_update
BEFORE UPDATE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events_are_append_only');
END;

CREATE TRIGGER audit_append_only_delete
BEFORE DELETE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events_are_append_only');
END;

CREATE TABLE deletion_requests (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT,
  requested_at INTEGER NOT NULL,
  cancel_until INTEGER NOT NULL,
  cancelled_at INTEGER,
  confirmed_at INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'cancelled', 'confirmed', 'completed')),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX one_pending_deletion_per_owner
  ON deletion_requests(account_id, COALESCE(profile_id, 'ACCOUNT'))
  WHERE status = 'pending';

CREATE TABLE exports (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT,
  export_kind TEXT NOT NULL CHECK (export_kind IN ('data', 'backup', 'artifact')),
  object_key_hash TEXT NOT NULL,
  object_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  status TEXT NOT NULL CHECK (status IN ('queued', 'ready', 'expired', 'deleted')),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE TABLE chart_catalog_sources (
  id TEXT PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_kind TEXT NOT NULL,
  source_url TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  retired_at INTEGER
);

CREATE TABLE chart_catalog_versions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES chart_catalog_sources(id) ON DELETE RESTRICT,
  version TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('staged', 'active', 'retired')),
  UNIQUE(source_id, version)
);

CREATE TABLE chart_catalog_entries (
  catalog_version_id TEXT NOT NULL REFERENCES chart_catalog_versions(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  md5 TEXT,
  title TEXT NOT NULL,
  artist TEXT NOT NULL,
  notes INTEGER NOT NULL CHECK (notes >= 0),
  song_mode INTEGER NOT NULL,
  PRIMARY KEY(catalog_version_id, sha256)
);

CREATE INDEX chart_catalog_by_hash
  ON chart_catalog_entries(sha256, catalog_version_id);
