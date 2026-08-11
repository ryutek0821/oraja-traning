-- Immutable Player Recommend/Daily Menu publication and profile capabilities.
-- Raw capability secrets are never stored; only SHA-256 digests are persisted.

ALTER TABLE capability_hashes
  ADD COLUMN last_used_at INTEGER;

CREATE TABLE artifact_revisions (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  table_kind TEXT NOT NULL CHECK (table_kind IN ('recommend', 'today')),
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  object_key TEXT NOT NULL,
  previous_revision INTEGER,
  created_at INTEGER NOT NULL,
  UNIQUE(profile_id, table_kind, revision),
  UNIQUE(profile_id, table_kind, content_hash),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX artifact_revisions_by_profile
  ON artifact_revisions(account_id, profile_id, table_kind, revision DESC);

CREATE TABLE artifact_latest (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  table_kind TEXT NOT NULL CHECK (table_kind IN ('recommend', 'today')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  object_key TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(profile_id, table_kind),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX artifact_latest_by_owner
  ON artifact_latest(account_id, profile_id, table_kind, revision);
