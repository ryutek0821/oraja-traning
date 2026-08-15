import { Container } from "@cloudflare/containers";
import { CloudflareAuthEmailSender } from "./email";
import {
  AuthError,
  changePassword,
  completePasswordReset,
  confirmEmail,
  csrfCookie,
  expiredCsrfCookie,
  expiredSessionCookie,
  loginAccount,
  logoutSession,
  readCookie,
  registerAccount,
  requestEmailChange,
  requestPasswordReset,
  rotateSession,
  sessionCookie,
  useRecoveryCode,
  verifyCsrf,
} from "./auth";
import {
  ApiError,
  IR_READ_PATHS,
  authErrorResponse as irErrorResponse,
  authenticateDeviceToken,
  bearerToken,
  enforceIrRateLimit,
  eventDigest,
  issueDeviceToken,
  listDevices,
  readJsonBody,
  readIrMethod,
  renameDevice,
  revokeAllDevices,
  revokeDevice,
  sessionAccount,
  validateIrEvent,
  type DeviceIdentity,
  type PlayAck,
} from "./ir-api";
import { ProfileDurableObject } from "./profile-do";
import { dashboardData } from "./dashboard";
import { createAdvisorProposal, decideAdvisorProposal, listAdvisorProposals, type AdvisorOwner } from "./advisor";
import { handleMcp } from "./mcp";
import {
  appendOAuthAudit,
  authenticateOAuthToken,
  enforceOAuthRateLimit,
  exchangeAuthorizationCode,
  inspectAuthorizationRequest,
  issueAuthorizationCode,
  oauthDiscovery,
  oauthProtectedResourceMetadata,
  registerClient,
  requireDcrInitialAccessToken,
  revokeOAuthToken,
  rotateRefreshToken,
} from "./oauth";
import {
  cancelDeletion,
  claimExportDownload,
  claimPrivacyTasks,
  expireDataExports,
  finalizeDeletion,
  purgeDueProfiles,
  recordPrivacyTaskResult,
  requestDataExport,
  requestDeletion,
  runPurgeTask,
} from "./privacy";
import { processExportSnapshots } from "./privacy-export";
import { D1UploadSessionStore } from "./upload-store";
import { EnvelopeCrypto, UploadService, handleUploadRequest } from "./upload-protocol";
import {
  D1JobLedger,
  JobDispatcher,
  JobLedgerError,
  consumeQueueMessage,
  dispatchSchedule,
  type JobEnvelope,
} from "./job-ledger";
import { SCHEDULE_CRONS, type ScheduleName } from "./workflow-state";
import { handleCapabilityTable } from "./tables";
import {
  listTableCapabilities,
  readProfileSettings,
  revokeTableCapability,
  rotateTableCapability,
  updateProfileSettings,
} from "./profile-settings";
export { GenerateWorkflow } from "./workflow";

export { ProfileDurableObject };

export interface Env {
  ENVIRONMENT: string;
  BUILD_VERSION: string;
  PUBLIC_ORIGIN: string;
  CORS_ORIGINS: string;
  ORIGIN_ALLOWLIST: string;
  CONTROL_DB: D1Database;
  PROFILE_DO: DurableObjectNamespace;
  PYTHON_PROCESSOR: DurableObjectNamespace;
  RAW_BUCKET: R2Bucket;
  ARTIFACT_BUCKET: R2Bucket;
  BACKUP_BUCKET: R2Bucket;
  JOB_QUEUE: Queue;
  GENERATE_WORKFLOW: Workflow;
  ASSETS?: Fetcher;
  AUTH_EMAIL?: SendEmail;
  AUTH_EMAIL_FROM?: string;
  EMAIL_ENCRYPTION_KEY?: string;
  AUTH_HASH_PEPPER?: string;
  DEVICE_TOKEN_PEPPER?: string;
  ENVELOPE_MASTER_KEY?: string;
  OAUTH_DCR_INITIAL_ACCESS_TOKEN?: string;
}

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

function json(value: unknown, status = 200, origin?: string, extraHeaders?: HeadersInit): Response {
  const headers = new Headers(JSON_HEADERS);
  if (origin) {
    headers.set("access-control-allow-origin", origin);
    headers.set("access-control-allow-credentials", "true");
    headers.append("vary", "Origin");
  }
  if (extraHeaders) {
    for (const [name, headerValue] of new Headers(extraHeaders)) headers.append(name, headerValue);
  }
  return new Response(JSON.stringify(value) + "\n", { status, headers });
}

function allowedOrigin(request: Request, env: Env): string | undefined {
  const origin = request.headers.get("Origin");
  if (!origin) return undefined;
  const allowed = env.CORS_ORIGINS.split(",").map((item) => item.trim()).filter(Boolean);
  return allowed.includes(origin) ? origin : undefined;
}

function requestId(request: Request): string {
  const supplied = request.headers.get("X-Request-ID")?.trim();
  return supplied && supplied.length <= 128 && /^[A-Za-z0-9._:-]+$/.test(supplied)
    ? supplied
    : crypto.randomUUID();
}

function clientIp(request: Request): string {
  return request.headers.get("CF-Connecting-IP")?.trim() || "unknown";
}

async function requestPayload(request: Request): Promise<Record<string, unknown>> {
  const contentLength = Number(request.headers.get("Content-Length") ?? "0");
  if (contentLength > 64 * 1024) throw new AuthError("payload_too_large", 413);
  const body: unknown = await request.json();
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new AuthError("invalid_request", 400);
  }
  return body as Record<string, unknown>;
}

function emailSender(env: Env): CloudflareAuthEmailSender | undefined {
  if (!env.AUTH_EMAIL || !env.AUTH_EMAIL_FROM) return undefined;
  return new CloudflareAuthEmailSender(env.AUTH_EMAIL, env.AUTH_EMAIL_FROM);
}

function authOptions(request: Request, env: Env) {
  return {
    now: Math.floor(Date.now() / 1000),
    requestId: requestId(request),
    ipAddress: clientIp(request),
    publicOrigin: env.PUBLIC_ORIGIN,
    emailSender: emailSender(env),
    emailEncryptionSecret: env.EMAIL_ENCRYPTION_KEY,
    hashingSecret: env.AUTH_HASH_PEPPER,
  };
}

function sessionHeaders(result: { sessionToken: string; csrfToken: string; expiresAt: number }): Headers {
  const headers = new Headers();
  headers.append("set-cookie", sessionCookie(result.sessionToken));
  headers.append("set-cookie", csrfCookie(result.csrfToken));
  headers.set("x-session-expires-at", String(result.expiresAt));
  return headers;
}

function authErrorResponse(error: unknown, origin?: string): Response {
  if (error instanceof AuthError) {
    const headers: HeadersInit = {};
    if (error.retryAfterSeconds) headers["retry-after"] = String(error.retryAfterSeconds);
    return json({ error: { code: error.code } }, error.status, origin, headers);
  }
  return json({ error: { code: "internal_error" } }, 500, origin);
}

async function handleAuth(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/v1/auth/")) return null;
  const options = authOptions(request, env);
  try {
    if (url.pathname === "/v1/auth/register" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await registerAccount(env.CONTROL_DB, {
        userId: body.user_id,
        password: body.password,
        termsVersion: body.terms_version,
        privacyVersion: body.privacy_version,
        termsConsent: body.terms_consent,
        privacyConsent: body.privacy_consent,
        displayName: body.display_name,
        timezone: body.timezone,
        email: body.email,
      }, options);
      return json({
        account_public_id: result.accountPublicId,
        profile_public_id: result.profilePublicId,
        recovery_codes: result.recoveryCodes,
        email_confirmation_sent: result.emailConfirmationSent,
      }, 201, origin);
    }
    if (url.pathname === "/v1/auth/login" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await loginAccount(env.CONTROL_DB, body.user_id, body.password, options);
      return json({ account_public_id: result.accountPublicId }, 200, origin, sessionHeaders(result));
    }
    if (url.pathname === "/v1/auth/recovery" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await useRecoveryCode(env.CONTROL_DB, body.user_id, body.code, options);
      return json({ account_public_id: result.accountPublicId }, 200, origin, sessionHeaders(result));
    }
    if (url.pathname === "/v1/auth/password-reset/request" && request.method === "POST") {
      const body = await requestPayload(request);
      await requestPasswordReset(env.CONTROL_DB, body.email, options);
      return json({ accepted: true }, 202, origin);
    }
    if (url.pathname === "/v1/auth/password-reset/complete" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await completePasswordReset(env.CONTROL_DB, body.token, body.password, options);
      return json({ account_public_id: result.accountPublicId }, 200, origin, sessionHeaders(result));
    }
    if (url.pathname === "/v1/auth/verify-email" && (request.method === "GET" || request.method === "POST")) {
      const token = request.method === "GET" ? url.searchParams.get("token") : (await requestPayload(request)).token;
      await confirmEmail(env.CONTROL_DB, token, options);
      return json({ confirmed: true }, 200, origin);
    }

    const sessionToken = readCookie(request, "__Host-oraja_session");
    if (!sessionToken) throw new AuthError("unauthorized", 401);
    const csrf = request.headers.get("X-CSRF-Token");
    if (!(await verifyCsrf(env.CONTROL_DB, sessionToken, csrf, options.now))) {
      throw new AuthError("csrf_required", 403);
    }
    if (url.pathname === "/v1/auth/logout" && request.method === "POST") {
      await logoutSession(env.CONTROL_DB, sessionToken, options);
      const headers = new Headers();
      headers.append("set-cookie", expiredSessionCookie());
      headers.append("set-cookie", expiredCsrfCookie());
      if (origin) {
        headers.set("access-control-allow-origin", origin);
        headers.set("access-control-allow-credentials", "true");
      }
      return new Response(null, { status: 204, headers });
    }
    if (url.pathname === "/v1/auth/session/rotate" && request.method === "POST") {
      const result = await rotateSession(env.CONTROL_DB, sessionToken, options);
      return json({ account_public_id: result.accountPublicId }, 200, origin, sessionHeaders(result));
    }
    if (url.pathname === "/v1/auth/password" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await changePassword(env.CONTROL_DB, sessionToken, body.current_password, body.new_password, options);
      return json({ account_public_id: result.accountPublicId }, 200, origin, sessionHeaders(result));
    }
    if (url.pathname === "/v1/auth/email" && request.method === "POST") {
      const body = await requestPayload(request);
      return json(await requestEmailChange(env.CONTROL_DB, sessionToken, body.email, options), 202, origin);
    }
    return json({ error: { code: "not_found" } }, 404, origin);
  } catch (error) {
    return authErrorResponse(error, origin);
  }
}

function requireDevicePepper(env: Env): string {
  if (!env.DEVICE_TOKEN_PEPPER) throw new ApiError("temporary_unavailable", 503, true, 5);
  return env.DEVICE_TOKEN_PEPPER;
}

async function requireWebAccount(request: Request, env: Env, mutate: boolean): Promise<string> {
  const sessionToken = readCookie(request, "__Host-oraja_session");
  if (!sessionToken) throw new ApiError("unauthorized", 401);
  if (mutate) {
    const csrf = request.headers.get("X-CSRF-Token");
    if (!(await verifyCsrf(env.CONTROL_DB, sessionToken, csrf, Math.floor(Date.now() / 1000)))) {
      throw new ApiError("csrf_required", 403);
    }
  }
  return sessionAccount(env.CONTROL_DB, sessionToken);
}

function bearerTokenForOAuth(request: Request): string | null {
  const value = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+([^\s]+)$/i.exec(value);
  return match?.[1] ?? null;
}

function apiFailure(error: unknown, origin?: string): Response {
  if (error instanceof ApiError) {
    const headers = error.retryAfterSeconds ? { "retry-after": String(error.retryAfterSeconds) } : undefined;
    return json({ error: { code: error.code } }, error.status, origin, headers);
  }
  return json({ error: { code: "internal_error" } }, 500, origin);
}

function oauthFailure(error: unknown, origin?: string): Response {
  const code = error instanceof ApiError ? error.code : "server_error";
  const status = error instanceof ApiError ? error.status : 500;
  const headers = error instanceof ApiError && error.retryAfterSeconds
    ? { "retry-after": String(error.retryAfterSeconds) }
    : undefined;
  return json({ error: code }, status, origin, headers);
}

function htmlEscape(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" })[character] ?? character);
}

function oauthConsentHtml(input: {
  clientName: string;
  profileName: string;
  scopes: string[];
  fields: Record<string, string>;
  csrfToken: string;
}): string {
  const hidden = Object.entries({ ...input.fields, csrf_token: input.csrfToken })
    .map(([name, value]) => `<input type="hidden" name="${htmlEscape(name)}" value="${htmlEscape(value)}">`).join("");
  const scopes = input.scopes.map((scope) => `<li><code>${htmlEscape(scope)}</code></li>`).join("");
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>接続の確認 / 연결 확인</title></head><body><main><h1>接続の確認 / 연결 확인</h1><p><strong>${htmlEscape(input.clientName)}</strong> がプロフィール <strong>${htmlEscape(input.profileName)}</strong> へのアクセスを求めています。</p><p><strong>${htmlEscape(input.clientName)}</strong>에서 프로필 <strong>${htmlEscape(input.profileName)}</strong>에 대한 접근을 요청합니다.</p><h2>許可する権限 / 허용할 권한</h2><ul>${scopes}</ul><p>許可後も設定から連携全体を失効できます。/ 허용 후에도 설정에서 연결 전체를 취소할 수 있습니다.</p><form method="post" action="/oauth/authorize">${hidden}<button type="submit" name="decision" value="approve">許可 / 허용</button><button type="submit" name="decision" value="deny">拒否 / 거부</button></form></main></body></html>`;
}

async function handleOAuthRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (!["/oauth/register", "/oauth/authorize", "/oauth/token", "/oauth/revoke", "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration", "/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"].includes(url.pathname)) return null;
  const requestIdValue = requestId(request);
  let auditClientId: string | undefined;
  try {
    if ((url.pathname === "/.well-known/oauth-authorization-server" || url.pathname === "/.well-known/openid-configuration") && request.method === "GET") {
      return json(oauthDiscovery(env.PUBLIC_ORIGIN), 200, origin);
    }
    if ((url.pathname === "/.well-known/oauth-protected-resource" || url.pathname === "/.well-known/oauth-protected-resource/mcp") && request.method === "GET") {
      return json(oauthProtectedResourceMetadata(env.PUBLIC_ORIGIN), 200, origin);
    }
    if (url.pathname === "/oauth/register" && request.method === "POST") {
      await requireDcrInitialAccessToken(env.OAUTH_DCR_INITIAL_ACCESS_TOKEN, request.headers.get("authorization"));
      const body = await readJsonBody(request, 32 * 1024);
      return json(await registerClient(env.CONTROL_DB, {
        clientName: body.client_name,
        redirectUris: body.redirect_uris,
        tokenEndpointAuthMethod: body.token_endpoint_auth_method,
        grantTypes: body.grant_types,
        responseTypes: body.response_types,
      }), 201, origin);
    }
    if (url.pathname === "/oauth/authorize" && (request.method === "GET" || request.method === "POST")) {
      const form = request.method === "POST" ? await request.formData() : null;
      const parameter = (name: string): string | null => {
        const value = form ? form.get(name) : url.searchParams.get(name);
        return typeof value === "string" ? value : null;
      };
      if (parameter("response_type") !== "code") throw new ApiError("unsupported_response_type", 400);
      auditClientId = parameter("client_id") ?? undefined;
      await enforceOAuthRateLimit(env.CONTROL_DB, "authorize", `${auditClientId ?? "missing"}:${clientIp(request)}`);
      const accountId = await requireWebAccount(request, env, false);
      const profile = await env.CONTROL_DB.prepare(
        "SELECT id, display_name FROM profiles WHERE account_id = ?1 AND status = 'active' ORDER BY created_at LIMIT 1",
      ).bind(accountId).first<{ id: string; display_name: string }>();
      if (!profile) throw new ApiError("profile_not_found", 404);
      const authorizationInput = {
        clientId: parameter("client_id"),
        redirectUri: parameter("redirect_uri"),
        scope: parameter("scope"),
        codeChallenge: parameter("code_challenge"),
        codeChallengeMethod: parameter("code_challenge_method"),
        resource: parameter("resource"),
        issuer: env.PUBLIC_ORIGIN,
      };
      const inspected = await inspectAuthorizationRequest(env.CONTROL_DB, authorizationInput);
      if (request.method === "GET") {
        const csrfToken = readCookie(request, "oraja_csrf");
        if (!csrfToken) throw new ApiError("csrf_required", 403);
        const fields = Object.fromEntries(
          ["response_type", "client_id", "redirect_uri", "scope", "code_challenge", "code_challenge_method", "resource", "state"]
            .map((name) => [name, parameter(name)] as const)
            .filter((entry): entry is readonly [string, string] => entry[1] !== null),
        );
        return new Response(oauthConsentHtml({
          clientName: inspected.clientName,
          profileName: profile.display_name,
          scopes: inspected.scopes,
          fields,
          csrfToken,
        }), {
          status: 200,
          headers: {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "content-security-policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "x-frame-options": "DENY",
          },
        });
      }
      const sessionToken = readCookie(request, "__Host-oraja_session");
      if (!sessionToken || !(await verifyCsrf(env.CONTROL_DB, sessionToken, parameter("csrf_token"), Math.floor(Date.now() / 1000)))) {
        throw new ApiError("csrf_required", 403);
      }
      const destination = new URL(inspected.redirectUri);
      const state = parameter("state");
      if (state) destination.searchParams.set("state", state);
      destination.searchParams.set("iss", new URL(env.PUBLIC_ORIGIN).origin);
      if (parameter("decision") !== "approve") {
        destination.searchParams.set("error", "access_denied");
        await appendOAuthAudit(env.CONTROL_DB, {
          eventType: "oauth.denied", status: "denied", reasonCode: "resource_owner_denied",
          requestId: requestIdValue, accountId, profileId: profile.id, resource: inspected.resource, clientId: inspected.clientId,
        });
        return new Response(null, { status: 302, headers: { location: destination.toString(), "cache-control": "no-store" } });
      }
      const code = await issueAuthorizationCode(env.CONTROL_DB, {
        ...authorizationInput,
        requestId: requestIdValue,
        accountId,
        profileId: profile.id,
      });
      destination.searchParams.set("code", code);
      return new Response(null, { status: 302, headers: { location: destination.toString(), "cache-control": "no-store" } });
    }
    if (url.pathname === "/oauth/token" && request.method === "POST") {
      const form = await request.formData();
      const grantType = form.get("grant_type");
      auditClientId = typeof form.get("client_id") === "string" ? String(form.get("client_id")) : undefined;
      await enforceOAuthRateLimit(env.CONTROL_DB, "token", `${auditClientId ?? "missing"}:${clientIp(request)}`);
      if (grantType === "authorization_code") {
        return json(await exchangeAuthorizationCode(env.CONTROL_DB, {
          code: form.get("code"),
          clientId: form.get("client_id"),
          redirectUri: form.get("redirect_uri"),
          codeVerifier: form.get("code_verifier"),
          resource: form.get("resource"),
          issuer: env.PUBLIC_ORIGIN,
          requestId: requestIdValue,
        }), 200, origin);
      }
      if (grantType === "refresh_token") {
        return json(await rotateRefreshToken(env.CONTROL_DB, {
          refreshToken: form.get("refresh_token"),
          clientId: form.get("client_id"),
          resource: form.get("resource"),
          issuer: env.PUBLIC_ORIGIN,
          requestId: requestIdValue,
        }), 200, origin);
      }
      throw new ApiError("unsupported_grant_type", 400);
    }
    if (url.pathname === "/oauth/revoke" && request.method === "POST") {
      const form = await request.formData();
      auditClientId = typeof form.get("client_id") === "string" ? String(form.get("client_id")) : undefined;
      await enforceOAuthRateLimit(env.CONTROL_DB, "revoke", `${auditClientId ?? "missing"}:${clientIp(request)}`);
      await revokeOAuthToken(env.CONTROL_DB, { token: form.get("token"), clientId: form.get("client_id"), requestId: requestIdValue });
      return new Response(null, { status: 200, headers: { "cache-control": "no-store" } });
    }
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    if (url.pathname.startsWith("/oauth/")) {
      await appendOAuthAudit(env.CONTROL_DB, {
        eventType: `oauth.${url.pathname.slice("/oauth/".length)}_denied`,
        status: "denied",
        reasonCode: error instanceof ApiError ? error.code : "internal_error",
        requestId: requestIdValue,
        clientId: auditClientId,
      }).catch(() => undefined);
    }
    return oauthFailure(error, origin);
  }
}

async function handleAdvisorRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const match = /^\/v1\/advisor\/proposals(?:\/([^/]+)\/(approve|reject))?$/.exec(url.pathname);
  if (!match) return null;
  try {
    // Third-party OAuth clients may propose, but only the signed-in profile
    // owner may inspect or decide proposals. Mutating owner calls require CSRF.
    if (!match[1] && request.method === "POST") {
      const principal = await authenticateOAuthToken(env.CONTROL_DB, bearerTokenForOAuth(request));
      const body = await readJsonBody(request, 40 * 1024);
      return json(await createAdvisorProposal(env.CONTROL_DB, principal, { providerName: body.provider_name, title: body.title, payload: body.payload }), 201, origin);
    }
    const accountId = await requireWebAccount(request, env, Boolean(match[1]));
    const body = match[1] && request.method === "POST" ? await readJsonBody(request, 8 * 1024) : undefined;
    const profileSelector = typeof body?.profile_id === "string" ? body.profile_id : url.searchParams.get("profile_id") ?? "";
    const profile = await env.CONTROL_DB.prepare(
      "SELECT id FROM profiles WHERE account_id = ?1 AND (?2 = '' OR id = ?2 OR public_id = ?2) AND status <> 'deleted' ORDER BY created_at LIMIT 1",
    ).bind(accountId, profileSelector).first<{ id: string }>();
    if (!profile) throw new ApiError("profile_not_found", 404);
    const owner: AdvisorOwner = { accountId, profileId: profile.id, actorId: `web-owner:${accountId}` };
    if (match[1] && match[2]) {
      if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
      return json(await decideAdvisorProposal(env.CONTROL_DB, owner, decodeURIComponent(match[1]), match[2] === "approve" ? "approved" : "rejected", body?.reason), 200, origin);
    }
    if (request.method === "GET") return json({ proposals: await listAdvisorProposals(env.CONTROL_DB, owner, url.searchParams.get("status") ?? undefined) }, 200, origin);
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

async function handlePrivacyRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const downloadMatch = /^\/v1\/privacy\/exports\/([0-9a-f-]{36})\/download$/.exec(url.pathname);
  if (!["/v1/privacy/export", "/v1/privacy/delete", "/v1/privacy/delete/cancel"].includes(url.pathname) && !downloadMatch) return null;
  try {
    const accountId = await requireWebAccount(request, env, true);
    if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
    if (downloadMatch) {
      const profile = await env.CONTROL_DB.prepare(
        "SELECT id FROM profiles WHERE account_id = ?1 AND status <> 'deleted' ORDER BY created_at LIMIT 1",
      ).bind(accountId).first<{ id: string }>();
      if (!profile) throw new ApiError("profile_not_found", 404);
      const now = Math.floor(Date.now() / 1000);
      const claimed = await claimExportDownload(env.CONTROL_DB, accountId, profile.id, downloadMatch[1], now);
      const object = await env.BACKUP_BUCKET.get(claimed.targetKey);
      if (!object) {
        await env.CONTROL_DB.prepare(
          "UPDATE exports SET downloaded_at = NULL WHERE id = ?1 AND account_id = ?2 AND profile_id = ?3 AND downloaded_at = ?4",
        ).bind(downloadMatch[1], accountId, profile.id, now).run();
        throw new ApiError("export_temporarily_unavailable", 503);
      }
      return new Response(object.body, {
        status: 200,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "content-disposition": `attachment; filename="oraja-profile-export-${downloadMatch[1]}.json"`,
          "cache-control": "no-store",
        },
      });
    }
    const body = await readJsonBody(request, 8 * 1024);
    const profilePublicId = typeof body.profile_id === "string" ? body.profile_id : "";
    const profile = await env.CONTROL_DB.prepare(
      "SELECT id FROM profiles WHERE account_id = ?1 AND (id = ?2 OR public_id = ?2) AND status <> 'deleted'",
    ).bind(accountId, profilePublicId).first<{ id: string }>();
    if (!profile) throw new ApiError("profile_not_found", 404);
    if (url.pathname === "/v1/privacy/export") return json(await requestDataExport(env.CONTROL_DB, accountId, profile.id), 202, origin);
    if (url.pathname === "/v1/privacy/delete") return json(await requestDeletion(env.CONTROL_DB, accountId, profile.id), 202, origin);
    return json(await cancelDeletion(env.CONTROL_DB, accountId, profile.id), 200, origin);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

async function handleDeviceRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const listOrCreate = /^\/v1\/profiles\/([^/]+)\/devices$/.exec(url.pathname);
  const revokeAll = /^\/v1\/profiles\/([^/]+)\/devices\/revoke-all$/.exec(url.pathname);
  const mutation = /^\/v1\/devices\/([^/]+)(?:\/revoke)?$/.exec(url.pathname);
  if (!listOrCreate && !revokeAll && !mutation) return null;
  const requestIdValue = requestId(request);
  try {
    if (listOrCreate && request.method === "GET") {
      const accountId = await requireWebAccount(request, env, false);
      return json({
        devices: await listDevices(env.CONTROL_DB, accountId, decodeURIComponent(listOrCreate[1])),
      }, 200, origin);
    }
    if (listOrCreate && request.method === "POST") {
      const accountId = await requireWebAccount(request, env, true);
      const body = await readJsonBody(request, 16 * 1024);
      return json(await issueDeviceToken(
        env.CONTROL_DB,
        accountId,
        decodeURIComponent(listOrCreate[1]),
        body.label,
        requireDevicePepper(env),
        { requestId: requestIdValue },
      ), 201, origin);
    }
    if (revokeAll && request.method === "POST") {
      const accountId = await requireWebAccount(request, env, true);
      const count = await revokeAllDevices(
        env.CONTROL_DB,
        accountId,
        decodeURIComponent(revokeAll[1]),
        { requestId: requestIdValue },
      );
      return json({ revoked: count }, 200, origin);
    }
    if (mutation) {
      const accountId = await requireWebAccount(request, env, true);
      const deviceId = decodeURIComponent(mutation[1]);
      if (request.method === "PATCH" && !url.pathname.endsWith("/revoke")) {
        const body = await readJsonBody(request, 16 * 1024);
        return json(await renameDevice(env.CONTROL_DB, accountId, deviceId, body.label, {
          requestId: requestIdValue,
        }), 200, origin);
      }
      if (request.method === "DELETE" || (request.method === "POST" && url.pathname.endsWith("/revoke"))) {
        await revokeDevice(env.CONTROL_DB, accountId, deviceId, { requestId: requestIdValue });
        return json({ revoked: true }, 200, origin);
      }
    }
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    return irErrorResponse(error, requestIdValue, origin);
  }
}

function publicPlayAck(ack: PlayAck): Omit<PlayAck, "enqueue_required"> {
  return {
    status: ack.status,
    event_id: ack.event_id,
    idempotency_key: ack.idempotency_key,
    job_id: ack.job_id,
    revision: ack.revision,
    retryable: false,
  };
}

async function enqueuePlay(
  env: Env,
  identity: DeviceIdentity,
  event: { event_id: string },
  ack: PlayAck,
  digest: string,
): Promise<void> {
  await env.JOB_QUEUE.send({
    type: "play.accepted.v1",
    job_id: ack.job_id,
    event_id: event.event_id,
    profile_id: identity.profileId,
    revision: ack.revision,
    input_digest: digest,
  });
}

async function markPlayEnqueued(stub: DurableObjectStub, eventId: string): Promise<void> {
  const result = await stub.fetch(
    `https://profile.internal/internal/play-events/${encodeURIComponent(eventId)}/enqueued`,
    { method: "POST" },
  );
  if (!result.ok) throw new ApiError("temporary_unavailable", 503, true, 5);
}

async function handlePlayRoute(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname !== "/v1/plays") return null;
  const requestIdValue = requestId(request);
  try {
    if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
    const pepper = requireDevicePepper(env);
    const now = Math.floor(Date.now() / 1000);
    await enforceIrRateLimit(env.CONTROL_DB, "ip", clientIp(request), pepper, now);
    const identity = await authenticateDeviceToken(env.CONTROL_DB, bearerToken(request), pepper, { now });
    await enforceIrRateLimit(env.CONTROL_DB, "token", identity.tokenHash, pepper, now);
    const event = validateIrEvent(await readJsonBody(request), identity, now);
    const digest = await eventDigest(event, identity);
    const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(identity.profileId));
    const durableResponse = await stub.fetch("https://profile.internal/internal/play-events", {
      method: "POST",
      headers: { "content-type": "application/json", "x-request-id": requestIdValue },
      body: JSON.stringify({
        profile_internal_id: identity.profileId,
        device_internal_id: identity.deviceId,
        payload_digest: digest,
        request_id: requestIdValue,
        event,
      }),
    });
    if (durableResponse.status === 409) {
      return json({ error: { code: "idempotency_conflict" } }, 409, origin);
    }
    if (!durableResponse.ok) throw new ApiError("temporary_unavailable", 503, true, 5);
    const ack = await durableResponse.json() as PlayAck;
    if (
      (ack.status !== "accepted" && ack.status !== "duplicate") ||
      ack.event_id !== event.event_id ||
      ack.idempotency_key !== event.event_id
    ) throw new ApiError("temporary_unavailable", 503, true, 5);
    if (ack.enqueue_required) {
      await enqueuePlay(env, identity, event, ack, digest);
      await markPlayEnqueued(stub, event.event_id);
    }
    return json(publicPlayAck(ack), ack.status === "accepted" ? 202 : 200, origin);
  } catch (error) {
    return irErrorResponse(error, requestIdValue, origin);
  }
}

async function handleIrReadRoute(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (!IR_READ_PATHS.has(url.pathname)) return null;
  const requestIdValue = requestId(request);
  try {
    if (request.method !== "GET") throw new ApiError("method_not_allowed", 405);
    const pepper = requireDevicePepper(env);
    const now = Math.floor(Date.now() / 1000);
    await enforceIrRateLimit(env.CONTROL_DB, "ip", clientIp(request), pepper, now);
    const identity = await authenticateDeviceToken(env.CONTROL_DB, bearerToken(request), pepper, { now });
    await enforceIrRateLimit(env.CONTROL_DB, "token", identity.tokenHash, pepper, now);
    return json(await readIrMethod(url, identity, {
      db: env.CONTROL_DB,
      profileDo: env.PROFILE_DO,
      buildVersion: env.BUILD_VERSION,
    }), 200, origin);
  } catch (error) {
    return irErrorResponse(error, requestIdValue, origin);
  }
}

async function handleUploadRoute(request: Request, env: Env): Promise<Response | null> {
  if (!new URL(request.url).pathname.startsWith("/v1/uploads")) return null;
  if (!env.ENVELOPE_MASTER_KEY) return json({ error: { code: "upload_not_configured" } }, 503);
  const service = new UploadService(
    env.RAW_BUCKET,
    new D1UploadSessionStore(env.CONTROL_DB),
    EnvelopeCrypto.fromSecret(env.ENVELOPE_MASTER_KEY),
  );
  const requestIdValue = requestId(request);
  return handleUploadRequest(request, service, async (candidate) => {
    const accountId = await requireWebAccount(candidate, env, candidate.method !== "GET");
    const profile = await env.CONTROL_DB
      .prepare(
        "SELECT id FROM profiles WHERE account_id = ?1 AND status = 'active' ORDER BY created_at LIMIT 1",
      )
      .bind(accountId)
      .first<{ id: string }>();
    return profile ? { profileId: profile.id, accountId } : null;
  }, async (context, completed) => {
    if (!context.accountId) throw new ApiError("unauthorized", 401);
    const jobKind = completed.submissionKind === "monthly" ? "monthly"
      : completed.submissionKind === "audit" ? "regenerate" : "initial";
    const accepted = await new JobDispatcher(
      new D1JobLedger(env.CONTROL_DB),
      env.JOB_QUEUE,
    ).acceptAndEnqueue({
      accountId: context.accountId,
      profileId: context.profileId,
      jobKind,
      eventId: `upload:${completed.uploadId}:${completed.manifestSha256}`,
      requestId: requestIdValue,
      correlationId: completed.uploadId,
      inputDigest: completed.manifestSha256,
      inputKey: `upload-session:${completed.uploadId}`,
    });
    return { job_id: accepted.job.jobId, job_status: accepted.status };
  });
}

async function handleJobRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const match = /^\/v1\/jobs\/([^/]+)(?:\/(cancel|retry))?$/.exec(new URL(request.url).pathname);
  if (!match) return null;
  try {
    const mutate = Boolean(match[2]);
    const accountId = await requireWebAccount(request, env, mutate);
    const jobId = decodeURIComponent(match[1]);
    const ledger = new D1JobLedger(env.CONTROL_DB);
    if (!match[2] && request.method === "GET") {
      const status = await ledger.getStatus(accountId, jobId);
      if (!status) throw new JobLedgerError("job_not_found", 404, false);
      return json(status, 200, origin);
    }
    if (match[2] === "cancel" && request.method === "POST") {
      return json({ status: await ledger.cancel(accountId, jobId) }, 202, origin);
    }
    if (match[2] === "retry" && request.method === "POST") {
      const job = await ledger.manualRetry(accountId, jobId, accountId);
      await new JobDispatcher(ledger, env.JOB_QUEUE).dispatch(job);
      return json({ status: "queued", job_id: job.jobId, workflow_run: job.workflowRun }, 202, origin);
    }
    throw new JobLedgerError("method_not_allowed", 405, false);
  } catch (error) {
    if (error instanceof JobLedgerError) {
      const headers = error.retryable ? { "retry-after": "5" } : undefined;
      return json({ error: { code: error.code } }, error.status, origin, headers);
    }
    return apiFailure(error, origin);
  }
}

async function handleProfileSettingsRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const capabilityMatch = /^\/v1\/profile\/capabilities\/(recommend|today)\/(rotate|revoke)$/.exec(url.pathname);
  const settingsRoute = url.pathname === "/v1/profile/settings";
  const capabilitiesRoute = url.pathname === "/v1/profile/capabilities";
  if (!settingsRoute && !capabilitiesRoute && !capabilityMatch) return null;
  try {
    const mutation = request.method !== "GET";
    const accountId = await requireWebAccount(request, env, mutation);
    if (settingsRoute && request.method === "GET") {
      return json({ settings: await readProfileSettings(env.CONTROL_DB, accountId) }, 200, origin);
    }
    if (settingsRoute && request.method === "PATCH") {
      const body = await readJsonBody(request, 8 * 1024);
      return json({ settings: await updateProfileSettings(env.CONTROL_DB, accountId, body) }, 200, origin);
    }
    if (capabilitiesRoute && request.method === "GET") {
      return json({ capabilities: await listTableCapabilities(env.CONTROL_DB, accountId) }, 200, origin);
    }
    if (capabilityMatch && request.method === "POST") {
      const result = capabilityMatch[2] === "rotate"
        ? await rotateTableCapability(env.CONTROL_DB, accountId, capabilityMatch[1], env.PUBLIC_ORIGIN)
        : await revokeTableCapability(env.CONTROL_DB, accountId, capabilityMatch[1]);
      return json(result, capabilityMatch[2] === "rotate" ? 201 : 200, origin);
    }
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

export class PythonProcessor extends Container {
  defaultPort = 8080;
  sleepAfter = "10m";
  enableInternet = false;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const suppliedOrigin = request.headers.get("Origin");
    const origin = allowedOrigin(request, env);
    if (suppliedOrigin && !origin) return json({ error: { code: "origin_not_allowed" } }, 403);
    if (request.method === "OPTIONS") {
      if (!origin) return json({ error: { code: "origin_required" } }, 403);
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": origin,
          "access-control-allow-credentials": "true",
          "access-control-allow-headers": "content-type,authorization,x-csrf-token,x-request-id,mcp-protocol-version,mcp-session-id",
          "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
          "access-control-expose-headers": "mcp-session-id",
          "access-control-max-age": "600",
          "vary": "Origin",
        },
      });
    }
    if (url.pathname === "/healthz" && request.method === "GET") {
      return json({ status: "ok", environment: env.ENVIRONMENT });
    }
    if (url.pathname === "/version" && request.method === "GET") {
      return json({ service: "oraja-training", environment: env.ENVIRONMENT, version: env.BUILD_VERSION });
    }
    const oauthResponse = await handleOAuthRoutes(request, env, origin);
    if (oauthResponse) return oauthResponse;
    if (url.pathname === "/mcp") {
      const response = await handleMcp(request, env);
      if (!origin) return response;
      const headers = new Headers(response.headers);
      headers.set("access-control-allow-origin", origin);
      headers.set("access-control-allow-credentials", "true");
      headers.set("access-control-expose-headers", "mcp-session-id");
      headers.append("vary", "Origin");
      return new Response(response.body, { status: response.status, headers });
    }
    const authResponse = await handleAuth(request, env, origin);
    if (authResponse) return authResponse;
    if (url.pathname === "/v1/dashboard") {
      try {
        return json(await dashboardData(request, env), 200, origin);
      } catch (error) {
        return apiFailure(error, origin);
      }
    }
    const deviceResponse = await handleDeviceRoutes(request, env, origin);
    if (deviceResponse) return deviceResponse;
    const playResponse = await handlePlayRoute(request, env, origin);
    if (playResponse) return playResponse;
    const irReadResponse = await handleIrReadRoute(request, env, origin);
    if (irReadResponse) return irReadResponse;
    const uploadResponse = await handleUploadRoute(request, env);
    if (uploadResponse) return uploadResponse;
    const settingsResponse = await handleProfileSettingsRoutes(request, env, origin);
    if (settingsResponse) return settingsResponse;
    const jobResponse = await handleJobRoutes(request, env, origin);
    if (jobResponse) return jobResponse;
    const tableResponse = await handleCapabilityTable(request, env);
    if (tableResponse) return tableResponse;
    const advisorResponse = await handleAdvisorRoutes(request, env, origin);
    if (advisorResponse) return advisorResponse;
    const privacyResponse = await handlePrivacyRoutes(request, env, origin);
    if (privacyResponse) return privacyResponse;
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: { code: "not_found" } }, 404);
  },
  async queue(batch: MessageBatch<JobEnvelope>, env: Env): Promise<void> {
    const ledger = new D1JobLedger(env.CONTROL_DB);
    for (const message of batch.messages) {
      await consumeQueueMessage(message, {
        ledger,
        startWorkflow: async (job) => {
          await env.GENERATE_WORKFLOW.create({
            id: `job:${job.jobId}:run:${job.workflowRun}`,
            params: job,
          });
        },
      });
    }
  },
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    const schedule = (Object.entries(SCHEDULE_CRONS).find(([, cron]) => cron === controller.cron)?.[0]
      ?? null) as ScheduleName | null;
    if (!schedule) return;
    const scheduledAt = Math.floor(controller.scheduledTime / 1000);
    if (schedule === "deletion-sweep") {
      const due = await purgeDueProfiles(env.CONTROL_DB, scheduledAt);
      await expireDataExports(env.CONTROL_DB, scheduledAt);
      const tasks = await claimPrivacyTasks(env.CONTROL_DB, scheduledAt, 100);
      for (const task of tasks) {
        try {
          await runPurgeTask(task, env);
          await recordPrivacyTaskResult(env.CONTROL_DB, task.id, true, null, scheduledAt);
        } catch (error) {
          const code = error instanceof ApiError ? error.code : "privacy_task_failed";
          await recordPrivacyTaskResult(env.CONTROL_DB, task.id, false, code, scheduledAt);
        }
      }
      for (const deletionId of due.deletionIds) await finalizeDeletion(env.CONTROL_DB, deletionId, scheduledAt);
      return;
    }
    if (schedule === "daily-backup") await processExportSnapshots(env, scheduledAt);
    const ledger = new D1JobLedger(env.CONTROL_DB);
    await dispatchSchedule(
      ledger,
      new JobDispatcher(ledger, env.JOB_QUEUE),
      schedule,
      scheduledAt,
    );
  },
};
