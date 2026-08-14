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
};

type TokenRow = {
  client_id: string;
  account_id: string;
  profile_id: string;
  scope: string;
  expires_at: number;
  revoked_at: number | null;
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
    if (url.protocol !== "https:" && url.hostname !== "localhost") throw new Error("scheme");
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

export function oauthDiscovery(origin: string): Record<string, unknown> {
  const base = new URL(origin);
  return {
    issuer: base.origin,
    authorization_endpoint: new URL("/oauth/authorize", base).toString(),
    token_endpoint: new URL("/oauth/token", base).toString(),
    registration_endpoint: new URL("/oauth/register", base).toString(),
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code"],
    code_challenge_methods_supported: ["S256"],
    scopes_supported: [...OAUTH_SCOPES],
    token_endpoint_auth_methods_supported: ["none"],
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
    accountId: string;
    profileId: string;
  },
  now = Math.floor(Date.now() / 1000),
): Promise<string> {
  if (typeof input.clientId !== "string" || typeof input.codeChallenge !== "string") {
    throw new ApiError("invalid_request", 400);
  }
  const redirectUri = validRedirectUri(input.redirectUri);
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
  await db
    .prepare(
      `INSERT INTO oauth_authorization_codes(
         code_hash, client_id, account_id, profile_id, redirect_uri, scope,
         code_challenge, code_challenge_method, created_at, expires_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'S256', ?8, ?9)`,
    )
    .bind(await sha256Hex(code), input.clientId, input.accountId, input.profileId, redirectUri, scopes.join(" "), input.codeChallenge, now, now + 300)
    .run();
  return code;
}

export async function exchangeAuthorizationCode(
  db: D1Database,
  input: { code: unknown; clientId: unknown; redirectUri: unknown; codeVerifier: unknown },
  now = Math.floor(Date.now() / 1000),
): Promise<{ access_token: string; token_type: "Bearer"; expires_in: number; scope: string }> {
  if (typeof input.code !== "string" || typeof input.clientId !== "string" || typeof input.codeVerifier !== "string") {
    throw new ApiError("invalid_grant", 400);
  }
  const redirectUri = validRedirectUri(input.redirectUri);
  if (input.codeVerifier.length < 43 || input.codeVerifier.length > 128 || !/^[A-Za-z0-9._~-]+$/.test(input.codeVerifier)) {
    throw new ApiError("invalid_grant", 400);
  }
  const row = await db
    .prepare(
      `SELECT code_hash, client_id, account_id, profile_id, redirect_uri, scope,
              code_challenge, code_challenge_method, expires_at, used_at
         FROM oauth_authorization_codes WHERE code_hash = ?1`,
    )
    .bind(await sha256Hex(input.code))
    .first<CodeRow>();
  if (!row || row.used_at !== null || row.expires_at <= now || row.client_id !== input.clientId || row.redirect_uri !== redirectUri) {
    throw new ApiError("invalid_grant", 400);
  }
  const verifierHash = await sha256Hex(input.codeVerifier);
  if (!constantTimeHexEqual(base64url(hexBytes(verifierHash)), row.code_challenge)) throw new ApiError("invalid_grant", 400);
  const consumed = await db
    .prepare("UPDATE oauth_authorization_codes SET used_at = ?1 WHERE code_hash = ?2 AND used_at IS NULL")
    .bind(now, row.code_hash)
    .run();
  if ((consumed.meta?.changes ?? 0) !== 1) throw new ApiError("invalid_grant", 400);
  const token = randomSecret(32);
  const expiresIn = 3600;
  await db
    .prepare(
      "INSERT INTO oauth_tokens(token_hash, client_id, account_id, profile_id, scope, created_at, expires_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
    )
    .bind(await sha256Hex(token), row.client_id, row.account_id, row.profile_id, row.scope, now, now + expiresIn)
    .run();
  return { access_token: token, token_type: "Bearer", expires_in: expiresIn, scope: row.scope };
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
): Promise<OAuthPrincipal> {
  if (!token || token.length < 32) throw new ApiError("unauthorized", 401);
  const row = await db
    .prepare("SELECT client_id, account_id, profile_id, scope, expires_at, revoked_at FROM oauth_tokens WHERE token_hash = ?1")
    .bind(await sha256Hex(token))
    .first<TokenRow>();
  if (!row || row.revoked_at !== null || row.expires_at <= now) throw new ApiError("invalid_token", 401);
  const scopes = parseScopes(row.scope);
  if (requiredScope && !scopes.includes(requiredScope)) throw new ApiError("insufficient_scope", 403);
  return { clientId: row.client_id, accountId: row.account_id, profileId: row.profile_id, scopes, expiresAt: row.expires_at };
}

export function hasOAuthScope(principal: OAuthPrincipal, scope: OAuthScope): boolean {
  return principal.scopes.includes(scope);
}
