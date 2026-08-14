import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const oauth = await readFile(new URL("worker/src/oauth.ts", root), "utf8");
const index = await readFile(new URL("worker/src/index.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0009_oauth_token_lifecycle.sql", root), "utf8");

test("OAuth metadata advertises refresh, revocation, PKCE, and protected-resource discovery", () => {
  assert.match(oauth, /grant_types_supported: \["authorization_code", "refresh_token"\]/);
  assert.match(oauth, /revocation_endpoint:/);
  assert.match(oauth, /code_challenge_methods_supported: \["S256"\]/);
  assert.match(oauth, /oauthProtectedResourceMetadata/);
  assert.match(index, /\.well-known\/oauth-protected-resource/);
});

test("authorization codes and tokens are exactly bound to the MCP resource", () => {
  assert.match(oauth, /parsed\.toString\(\) !== mcpOAuthResource\(origin\)/);
  assert.match(oauth, /row\.resource !== resource/);
  assert.match(oauth, /row\.token_kind !== "access"/);
  assert.match(index, /codeChallengeMethod: url\.searchParams\.get\("code_challenge_method"\)/);
  assert.match(index, /destination\.searchParams\.set\("iss"/);
});

test("refresh rotation detects reuse and revokes the whole grant", () => {
  assert.match(migration, /CREATE TABLE oauth_grants/);
  assert.match(migration, /reuse_detected_at INTEGER/);
  assert.match(migration, /token_kind TEXT NOT NULL DEFAULT 'access'/);
  assert.match(oauth, /rotated_to_hash !== null/);
  assert.match(oauth, /"refresh_token_reuse", now, true/);
  assert.match(oauth, /UPDATE oauth_tokens SET revoked_at = COALESCE\(revoked_at, \?1\) WHERE grant_id = \?2/);
  assert.match(oauth, /now \+ 30 \* 86400/);
});

test("revocation is non-enumerating and operates on a grant", () => {
  assert.match(oauth, /RFC 7009 revocation deliberately does not reveal/);
  assert.match(oauth, /revokeGrant\(db, row\.grant_id, "client_revocation", now\)/);
  assert.match(index, /url\.pathname === "\/oauth\/revoke"/);
});
