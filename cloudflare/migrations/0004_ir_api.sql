-- Issue #12: profile-scoped IR device credentials and request buckets.
-- The bearer secret is never stored. token_suffix is display-only metadata.

ALTER TABLE device_token_hashes
  ADD COLUMN token_suffix TEXT NOT NULL DEFAULT '';

CREATE INDEX device_tokens_by_device_validity
  ON device_token_hashes(device_id, revoked_at, last_used_at);

CREATE TABLE ir_rate_limits (
  scope TEXT NOT NULL CHECK (scope IN ('ip', 'token')),
  key_hash TEXT NOT NULL,
  window_started_at INTEGER NOT NULL,
  request_count INTEGER NOT NULL CHECK (request_count >= 0),
  PRIMARY KEY (scope, key_hash)
);

CREATE INDEX ir_rate_limits_by_window
  ON ir_rate_limits(scope, window_started_at);
