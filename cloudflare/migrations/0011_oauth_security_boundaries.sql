-- OAuth endpoint abuse controls and bounded Client ID Metadata Document cache.

CREATE TABLE oauth_rate_limits (
  endpoint TEXT NOT NULL CHECK (endpoint IN ('authorize', 'token', 'revoke')),
  key_hash TEXT NOT NULL,
  request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
  window_started_at INTEGER NOT NULL,
  blocked_until INTEGER NOT NULL DEFAULT 0 CHECK (blocked_until >= 0),
  PRIMARY KEY(endpoint, key_hash)
);

CREATE INDEX oauth_rate_limits_by_block
  ON oauth_rate_limits(endpoint, blocked_until);

CREATE TABLE oauth_client_metadata_cache (
  client_id TEXT PRIMARY KEY,
  metadata_json TEXT NOT NULL,
  etag TEXT,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX oauth_client_metadata_cache_by_expiry
  ON oauth_client_metadata_cache(expires_at);
