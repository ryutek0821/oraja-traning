-- Bind OAuth grants and credentials to the MCP resource and support rotating
-- refresh tokens. Existing access-only credentials predate resource binding;
-- revoke them instead of silently treating them as valid for every audience.

CREATE TABLE oauth_grants (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  resource TEXT NOT NULL,
  scope TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER,
  revoke_reason TEXT,
  reuse_detected_at INTEGER,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX oauth_grants_by_owner
  ON oauth_grants(account_id, profile_id, client_id, revoked_at);

ALTER TABLE oauth_authorization_codes ADD COLUMN grant_id TEXT REFERENCES oauth_grants(id) ON DELETE CASCADE;
ALTER TABLE oauth_authorization_codes ADD COLUMN resource TEXT;

ALTER TABLE oauth_tokens ADD COLUMN grant_id TEXT REFERENCES oauth_grants(id) ON DELETE CASCADE;
ALTER TABLE oauth_tokens ADD COLUMN resource TEXT;
ALTER TABLE oauth_tokens ADD COLUMN token_kind TEXT NOT NULL DEFAULT 'access';
ALTER TABLE oauth_tokens ADD COLUMN family_id TEXT;
ALTER TABLE oauth_tokens ADD COLUMN rotated_to_hash TEXT;

CREATE INDEX oauth_tokens_by_grant
  ON oauth_tokens(grant_id, token_kind, revoked_at);

UPDATE oauth_tokens
   SET revoked_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE revoked_at IS NULL;
