import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";

export const OAUTH_SCOPES = [
  "profile:read",
  "training:read",
  "plays:read",
  "recommendations:read",
  "advisor:read",
  "advisor:propose",
] as const;

export type OAuthScope = (typeof OAUTH_SCOPES)[number];

export type OAuthPrincipal = {
  clientId: string;
  accountId: string;
  profileId: string;
  scopes: OAuthScope[];
  resource: string;
  grantId: string;
  expiresAt: number;
};

type ClientRow = {
  client_id: string;
  client_name: string;
  redirect_uris_json: string;
  revoked_at: number | null;
};

type CodeRow = {
  code_hash: string;
  client_id: string;
  account_id: string;
  profile_id: string;
  redirect_uri: string;
  scope: string;
  code_challenge: string;
  code_challenge_method: "S256";
  expires_at: number;
  used_at: number | null;
  grant_id: string;
  resource: string;
};

type TokenRow = {
  client_id: string;
  account_id: string;
  profile_id: string;
  scope: string;
  expires_at: number;
  revoked_at: number | null;
  token_kind: "access" | "refresh";
  grant_id: string;
  resource: string;
  family_id: string | null;
  rotated_to_hash: string | null;
};

type OAuthTokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  expires_in: number;
  scope: string;
  resource: string;
};

function randomSecret(bytes = 32): string {
  const value = crypto.getRandomValues(new Uint8Array(bytes));
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function hexBytes(value: string): Uint8Array {
  const bytes = new Uint8Array(value.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

function base64url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function parseScopes(value: unknown): OAuthScope[] {
  if (typeof value !== "string") throw new ApiError("invalid_scope", 400);
  const scopes = [...new Set(value.split(/\s+/).filter(Boolean))];
  if (scopes.length === 0 || scopes.some((scope) => !(OAUTH_SCOPES as readonly string[]).includes(scope))) {
    throw new ApiError("invalid_scope", 400);
  }
  return scopes as OAuthScope[];
}

function validRedirectUri(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048) throw new ApiError("invalid_redirect_uri", 400);
  try {
    const url = new URL(value);
    if ((url.protocol !== "https:" && url.hostname !== "localhost") || url.hash) throw new Error("scheme");
  } catch {
    throw new ApiError("invalid_redirect_uri", 400);
  }
  return value;
}

function parseRedirectUris(value: string): string[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (!Array.isArray(parsed) || parsed.length === 0 || parsed.some((item) => typeof item !== "string")) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  return parsed.map(validRedirectUri);
}

export function mcpOAuthResource(origin: string): string {
  return new URL("/mcp", new URL(origin).origin).toString();
}

function validResource(origin: string, value: unknown): string {
  if (typeof value !== "string" || value.length > 2048) throw new ApiError("invalid_target", 400);
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new ApiError("invalid_target", 400);
  }
  if (parsed.username || parsed.password || parsed.hash || parsed.toString() !== mcpOAuthResource(origin)) {
    throw new ApiError("invalid_target", 400);
  }
  return parsed.toString();
}

export function oauthDiscovery(origin: string): Record<string, unknown> {
  const base = new URL(origin);
  return {
    issuer: base.origin,
    authorization_endpoint: new URL("/oauth/authorize", base).toString(),
    token_endpoint: new URL("/oauth/token", base).toString(),
    revocation_endpoint: new URL("/oauth/revoke", base).toString(),
    registration_endpoint: new URL("/oauth/register", base).toString(),
    response_types_supported: ["code"],
    response_modes_supported: ["query"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    code_challenge_methods_supported: ["S256"],
    scopes_supported: [...OAUTH_SCOPES],
    token_endpoint_auth_methods_supported: ["none"],
    revocation_endpoint_auth_methods_supported: ["none"],
    authorization_response_iss_parameter_supported: true,
  };
}

export function oauthProtectedResourceMetadata(origin: string): Record<string, unknown> {
  const base = new URL(origin);
  return {
    resource: mcpOAuthResource(origin),
    authorization_servers: [base.origin],
    bearer_methods_supported: ["header"],
    scopes_supported: [...OAUTH_SCOPES],
    resource_name: "oraja training MCP",
  };
}

export async function registerClient(
  db: D1Database,
  input: { clientName: unknown; redirectUris: unknown },
  now = Math.floor(Date.now() / 1000),
): Promise<{ client_id: string; client_name: string; redirect_uris: string[] }> {
  if (typeof input.clientName !== "string" || input.clientName.trim().length < 1 || input.clientName.length > 120) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (!Array.isArray(input.redirectUris) || input.redirectUris.length === 0) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  const redirectUris = input.redirectUris.map(validRedirectUri);
  const clientId = `client_${randomSecret(18)}`;
  await db
    .prepare(
      "INSERT INTO oauth_clients(client_id, client_name, redirect_uris_json, created_at) VALUES (?1, ?2, ?3, ?4)",
    )
    .bind(clientId, input.clientName.trim(), JSON.stringify(redirectUris), now)
    .run();
  return { client_id: clientId, client_name: input.clientName.trim(), redirect_uris: redirectUris };
}

export async function issueAuthorizationCode(
  db: D1Database,
  input: {
    clientId: unknown;
    redirectUri: unknown;
    scope: unknown;
    codeChallenge: unknown;
    codeChallengeMethod: unknown;
    resource: unknown;
    issuer: string;
    accountId: string;
    profileId: string;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<string> {
  if (typeof input.clientId !== "string" || typeof input.codeChallenge !== "string" || input.codeChallengeMethod !== "S256") {
    throw new ApiError("invalid_request", 400);
  }
  const redirectUri = validRedirectUri(input.redirectUri);
  const resource = validResource(input.issuer, input.resource);
  if (!/^[A-Za-z0-9_-]{43}$/.test(input.codeChallenge)) throw new ApiError("invalid_request", 400);
  const scopes = parseScopes(input.scope);
  const client = await db
    .prepare("SELECT client_id, client_name, redirect_uris_json, revoked_at FROM oauth_clients WHERE client_id = ?1")
    .bind(input.clientId)
    .first<ClientRow>();
  if (!client || client.revoked_at !== null || !parseRedirectUris(client.redirect_uris_json).includes(redirectUri)) {
    throw new ApiError("invalid_client", 400);
  }
  const code = randomSecret(32);
  const grantId = `grant_${randomSecret(18)}`;
  await db.batch([
    db.prepare(
      `INSERT INTO oauth_grants(
         id, client_id, account_id, profile_id, resource, scope, created_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
    ).bind(grantId, input.clientId, input.accountId, input.profileId, resource, scopes.join(" "), now),
    db.prepare(
      `INSERT INTO oauth_authorization_codes(
         code_hash, client_id, account_id, profile_id, redirect_uri, scope,
         code_challenge, code_challenge_method, created_at, expires_at, grant_id, resource
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'S256', ?8, ?9, ?10, ?11)`,
    ).bind(await sha256Hex(code), input.clientId, input.accountId, input.profileId, redirectUri, scopes.join(" "), input.codeChallenge, now, now + 300, grantId, resource),
  ]);
  return code;
}

export async function exchangeAuthorizationCode(
  db: D1Database,
  input: { code: unknown; clientId: unknown; redirectUri: unknown; codeVerifier: unknown; resource: unknown; issuer: string },
  now = Math.floor(Date.now() / 1000),
): Promise<OAuthTokenResponse> {
  if (typeof input.code !== "string" || typeof input.clientId !== "string" || typeof input.codeVerifier !== "string") {
    throw new ApiError("invalid_grant", 400);
  }
  const redirectUri = validRedirectUri(input.redirectUri);
  const resource = validResource(input.issuer, input.resource);
  if (input.codeVerifier.length < 43 || input.codeVerifier.length > 128 || !/^[A-Za-z0-9._~-]+$/.test(input.codeVerifier)) {
    throw new ApiError("invalid_grant", 400);
  }
  const row = await db
    .prepare(
      `SELECT code_hash, client_id, account_id, profile_id, redirect_uri, scope,
              code_challenge, code_challenge_method, expires_at, used_at, grant_id, resource
         FROM oauth_authorization_codes WHERE code_hash = ?1`,
    )
    .bind(await sha256Hex(input.code))
    .first<CodeRow>();
  if (!row || row.used_at !== null || row.expires_at <= now || row.client_id !== input.clientId || row.redirect_uri !== redirectUri || row.resource !== resource) {
    throw new ApiError("invalid_grant", 400);
  }
  const activeClient = await db.prepare(
    "SELECT client_id FROM oauth_clients WHERE client_id = ?1 AND revoked_at IS NULL",
  ).bind(row.client_id).first<{ client_id: string }>();
  const activeGrant = await db.prepare(
    "SELECT id FROM oauth_grants WHERE id = ?1 AND revoked_at IS NULL",
  ).bind(row.grant_id).first<{ id: string }>();
  if (!activeClient || !activeGrant) throw new ApiError("invalid_grant", 400);
  const verifierHash = await sha256Hex(input.codeVerifier);
  if (!constantTimeHexEqual(base64url(hexBytes(verifierHash)), row.code_challenge)) throw new ApiError("invalid_grant", 400);
  const consumed = await db
    .prepare("UPDATE oauth_authorization_codes SET used_at = ?1 WHERE code_hash = ?2 AND used_at IS NULL")
    .bind(now, row.code_hash)
    .run();
  if ((consumed.meta?.changes ?? 0) !== 1) throw new ApiError("invalid_grant", 400);
  return issueTokenPair(db, {
    clientId: row.client_id,
    accountId: row.account_id,
    profileId: row.profile_id,
    grantId: row.grant_id,
    familyId: `family_${randomSecret(18)}`,
    resource: row.resource,
    scope: row.scope,
  }, now);
}

async function issueTokenPair(
  db: D1Database,
  owner: { clientId: string; accountId: string; profileId: string; grantId: string; familyId: string; resource: string; scope: string },
  now: number,
): Promise<OAuthTokenResponse> {
  const accessToken = randomSecret(32);
  const refreshToken = randomSecret(32);
  const accessHash = await sha256Hex(accessToken);
  const refreshHash = await sha256Hex(refreshToken);
  await db.batch([
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'access', ?10)`,
    ).bind(accessHash, owner.clientId, owner.accountId, owner.profileId, owner.scope, now, now + 3600, owner.grantId, owner.resource, owner.familyId),
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'refresh', ?10)`,
    ).bind(refreshHash, owner.clientId, owner.accountId, owner.profileId, owner.scope, now, now + 30 * 86400, owner.grantId, owner.resource, owner.familyId),
  ]);
  return {
    access_token: accessToken,
    refresh_token: refreshToken,
    token_type: "Bearer",
    expires_in: 3600,
    scope: owner.scope,
    resource: owner.resource,
  };
}

async function revokeGrant(db: D1Database, grantId: string, reason: string, now: number, reuse = false): Promise<void> {
  await db.batch([
    db.prepare(
      `UPDATE oauth_grants
          SET revoked_at = COALESCE(revoked_at, ?1), revoke_reason = COALESCE(revoke_reason, ?2),
              reuse_detected_at = CASE WHEN ?3 = 1 THEN COALESCE(reuse_detected_at, ?1) ELSE reuse_detected_at END
        WHERE id = ?4`,
    ).bind(now, reason, reuse ? 1 : 0, grantId),
    db.prepare("UPDATE oauth_tokens SET revoked_at = COALESCE(revoked_at, ?1) WHERE grant_id = ?2").bind(now, grantId),
    db.prepare("UPDATE oauth_authorization_codes SET used_at = COALESCE(used_at, ?1) WHERE grant_id = ?2").bind(now, grantId),
  ]);
}

export async function rotateRefreshToken(
  db: D1Database,
  input: { refreshToken: unknown; clientId: unknown; resource: unknown; issuer: string },
  now = Math.floor(Date.now() / 1000),
): Promise<OAuthTokenResponse> {
  if (typeof input.refreshToken !== "string" || typeof input.clientId !== "string") {
    throw new ApiError("invalid_grant", 400);
  }
  const resource = validResource(input.issuer, input.resource);
  const tokenHash = await sha256Hex(input.refreshToken);
  const row = await db.prepare(
    `SELECT token_hash, client_id, account_id, profile_id, scope, expires_at, revoked_at,
            token_kind, grant_id, resource, family_id, rotated_to_hash
       FROM oauth_tokens WHERE token_hash = ?1`,
  ).bind(tokenHash).first<TokenRow & { token_hash: string }>();
  if (!row || row.token_kind !== "refresh" || row.client_id !== input.clientId || row.resource !== resource || !row.family_id) {
    throw new ApiError("invalid_grant", 400);
  }
  if (row.revoked_at !== null) {
    if (row.rotated_to_hash !== null) await revokeGrant(db, row.grant_id, "refresh_token_reuse", now, true);
    throw new ApiError("invalid_grant", 400);
  }
  if (row.expires_at <= now) {
    await revokeGrant(db, row.grant_id, "refresh_token_expired", now);
    throw new ApiError("invalid_grant", 400);
  }
  const activeGrant = await db.prepare(
    `SELECT g.id
       FROM oauth_grants g
       JOIN oauth_clients c ON c.client_id = g.client_id
      WHERE g.id = ?1 AND g.revoked_at IS NULL AND c.revoked_at IS NULL`,
  ).bind(row.grant_id).first<{ id: string }>();
  if (!activeGrant) throw new ApiError("invalid_grant", 400);

  const accessToken = randomSecret(32);
  const refreshToken = randomSecret(32);
  const accessHash = await sha256Hex(accessToken);
  const refreshHash = await sha256Hex(refreshToken);
  const results = await db.batch([
    db.prepare(
      `UPDATE oauth_tokens
          SET revoked_at = ?1, rotated_to_hash = ?2
        WHERE token_hash = ?3 AND token_kind = 'refresh' AND revoked_at IS NULL`,
    ).bind(now, refreshHash, tokenHash),
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'access', ?10)`,
    ).bind(accessHash, row.client_id, row.account_id, row.profile_id, row.scope, now, now + 3600, row.grant_id, row.resource, row.family_id),
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'refresh', ?10)`,
    ).bind(refreshHash, row.client_id, row.account_id, row.profile_id, row.scope, now, now + 30 * 86400, row.grant_id, row.resource, row.family_id),
  ]);
  if ((results[0]?.meta?.changes ?? 0) !== 1) {
    await revokeGrant(db, row.grant_id, "refresh_token_reuse", now, true);
    throw new ApiError("invalid_grant", 400);
  }
  return { access_token: accessToken, refresh_token: refreshToken, token_type: "Bearer", expires_in: 3600, scope: row.scope, resource: row.resource };
}

export async function revokeOAuthToken(
  db: D1Database,
  input: { token: unknown; clientId: unknown },
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  if (typeof input.token !== "string" || typeof input.clientId !== "string") return;
  const row = await db.prepare(
    "SELECT grant_id, client_id FROM oauth_tokens WHERE token_hash = ?1",
  ).bind(await sha256Hex(input.token)).first<{ grant_id: string | null; client_id: string }>();
  // RFC 7009 revocation deliberately does not reveal whether a token exists or
  // belongs to the requesting public client.
  if (row?.grant_id && row.client_id === input.clientId) await revokeGrant(db, row.grant_id, "client_revocation", now);
}

function constantTimeHexEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

export async function authenticateOAuthToken(
  db: D1Database,
  token: string | null | undefined,
  requiredScope?: OAuthScope,
  now = Math.floor(Date.now() / 1000),
  requiredResource?: string,
): Promise<OAuthPrincipal> {
  if (!token || token.length < 32) throw new ApiError("unauthorized", 401);
  const row = await db
    .prepare(
      `SELECT t.client_id, t.account_id, t.profile_id, t.scope, t.expires_at, t.revoked_at,
              t.token_kind, t.grant_id, t.resource, t.family_id, t.rotated_to_hash
         FROM oauth_tokens t
         JOIN oauth_grants g ON g.id = t.grant_id
         JOIN oauth_clients c ON c.client_id = t.client_id
        WHERE t.token_hash = ?1 AND g.revoked_at IS NULL AND c.revoked_at IS NULL`,
    )
    .bind(await sha256Hex(token))
    .first<TokenRow>();
  if (!row || row.token_kind !== "access" || row.revoked_at !== null || row.expires_at <= now || !row.resource || !row.grant_id) {
    throw new ApiError("invalid_token", 401);
  }
  if (requiredResource && row.resource !== requiredResource) throw new ApiError("invalid_token", 401);
  const scopes = parseScopes(row.scope);
  if (requiredScope && !scopes.includes(requiredScope)) throw new ApiError("insufficient_scope", 403);
  return { clientId: row.client_id, accountId: row.account_id, profileId: row.profile_id, scopes, resource: row.resource, grantId: row.grant_id, expiresAt: row.expires_at };
}

export function hasOAuthScope(principal: OAuthPrincipal, scope: OAuthScope): boolean {
  return principal.scopes.includes(scope);
}
