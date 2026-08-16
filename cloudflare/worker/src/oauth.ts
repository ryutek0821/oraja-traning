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

type ClientMetadataCacheRow = {
  metadata_json: string;
  expires_at: number;
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

type OAuthEndpoint = "authorize" | "token" | "revoke";

const OAUTH_RATE_LIMITS: Record<OAuthEndpoint, number> = {
  authorize: 30,
  token: 20,
  revoke: 30,
};

function randomSecret(bytes = 32): string {
  const value = crypto.getRandomValues(new Uint8Array(bytes));
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export async function enforceOAuthRateLimit(
  db: D1Database,
  endpoint: OAuthEndpoint,
  key: string,
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  const keyHash = await sha256Hex(`oauth-rate:${endpoint}:${key}`);
  const maximum = OAUTH_RATE_LIMITS[endpoint];
  await db.prepare(
    `INSERT INTO oauth_rate_limits(endpoint, key_hash, request_count, window_started_at, blocked_until)
     VALUES (?1, ?2, 1, ?3, 0)
     ON CONFLICT(endpoint, key_hash) DO UPDATE SET
       request_count = CASE
         WHEN ?3 - oauth_rate_limits.window_started_at >= 60 THEN 1
         ELSE oauth_rate_limits.request_count + 1
       END,
       window_started_at = CASE
         WHEN ?3 - oauth_rate_limits.window_started_at >= 60 THEN ?3
         ELSE oauth_rate_limits.window_started_at
       END,
       blocked_until = CASE
         WHEN ?3 - oauth_rate_limits.window_started_at < 60
          AND oauth_rate_limits.request_count + 1 > ?4 THEN ?3 + 60
         ELSE 0
       END`,
  ).bind(endpoint, keyHash, now, maximum).run();
  const row = await db.prepare(
    "SELECT blocked_until FROM oauth_rate_limits WHERE endpoint = ?1 AND key_hash = ?2",
  ).bind(endpoint, keyHash).first<{ blocked_until: number }>();
  if (row && row.blocked_until > now) throw new ApiError("rate_limited", 429, true, row.blocked_until - now);
}

export async function appendOAuthAudit(
  db: D1Database,
  event: {
    eventType: string;
    status: "success" | "denied";
    reasonCode?: string;
    requestId?: string;
    accountId?: string;
    profileId?: string;
    resource?: string;
    clientId?: string;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  await db.prepare(
    `INSERT INTO audit_events(
       id, account_id, profile_id, actor_kind, event_type, reason_code,
       request_id, resource_hash, input_hash, status, occurred_at
     ) VALUES (?1, ?2, ?3, 'oauth', ?4, ?5, ?6, ?7, ?8, ?9, ?10)`,
  ).bind(
    crypto.randomUUID(),
    event.accountId ?? null,
    event.profileId ?? null,
    event.eventType,
    event.reasonCode ?? null,
    event.requestId ?? null,
    event.resource ? await sha256Hex(event.resource) : null,
    event.clientId ? await sha256Hex(event.clientId) : null,
    event.status,
    now,
  ).run();
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

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  if (normalized === "localhost" || normalized === "[::1]") return true;
  const octets = normalized.split(".");
  return octets.length === 4
    && octets[0] === "127"
    && octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255);
}

function validRedirectUri(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048) throw new ApiError("invalid_redirect_uri", 400);
  try {
    const url = new URL(value);
    const secure = url.protocol === "https:";
    const loopback = url.protocol === "http:" && isLoopbackHostname(url.hostname);
    if ((!secure && !loopback) || url.hash) throw new Error("scheme");
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

function clientMetadataUrl(clientId: string): URL {
  let url: URL;
  try {
    url = new URL(clientId);
  } catch {
    throw new ApiError("invalid_client", 400);
  }
  const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
  const blockedName = hostname === "localhost"
    || [".localhost", ".local", ".internal", ".home", ".arpa", ".test"].some((suffix) => hostname.endsWith(suffix));
  const ipLiteral = /^\d+(?:\.\d+){3}$/.test(hostname) || hostname.includes(":") || /^0x/i.test(hostname);
  if (
    url.protocol !== "https:" || (url.port && url.port !== "443") || url.username || url.password
    || url.search || url.hash || blockedName || ipLiteral || !hostname.includes(".") || url.toString() !== clientId
  ) throw new ApiError("invalid_client", 400);
  return url;
}

async function limitedJson(response: Response, maximumBytes = 32 * 1024): Promise<unknown> {
  const declared = Number(response.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > maximumBytes) throw new ApiError("invalid_client_metadata", 400);
  const reader = response.body?.getReader();
  if (!reader) throw new ApiError("invalid_client_metadata", 400);
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > maximumBytes) {
      await reader.cancel();
      throw new ApiError("invalid_client_metadata", 400);
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new ApiError("invalid_client_metadata", 400);
  }
}

function validateClientMetadata(clientId: string, value: unknown): { clientName: string; redirectUris: string[]; json: string } {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_client_metadata", 400);
  const metadata = value as Record<string, unknown>;
  if (metadata.client_id !== clientId || typeof metadata.client_name !== "string" || metadata.client_name.trim().length < 1 || metadata.client_name.length > 120) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (metadata.token_endpoint_auth_method !== "none") throw new ApiError("invalid_client_metadata", 400);
  if (!Array.isArray(metadata.response_types) || metadata.response_types.length !== 1 || metadata.response_types[0] !== "code") {
    throw new ApiError("invalid_client_metadata", 400);
  }
  const grants = metadata.grant_types;
  if (!Array.isArray(grants) || grants.some((grant) => grant !== "authorization_code" && grant !== "refresh_token") || !grants.includes("authorization_code")) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (!Array.isArray(metadata.redirect_uris) || metadata.redirect_uris.length === 0 || metadata.redirect_uris.length > 20) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  const redirectUris = [...new Set(metadata.redirect_uris.map(validRedirectUri))];
  if (redirectUris.length !== metadata.redirect_uris.length) throw new ApiError("invalid_client_metadata", 400);
  return { clientName: metadata.client_name.trim(), redirectUris, json: JSON.stringify(metadata) };
}

async function resolveOAuthClient(
  db: D1Database,
  clientId: string,
  now: number,
  fetcher: typeof fetch = fetch,
): Promise<ClientRow | null> {
  const stored = await db.prepare(
    "SELECT client_id, client_name, redirect_uris_json, revoked_at FROM oauth_clients WHERE client_id = ?1",
  ).bind(clientId).first<ClientRow>();
  if (stored && (!clientId.startsWith("https://") || stored.revoked_at !== null)) return stored;
  const url = clientMetadataUrl(clientId);
  const cached = await db.prepare(
    "SELECT metadata_json, expires_at FROM oauth_client_metadata_cache WHERE client_id = ?1",
  ).bind(clientId).first<ClientMetadataCacheRow>();
  let validated: ReturnType<typeof validateClientMetadata>;
  if (cached && cached.expires_at > now) {
    try {
      validated = validateClientMetadata(clientId, JSON.parse(cached.metadata_json));
    } catch {
      throw new ApiError("invalid_client", 400);
    }
  } else {
    let response: Response;
    try {
      response = await fetcher(new Request(url, { headers: { accept: "application/json" }, redirect: "manual", signal: AbortSignal.timeout(5000) }));
    } catch {
      throw new ApiError("invalid_client", 400);
    }
    const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() ?? "";
    if (!response.ok || response.status >= 300 || (contentType !== "application/json" && !contentType.endsWith("+json")) || response.url !== clientId) {
      throw new ApiError("invalid_client", 400);
    }
    validated = validateClientMetadata(clientId, await limitedJson(response));
    await db.prepare(
      `INSERT INTO oauth_client_metadata_cache(client_id, metadata_json, etag, fetched_at, expires_at)
       VALUES (?1, ?2, ?3, ?4, ?5)
       ON CONFLICT(client_id) DO UPDATE SET
         metadata_json = excluded.metadata_json, etag = excluded.etag,
         fetched_at = excluded.fetched_at, expires_at = excluded.expires_at`,
    ).bind(clientId, validated.json, response.headers.get("etag"), now, now + 3600).run();
  }
  await db.prepare(
    `INSERT INTO oauth_clients(client_id, client_name, redirect_uris_json, created_at)
     VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT(client_id) DO UPDATE SET
       client_name = excluded.client_name, redirect_uris_json = excluded.redirect_uris_json
     WHERE oauth_clients.revoked_at IS NULL`,
  ).bind(clientId, validated.clientName, JSON.stringify(validated.redirectUris), now).run();
  return db.prepare(
    "SELECT client_id, client_name, redirect_uris_json, revoked_at FROM oauth_clients WHERE client_id = ?1",
  ).bind(clientId).first<ClientRow>();
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

export async function requireDcrInitialAccessToken(configured: string | undefined, presented: string | null): Promise<void> {
  if (!configured) throw new ApiError("temporary_unavailable", 503, true, 5);
  const match = /^Bearer\s+([^\s]+)$/i.exec(presented ?? "");
  if (!match) throw new ApiError("invalid_token", 401);
  const [candidateHash, configuredHash] = await Promise.all([sha256Hex(match[1]), sha256Hex(configured)]);
  if (!constantTimeHexEqual(candidateHash, configuredHash)) throw new ApiError("invalid_token", 401);
}

export async function registerClient(
  db: D1Database,
  input: {
    clientName: unknown;
    redirectUris: unknown;
    tokenEndpointAuthMethod?: unknown;
    grantTypes?: unknown;
    responseTypes?: unknown;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<{ client_id: string; client_name: string; redirect_uris: string[] }> {
  if (typeof input.clientName !== "string" || input.clientName.trim().length < 1 || input.clientName.length > 120) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (!Array.isArray(input.redirectUris) || input.redirectUris.length === 0) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (input.tokenEndpointAuthMethod !== undefined && input.tokenEndpointAuthMethod !== "none") throw new ApiError("invalid_client_metadata", 400);
  if (input.responseTypes !== undefined && (!Array.isArray(input.responseTypes) || input.responseTypes.length !== 1 || input.responseTypes[0] !== "code")) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (input.grantTypes !== undefined && (!Array.isArray(input.grantTypes) || input.grantTypes.some((grant) => grant !== "authorization_code" && grant !== "refresh_token") || !input.grantTypes.includes("authorization_code"))) {
    throw new ApiError("invalid_client_metadata", 400);
  }
  if (input.redirectUris.length > 20) throw new ApiError("invalid_client_metadata", 400);
  const redirectUris = [...new Set(input.redirectUris.map(validRedirectUri))];
  if (redirectUris.length !== input.redirectUris.length) throw new ApiError("invalid_client_metadata", 400);
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
    requestId?: string;
    fetcher?: typeof fetch;
    accountId: string;
    profileId: string;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<string> {
  const authorization = await inspectAuthorizationRequest(db, input, now);
  const { redirectUri, resource, scopes } = authorization;
  const clientId = authorization.clientId;
  const codeChallenge = authorization.codeChallenge;
  const code = randomSecret(32);
  const grantId = `grant_${randomSecret(18)}`;
  await db.batch([
    db.prepare(
      `INSERT INTO oauth_grants(
         id, client_id, account_id, profile_id, resource, scope, created_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
    ).bind(grantId, clientId, input.accountId, input.profileId, resource, scopes.join(" "), now),
    db.prepare(
      `INSERT INTO oauth_authorization_codes(
         code_hash, client_id, account_id, profile_id, redirect_uri, scope,
         code_challenge, code_challenge_method, created_at, expires_at, grant_id, resource
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'S256', ?8, ?9, ?10, ?11)`,
    ).bind(await sha256Hex(code), clientId, input.accountId, input.profileId, redirectUri, scopes.join(" "), codeChallenge, now, now + 300, grantId, resource),
  ]);
  await appendOAuthAudit(db, {
    eventType: "oauth.authorized", status: "success", requestId: input.requestId,
    accountId: input.accountId, profileId: input.profileId, resource, clientId,
  }, now);
  return code;
}

export async function inspectAuthorizationRequest(
  db: D1Database,
  input: {
    clientId: unknown;
    redirectUri: unknown;
    scope: unknown;
    codeChallenge: unknown;
    codeChallengeMethod: unknown;
    resource: unknown;
    issuer: string;
    fetcher?: typeof fetch;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<{ clientId: string; clientName: string; redirectUri: string; scopes: OAuthScope[]; resource: string; codeChallenge: string }> {
  if (typeof input.clientId !== "string" || typeof input.codeChallenge !== "string" || input.codeChallengeMethod !== "S256") {
    throw new ApiError("invalid_request", 400);
  }
  const redirectUri = validRedirectUri(input.redirectUri);
  const resource = validResource(input.issuer, input.resource);
  if (!/^[A-Za-z0-9_-]{43}$/.test(input.codeChallenge)) throw new ApiError("invalid_request", 400);
  const scopes = parseScopes(input.scope);
  const client = await resolveOAuthClient(db, input.clientId, now, input.fetcher);
  if (!client || client.revoked_at !== null || !parseRedirectUris(client.redirect_uris_json).includes(redirectUri)) {
    throw new ApiError("invalid_client", 400);
  }
  return { clientId: input.clientId, clientName: client.client_name, redirectUri, scopes, resource, codeChallenge: input.codeChallenge };
}

export async function exchangeAuthorizationCode(
  db: D1Database,
  input: { code: unknown; clientId: unknown; redirectUri: unknown; codeVerifier: unknown; resource: unknown; issuer: string; requestId?: string },
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
  const response = await issueTokenPair(db, {
    clientId: row.client_id,
    accountId: row.account_id,
    profileId: row.profile_id,
    grantId: row.grant_id,
    familyId: `family_${randomSecret(18)}`,
    resource: row.resource,
    scope: row.scope,
  }, now, row.code_hash);
  await appendOAuthAudit(db, {
    eventType: "oauth.token_issued", status: "success", requestId: input.requestId,
    accountId: row.account_id, profileId: row.profile_id, resource: row.resource, clientId: row.client_id,
  }, now);
  return response;
}

async function issueTokenPair(
  db: D1Database,
  owner: { clientId: string; accountId: string; profileId: string; grantId: string; familyId: string; resource: string; scope: string },
  now: number,
  authorizationCodeHash: string,
): Promise<OAuthTokenResponse> {
  const accessToken = randomSecret(32);
  const refreshToken = randomSecret(32);
  const accessHash = await sha256Hex(accessToken);
  const refreshHash = await sha256Hex(refreshToken);
  const markerWords = crypto.getRandomValues(new Uint32Array(2));
  const claimMarker = -((markerWords[0] & 0xfffff) * 0x100000000 + markerWords[1] + 1);
  const results = await db.batch([
    db.prepare(
      "UPDATE oauth_authorization_codes SET used_at = ?1 WHERE code_hash = ?2 AND used_at IS NULL",
    ).bind(claimMarker, authorizationCodeHash),
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'access', ?10
           WHERE EXISTS (
             SELECT 1 FROM oauth_authorization_codes WHERE code_hash = ?11 AND used_at = ?12
           )`,
    ).bind(accessHash, owner.clientId, owner.accountId, owner.profileId, owner.scope, now, now + 3600, owner.grantId, owner.resource, owner.familyId, authorizationCodeHash, claimMarker),
    db.prepare(
      `INSERT INTO oauth_tokens(
         token_hash, client_id, account_id, profile_id, scope, created_at, expires_at,
         grant_id, resource, token_kind, family_id
       ) SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'refresh', ?10
           WHERE EXISTS (
             SELECT 1 FROM oauth_authorization_codes WHERE code_hash = ?11 AND used_at = ?12
           )`,
    ).bind(refreshHash, owner.clientId, owner.accountId, owner.profileId, owner.scope, now, now + 30 * 86400, owner.grantId, owner.resource, owner.familyId, authorizationCodeHash, claimMarker),
    db.prepare(
      "UPDATE oauth_authorization_codes SET used_at = ?1 WHERE code_hash = ?2 AND used_at = ?3",
    ).bind(now, authorizationCodeHash, claimMarker),
  ]);
  if (results.some((result) => (result.meta?.changes ?? 0) !== 1)) throw new ApiError("invalid_grant", 400);
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
  input: { refreshToken: unknown; clientId: unknown; resource: unknown; issuer: string; requestId?: string },
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
    if (row.rotated_to_hash !== null) {
      await revokeGrant(db, row.grant_id, "refresh_token_reuse", now, true);
      await appendOAuthAudit(db, {
        eventType: "oauth.refresh_reuse", status: "denied", reasonCode: "refresh_token_reuse", requestId: input.requestId,
        accountId: row.account_id, profileId: row.profile_id, resource: row.resource, clientId: row.client_id,
      }, now);
    }
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
  await appendOAuthAudit(db, {
    eventType: "oauth.token_refreshed", status: "success", requestId: input.requestId,
    accountId: row.account_id, profileId: row.profile_id, resource: row.resource, clientId: row.client_id,
  }, now);
  return { access_token: accessToken, refresh_token: refreshToken, token_type: "Bearer", expires_in: 3600, scope: row.scope, resource: row.resource };
}

export async function revokeOAuthToken(
  db: D1Database,
  input: { token: unknown; clientId: unknown; requestId?: string },
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  if (typeof input.token !== "string" || typeof input.clientId !== "string") return;
  const row = await db.prepare(
    "SELECT grant_id, client_id FROM oauth_tokens WHERE token_hash = ?1",
  ).bind(await sha256Hex(input.token)).first<{ grant_id: string | null; client_id: string }>();
  // RFC 7009 revocation deliberately does not reveal whether a token exists or
  // belongs to the requesting public client.
  if (row?.grant_id && row.client_id === input.clientId) await revokeGrant(db, row.grant_id, "client_revocation", now);
  await appendOAuthAudit(db, {
    eventType: "oauth.revoked", status: "success", requestId: input.requestId,
    clientId: input.clientId,
  }, now);
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
