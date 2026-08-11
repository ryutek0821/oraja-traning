-- OAuth 2.1 authorization-code state and approved advisor proposals.
-- Values that can authenticate or identify an account are stored as hashes or
-- internal IDs; bearer tokens and authorization codes are never persisted.

CREATE TABLE oauth_clients (
  client_id TEXT PRIMARY KEY,
  client_name TEXT NOT NULL,
  redirect_uris_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  revoked_at INTEGER
);

CREATE TABLE oauth_authorization_codes (
  code_hash TEXT PRIMARY KEY,
  client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scope TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  code_challenge_method TEXT NOT NULL CHECK (code_challenge_method = 'S256'),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX oauth_codes_by_client ON oauth_authorization_codes(client_id, expires_at, used_at);

CREATE TABLE oauth_tokens (
  token_hash TEXT PRIMARY KEY,
  client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX oauth_tokens_by_owner ON oauth_tokens(account_id, profile_id, expires_at, revoked_at);

CREATE TABLE advisor_proposals (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  title TEXT NOT NULL,
  proposal_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at INTEGER NOT NULL,
  decided_at INTEGER,
  decision_reason TEXT,
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX advisor_proposals_by_profile
  ON advisor_proposals(account_id, profile_id, status, created_at);
