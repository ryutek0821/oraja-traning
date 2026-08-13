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
  requestEmailChange,
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
          "access-control-allow-headers": "content-type,authorization,x-csrf-token,x-request-id",
          "access-control-allow-methods": "GET,POST,PATCH,DELETE,OPTIONS",
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
    const authResponse = await handleAuth(request, env, origin);
    if (authResponse) return authResponse;
    const deviceResponse = await handleDeviceRoutes(request, env, origin);
    if (deviceResponse) return deviceResponse;
    const playResponse = await handlePlayRoute(request, env, origin);
    if (playResponse) return playResponse;
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: { code: "not_found" } }, 404);
  },
};
