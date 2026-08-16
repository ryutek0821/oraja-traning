import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const oauth = await readFile(new URL("worker/src/oauth.ts", root), "utf8");
const index = await readFile(new URL("worker/src/index.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0009_oauth_token_lifecycle.sql", root), "utf8");
const securityMigration = await readFile(new URL("migrations/0011_oauth_security_boundaries.sql", root), "utf8");
const auth = await readFile(new URL("worker/src/auth.ts", root), "utf8");

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
  assert.match(index, /codeChallengeMethod: parameter\("code_challenge_method"\)/);
  assert.match(index, /destination\.searchParams\.set\("iss"/);
});

test("redirect URIs allow HTTPS or HTTP loopback only", () => {
  assert.match(oauth, /const secure = url\.protocol === "https:"/);
  assert.match(oauth, /url\.protocol === "http:" && isLoopbackHostname\(url\.hostname\)/);
  assert.match(oauth, /normalized === "localhost" \|\| normalized === "\[::1\]"/);
  assert.match(oauth, /octets\[0\] === "127"/);
});

test("authorization code consumption and token insertion share one guarded batch", () => {
  assert.match(oauth, /const results = await db\.batch\(\[/);
  assert.match(oauth, /UPDATE oauth_authorization_codes SET used_at = \?1 WHERE code_hash = \?2 AND used_at IS NULL/);
  assert.match(oauth, /WHERE EXISTS \(\s*SELECT 1 FROM oauth_authorization_codes WHERE code_hash = \?11 AND used_at = \?12/);
  assert.match(oauth, /results\.some\(\(result\) => \(result\.meta\?\.changes \?\? 0\) !== 1\)/);
  assert.doesNotMatch(oauth, /const consumed = await db/);
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

test("account recovery and every password replacement revoke OAuth grants", () => {
  assert.match(auth, /revokeOAuthGrantsForAccount\(db, account\.id, "account_recovery", now\)/);
  assert.match(auth, /revokeOAuthGrantsForAccount\(db, token\.account_id, "password_reset", now\)/);
  assert.match(auth, /revokeOAuthGrantsForAccount\(db, session\.account_id, "password_change", now\)/);
  assert.match(index, /url\.pathname === "\/v1\/auth\/password"/);
});

test("OAuth endpoints are rate limited and write append-only audit events", () => {
  assert.match(securityMigration, /CREATE TABLE oauth_rate_limits/);
  assert.match(oauth, /oauth-rate:\$\{endpoint\}:\$\{key\}/);
  assert.match(index, /enforceOAuthRateLimit\(env\.CONTROL_DB, "authorize"/);
  assert.match(index, /enforceOAuthRateLimit\(env\.CONTROL_DB, "token"/);
  assert.match(index, /enforceOAuthRateLimit\(env\.CONTROL_DB, "revoke"/);
  assert.match(oauth, /INSERT INTO audit_events/);
  assert.match(oauth, /eventType: "oauth\.refresh_reuse"/);
});

test("CIMD is bounded, cached, and rejects SSRF-shaped client identifiers", () => {
  assert.match(securityMigration, /CREATE TABLE oauth_client_metadata_cache/);
  assert.match(oauth, /url\.protocol !== "https:"/);
  assert.match(oauth, /blockedName \|\| ipLiteral/);
  assert.match(oauth, /redirect: "manual"/);
  assert.match(oauth, /AbortSignal\.timeout\(5000\)/);
  assert.match(oauth, /limitedJson\(response\)/);
  assert.match(oauth, /response\.url !== clientId/);
});

test("authorization requires explicit bilingual consent with CSRF protection", () => {
  assert.match(index, /接続の確認 \/ 연결 확인/);
  assert.match(index, /許可 \/ 허용/);
  assert.match(index, /拒否 \/ 거부/);
  assert.match(index, /verifyCsrf\(env\.CONTROL_DB, sessionToken, parameter\("csrf_token"\)/);
  assert.match(index, /parameter\("decision"\) !== "approve"/);
  assert.match(index, /frame-ancestors 'none'/);
});
