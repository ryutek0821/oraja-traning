-- Authentication hardening for issue #7.
-- Password parameters are versioned outside the encoded Argon2 string so a
-- future parameter upgrade can be rolled out with a successful-login rehash.

ALTER TABLE credentials
  ADD COLUMN password_params_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE credentials
  ADD COLUMN password_rehash_required INTEGER NOT NULL DEFAULT 0
    CHECK (password_rehash_required IN (0, 1));

ALTER TABLE sessions
  ADD COLUMN last_seen_at INTEGER;

ALTER TABLE sessions
  ADD COLUMN rotated_at INTEGER;

ALTER TABLE sessions
  ADD COLUMN csrf_hash TEXT NOT NULL DEFAULT '';

-- Recovery codes are never stored in plaintext. Existing rows from the
-- foundation migration receive an empty salt and cannot match a new code.
ALTER TABLE recovery_codes
  ADD COLUMN salt TEXT NOT NULL DEFAULT '';

CREATE INDEX sessions_by_hash_validity
  ON sessions(session_hash, expires_at, revoked_at);

CREATE INDEX recovery_codes_by_account_unused
  ON recovery_codes(account_id, used_at, created_at);

CREATE INDEX email_tokens_by_purpose_validity
  ON email_tokens(purpose, token_hash, expires_at, used_at);

-- Keys are SHA-256 digests of an IP/account identifier. Raw network
-- identifiers and usernames are intentionally not persisted here.
CREATE TABLE auth_rate_limits (
  scope TEXT NOT NULL CHECK (scope IN ('ip', 'account', 'recovery', 'email')),
  key_hash TEXT NOT NULL,
  failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
  first_failed_at INTEGER NOT NULL,
  last_failed_at INTEGER NOT NULL,
  blocked_until INTEGER NOT NULL DEFAULT 0 CHECK (blocked_until >= 0),
  PRIMARY KEY (scope, key_hash)
);

CREATE INDEX auth_rate_limits_by_block
  ON auth_rate_limits(scope, blocked_until);
