import { Container } from "@cloudflare/containers";
import { WorkflowEntrypoint } from "cloudflare:workers";
import { CloudflareAuthEmailSender } from "./email";
import {
  AuthError,
  completePasswordReset,
  confirmEmail,
  csrfCookie,
  expiredCsrfCookie,
  expiredSessionCookie,
  loginAccount,
  logoutSession,
  readCookie,
  registerAccount,
  requestPasswordReset,
  rotateSession,
  sessionCookie,
  useRecoveryCode,
  verifyCsrf,
} from "./auth";
import {
  ApiError,
  authErrorResponse as irErrorResponse,
  authenticateDeviceToken,
  bearerToken,
  enforceIrRateLimit,
  eventDigest,
  issueDeviceToken,
  listDevices,
  readJsonBody,
  renameDevice,
  revokeAllDevices,
  revokeDevice,
  sessionAccount,
  validateIrEvent,
  type DeviceIdentity,
  type PlayAck,
} from "./ir-api";
import { ProfileDurableObject } from "./profile-do";
import { createAdvisorProposal, decideAdvisorProposal, listAdvisorProposals } from "./advisor";
import { handleMcp } from "./mcp";
import {
  authenticateOAuthToken,
  exchangeAuthorizationCode,
  issueAuthorizationCode,
  oauthDiscovery,
  registerClient,
} from "./oauth";
import { cancelDeletion, requestDataExport, requestDeletion } from "./privacy";

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
  ASSETS: Fetcher;
  AUTH_EMAIL?: SendEmail;
  AUTH_EMAIL_FROM?: string;
  EMAIL_ENCRYPTION_KEY?: string;
  DEVICE_TOKEN_PEPPER?: string;
}

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

function json(
  value: unknown,
  status = 200,
  origin?: string,
  extraHeaders?: HeadersInit,
): Response {
  const headers = new Headers(JSON_HEADERS);
  if (origin) {
    headers.set("access-control-allow-origin", origin);
    headers.set("access-control-allow-credentials", "true");
  }
  if (extraHeaders) {
    for (const [name, value] of new Headers(extraHeaders)) headers.append(name, value);
  }
  return new Response(JSON.stringify(value) + "\n", { status, headers });
}

function allowedOrigin(request: Request, env: Env): string | undefined {
  const origin = request.headers.get("Origin");
  if (!origin) return undefined;
  const allowed = env.CORS_ORIGINS.split(",").map((item) => item.trim());
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
  const contentType = request.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) {
    const body = await request.json();
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new AuthError("invalid_request", 400);
    return body as Record<string, unknown>;
  }
  const form = await request.formData();
  return Object.fromEntries(form.entries());
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
  };
}

function sessionHeaders(result: { sessionToken: string; expiresAt: number }): HeadersInit {
  return {
    "set-cookie": sessionCookie(result.sessionToken),
    "x-session-expires-at": String(result.expiresAt),
  };
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
  if (request.method === "OPTIONS") return null;
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
      const headers = new Headers(sessionHeaders(result));
      headers.append("set-cookie", csrfCookie(result.csrfToken));
      return json({ account_public_id: result.accountPublicId }, 200, origin, headers);
    }

    if (url.pathname === "/v1/auth/recovery" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await useRecoveryCode(env.CONTROL_DB, body.user_id, body.code, options);
      const headers = new Headers(sessionHeaders(result));
      headers.append("set-cookie", csrfCookie(result.csrfToken));
      return json({ account_public_id: result.accountPublicId }, 200, origin, headers);
    }

    if (url.pathname === "/v1/auth/password-reset/request" && request.method === "POST") {
      const body = await requestPayload(request);
      await requestPasswordReset(env.CONTROL_DB, body.email, options);
      return json({ accepted: true }, 202, origin);
    }

    if (url.pathname === "/v1/auth/password-reset/complete" && request.method === "POST") {
      const body = await requestPayload(request);
      const result = await completePasswordReset(env.CONTROL_DB, body.token, body.password, options);
      const headers = new Headers(sessionHeaders(result));
      headers.append("set-cookie", csrfCookie(result.csrfToken));
      return json({ account_public_id: result.accountPublicId }, 200, origin, headers);
    }

    if (url.pathname === "/v1/auth/verify-email" && (request.method === "GET" || request.method === "POST")) {
      const token = request.method === "GET" ? url.searchParams.get("token") : (await requestPayload(request)).token;
      await confirmEmail(env.CONTROL_DB, token, options);
      return json({ confirmed: true }, 200, origin);
    }

    const sessionToken = readCookie(request, "__Host-oraja_session");
    if (!sessionToken) throw new AuthError("unauthorized", 401);
    const csrf = request.headers.get("X-CSRF-Token") || readCookie(request, "oraja_csrf");
    if (!(await verifyCsrf(env.CONTROL_DB, sessionToken, csrf, options.now))) {
      throw new AuthError("csrf_required", 403);
    }

    if (url.pathname === "/v1/auth/logout" && request.method === "POST") {
      await logoutSession(env.CONTROL_DB, sessionToken, options);
      return new Response(null, {
        status: 204,
        headers: (() => {
          const headers = new Headers();
          if (origin) {
            headers.set("access-control-allow-origin", origin);
            headers.set("access-control-allow-credentials", "true");
          }
          headers.append("set-cookie", expiredSessionCookie());
          headers.append("set-cookie", expiredCsrfCookie());
          return headers;
        })(),
      });
    }

    if (url.pathname === "/v1/auth/session/rotate" && request.method === "POST") {
      const result = await rotateSession(env.CONTROL_DB, sessionToken, options);
      const headers = new Headers(sessionHeaders(result));
      headers.append("set-cookie", csrfCookie(result.csrfToken));
      return json({ rotated: true }, 200, origin, headers);
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

async function requireWebAccount(
  request: Request,
  env: Env,
  mutate: boolean,
): Promise<string> {
  const sessionToken = readCookie(request, "__Host-oraja_session");
  if (!sessionToken) throw new ApiError("unauthorized", 401);
  if (mutate) {
    const csrf = request.headers.get("X-CSRF-Token") || readCookie(request, "oraja_csrf");
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
  if (error instanceof ApiError) return json({ error: { code: error.code } }, error.status, origin);
  return json({ error: { code: "internal_error" } }, 500, origin);
}

async function handleOAuthRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (
    url.pathname !== "/oauth/register" &&
    url.pathname !== "/oauth/authorize" &&
    url.pathname !== "/oauth/token" &&
    url.pathname !== "/.well-known/oauth-authorization-server" &&
    url.pathname !== "/.well-known/openid-configuration"
  ) return null;
  try {
    if ((url.pathname === "/.well-known/oauth-authorization-server" || url.pathname === "/.well-known/openid-configuration") && request.method === "GET") {
      return json(oauthDiscovery(env.PUBLIC_ORIGIN), 200, origin);
    }
    if (url.pathname === "/oauth/register" && request.method === "POST") {
      const body = await readJsonBody(request, 32 * 1024);
      return json(await registerClient(env.CONTROL_DB, {
        clientName: body.client_name,
        redirectUris: body.redirect_uris,
      }), 201, origin);
    }
    if (url.pathname === "/oauth/authorize" && request.method === "GET") {
      const accountId = await requireWebAccount(request, env, false);
      const clientId = url.searchParams.get("client_id");
      const redirectUri = url.searchParams.get("redirect_uri");
      const scope = url.searchParams.get("scope");
      const codeChallenge = url.searchParams.get("code_challenge");
      const profile = await env.CONTROL_DB.prepare("SELECT id FROM profiles WHERE account_id = ?1 AND status = 'active' ORDER BY created_at LIMIT 1").bind(accountId).first<{ id: string }>();
      if (!profile) throw new ApiError("profile_not_found", 404);
      const code = await issueAuthorizationCode(env.CONTROL_DB, {
        clientId,
        redirectUri,
        scope,
        codeChallenge,
        accountId,
        profileId: profile.id,
      });
      const destination = new URL(redirectUri ?? "https://invalid.example.invalid");
      destination.searchParams.set("code", code);
      const state = url.searchParams.get("state");
      if (state) destination.searchParams.set("state", state);
      return new Response(null, { status: 302, headers: { location: destination.toString(), "cache-control": "no-store" } });
    }
    if (url.pathname === "/oauth/token" && request.method === "POST") {
      const form = await request.formData();
      if (form.get("grant_type") !== "authorization_code") throw new ApiError("unsupported_grant_type", 400);
      return json(await exchangeAuthorizationCode(env.CONTROL_DB, {
        code: form.get("code"),
        clientId: form.get("client_id"),
        redirectUri: form.get("redirect_uri"),
        codeVerifier: form.get("code_verifier"),
      }), 200, origin);
    }
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

async function handleAdvisorRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const proposalMatch = /^\/v1\/advisor\/proposals(?:\/([^/]+)\/(approve|reject))?$/.exec(url.pathname);
  if (!proposalMatch) return null;
  try {
    const principal = await authenticateOAuthToken(env.CONTROL_DB, bearerTokenForOAuth(request));
    if (proposalMatch[1] && proposalMatch[2]) {
      if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
      const body = await readJsonBody(request, 8 * 1024);
      return json(await decideAdvisorProposal(env.CONTROL_DB, principal, decodeURIComponent(proposalMatch[1]), proposalMatch[2] === "approve" ? "approved" : "rejected", body.reason), 200, origin);
    }
    if (request.method === "GET") return json({ proposals: await listAdvisorProposals(env.CONTROL_DB, principal, url.searchParams.get("status") ?? undefined) }, 200, origin);
    if (request.method === "POST") {
      const body = await readJsonBody(request, 40 * 1024);
      return json(await createAdvisorProposal(env.CONTROL_DB, principal, { providerName: body.provider_name, title: body.title, payload: body.payload }), 201, origin);
    }
    throw new ApiError("method_not_allowed", 405);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

async function profileInternalId(request: Request, env: Env): Promise<{ accountId: string; profileId: string }> {
  const accountId = await requireWebAccount(request, env, true);
  const body = await readJsonBody(request, 8 * 1024);
  const profilePublicId = typeof body.profile_id === "string" ? body.profile_id : "";
  const profile = await env.CONTROL_DB.prepare("SELECT id FROM profiles WHERE account_id = ?1 AND (id = ?2 OR public_id = ?2) AND status <> 'deleted'").bind(accountId, profilePublicId).first<{ id: string }>();
  if (!profile) throw new ApiError("profile_not_found", 404);
  return { accountId, profileId: profile.id };
}

async function handlePrivacyRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (!["/v1/privacy/export", "/v1/privacy/delete", "/v1/privacy/delete/cancel"].includes(url.pathname)) return null;
  try {
    const owner = await profileInternalId(request, env);
    if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
    if (url.pathname === "/v1/privacy/export") return json(await requestDataExport(env.CONTROL_DB, owner.accountId, owner.profileId), 202, origin);
    if (url.pathname === "/v1/privacy/delete") return json(await requestDeletion(env.CONTROL_DB, owner.accountId, owner.profileId), 202, origin);
    return json(await cancelDeletion(env.CONTROL_DB, owner.accountId, owner.profileId), 200, origin);
  } catch (error) {
    return apiFailure(error, origin);
  }
}

async function handleDeviceRoutes(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  const listOrCreate = /^\/v1\/profiles\/([^/]+)\/devices$/.exec(url.pathname);
  const revokeAll = /^\/v1\/profiles\/([^/]+)\/devices\/revoke-all$/.exec(url.pathname);
  const deviceMutation = /^\/v1\/devices\/([^/]+)(?:\/revoke)?$/.exec(url.pathname);
  const deviceId = deviceMutation?.[1];
  const isRevokeAction = deviceMutation && url.pathname.endsWith("/revoke");
  if (!listOrCreate && !revokeAll && !deviceMutation) return null;

  const requestIdValue = requestId(request);
  try {
    if (listOrCreate && request.method === "GET") {
      const accountId = await requireWebAccount(request, env, false);
      const devices = await listDevices(env.CONTROL_DB, accountId, decodeURIComponent(listOrCreate[1]));
      return json({ devices }, 200, origin);
    }
    if (listOrCreate && request.method === "POST") {
      const accountId = await requireWebAccount(request, env, true);
      const body = await readJsonBody(request, 16 * 1024);
      const result = await issueDeviceToken(
        env.CONTROL_DB,
        accountId,
        decodeURIComponent(listOrCreate[1]),
        body.label,
        requireDevicePepper(env),
        { requestId: requestIdValue },
      );
      return json(result, 201, origin);
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
    if (deviceMutation && deviceId && request.method === "PATCH" && !isRevokeAction) {
      const accountId = await requireWebAccount(request, env, true);
      const body = await readJsonBody(request, 16 * 1024);
      const device = await renameDevice(
        env.CONTROL_DB,
        accountId,
        decodeURIComponent(deviceId),
        body.label,
        { requestId: requestIdValue },
      );
      return json(device, 200, origin);
    }
    if (deviceMutation && deviceId && (request.method === "DELETE" || (request.method === "POST" && isRevokeAction))) {
      const accountId = await requireWebAccount(request, env, true);
      await revokeDevice(env.CONTROL_DB, accountId, decodeURIComponent(deviceId), { requestId: requestIdValue });
      return json({ revoked: true }, 200, origin);
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

async function markPlayEnqueued(
  stub: DurableObjectStub,
  eventId: string,
): Promise<void> {
  const marked = await stub.fetch(`https://profile.internal/internal/play-events/${encodeURIComponent(eventId)}/enqueued`, {
    method: "POST",
  });
  if (!marked.ok) throw new ApiError("temporary_unavailable", 503, true, 5);
}

async function enqueuePlay(
  env: Env,
  identity: DeviceIdentity,
  event: { event_id: string },
  ack: PlayAck,
  digest: string,
): Promise<void> {
  if (env.JOB_QUEUE) {
    await env.JOB_QUEUE.send({
      type: "play.accepted.v1",
      job_id: ack.job_id,
      event_id: event.event_id,
      profile_id: identity.profileId,
      revision: ack.revision,
      input_digest: digest,
    });
  }
}

async function handlePlayRoute(request: Request, env: Env, origin?: string): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname !== "/v1/plays") return null;
  const requestIdValue = requestId(request);
  try {
    if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
    const pepper = requireDevicePepper(env);
    const now = Math.floor(Date.now() / 1000);
    const ip = clientIp(request);
    await enforceIrRateLimit(env.CONTROL_DB, "ip", ip, pepper, now);
    const identity = await authenticateDeviceToken(env.CONTROL_DB, bearerToken(request), pepper, { now });
    await enforceIrRateLimit(env.CONTROL_DB, "token", identity.tokenHash, pepper, now);
    const payload = await readJsonBody(request);
    const event = validateIrEvent(payload, identity, now);
    const digest = await eventDigest(event, identity);
    if (!env.PROFILE_DO) throw new ApiError("temporary_unavailable", 503, true, 5);
    const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(identity.profileId));
    const durableResponse = await stub.fetch("https://profile.internal/internal/play-events", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-request-id": requestIdValue,
      },
      body: JSON.stringify({
        profile_internal_id: identity.profileId,
        device_internal_id: identity.deviceId,
        payload_digest: digest,
        request_id: requestIdValue,
        event,
      }),
    });
    if (durableResponse.status === 409) return json({ error: { code: "idempotency_conflict" } }, 409, origin);
    if (!durableResponse.ok) throw new ApiError("temporary_unavailable", 503, true, 5);
    let ack: PlayAck;
    try {
      ack = await durableResponse.json() as PlayAck;
    } catch {
      throw new ApiError("temporary_unavailable", 503, true, 5);
    }
    if (
      (ack.status !== "accepted" && ack.status !== "duplicate") ||
      ack.event_id !== event.event_id ||
      ack.idempotency_key !== event.event_id
    ) {
      throw new ApiError("temporary_unavailable", 503, true, 5);
    }
    if (ack.enqueue_required) {
      await enqueuePlay(env, identity, event, ack, digest);
      await markPlayEnqueued(stub, event.event_id);
    }
    return json(publicPlayAck(ack), ack.status === "accepted" ? 202 : 200, origin);
  } catch (error) {
    return irErrorResponse(error, requestIdValue, origin);
  }
}

export class PythonProcessor extends Container {
  defaultPort = 8080;
  sleepAfter = "10m";
  enableInternet = false;
}

export class GenerateWorkflow extends WorkflowEntrypoint<Env, { job_id: string }> {
  async run(
    event: { payload: { job_id: string } },
    step: { do<T>(name: string, callback: () => Promise<T>): Promise<T> },
  ): Promise<{ status: string; job_id: string }> {
    return step.do("foundation-health-check", async () => ({
      status: "foundation-ready",
      job_id: event.payload.job_id,
    }));
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const origin = allowedOrigin(request, env);

    if (url.pathname === "/healthz" && request.method === "GET") {
      return json({ status: "ok", environment: env.ENVIRONMENT }, 200, origin);
    }
    if (url.pathname === "/version" && request.method === "GET") {
      return json(
        {
          service: "oraja-training",
          environment: env.ENVIRONMENT,
          version: env.BUILD_VERSION,
        },
        200,
        origin,
      );
    }

    if (request.method === "OPTIONS" && origin) {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": origin,
          "access-control-allow-credentials": "true",
          "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
          "access-control-allow-headers": "content-type,authorization,x-csrf-token,x-request-id",
          "access-control-max-age": "600",
        },
      });
    }

    const oauthResponse = await handleOAuthRoutes(request, env, origin);
    if (oauthResponse) return oauthResponse;

    if (url.pathname === "/mcp") return handleMcp(request, env);

    const authResponse = await handleAuth(request, env, origin);
    if (authResponse) return authResponse;

    const deviceResponse = await handleDeviceRoutes(request, env, origin);
    if (deviceResponse) return deviceResponse;

    const playResponse = await handlePlayRoute(request, env, origin);
    if (playResponse) return playResponse;

    const advisorResponse = await handleAdvisorRoutes(request, env, origin);
    if (advisorResponse) return advisorResponse;

    const privacyResponse = await handlePrivacyRoutes(request, env, origin);
    if (privacyResponse) return privacyResponse;

    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: { code: "not_found" } }, 404, origin);
  },
};
