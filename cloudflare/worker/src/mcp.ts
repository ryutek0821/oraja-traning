import { createAdvisorProposal } from "./advisor";
import { ApiError } from "./ir-api";
import { authenticateOAuthToken, hasOAuthScope, type OAuthPrincipal, type OAuthScope } from "./oauth";
import type { Env } from "./index";

const MCP_PROTOCOL_VERSION = "2026-07-28";
const MAX_RESPONSE_BYTES = 256 * 1024;

type JsonRpcRequest = { jsonrpc?: unknown; id?: unknown; method?: unknown; params?: unknown };
type RpcTool = { name: string; description: string; inputSchema: Record<string, unknown> };

function rpc(id: unknown, result: unknown): Response {
  const body = JSON.stringify({ jsonrpc: "2.0", id: id ?? null, result }) + "\n";
  if (body.length > MAX_RESPONSE_BYTES) return rpcError(id, -32002, "response_too_large", 413);
  return new Response(body, { status: 200, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

function rpcError(id: unknown, code: number, message: string, status = 400): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: id ?? null, error: { code, message } }) + "\n", {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function requestScope(principal: OAuthPrincipal, scope: OAuthScope): void {
  if (!hasOAuthScope(principal, scope)) throw new ApiError("insufficient_scope", 403);
}

async function readRequest(request: Request): Promise<JsonRpcRequest> {
  if (!(request.headers.get("content-type") ?? "").includes("application/json")) throw new ApiError("unsupported_media_type", 415);
  const value: unknown = await request.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_jsonrpc", 400);
  return value as JsonRpcRequest;
}

function bearer(request: Request): string | null {
  return /^Bearer\s+([^\s]+)$/i.exec(request.headers.get("authorization") ?? "")?.[1] ?? null;
}

function params(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function integer(value: unknown, fallback: number, allowed: readonly number[]): number {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(parsed) || !allowed.includes(parsed)) throw new ApiError("invalid_arguments", 400);
  return parsed;
}

function textResult(value: unknown): { content: { type: "text"; text: string }[] } {
  return { content: [{ type: "text", text: JSON.stringify(value) }] };
}

async function profileDoJson(env: Env, principal: OAuthPrincipal, path: string): Promise<Record<string, unknown>> {
  const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(principal.profileId));
  const response = await stub.fetch(new Request(`https://profile.internal${path}`));
  const value = await response.json<Record<string, unknown>>();
  if (!response.ok) throw new ApiError(typeof (value.error as Record<string, unknown> | undefined)?.code === "string" ? String((value.error as Record<string, unknown>).code) : "profile_read_failed", response.status);
  return value;
}

function base64url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(normalized);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function cursorSignature(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return base64url(new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload))));
}

function cursorSecret(env: Env): string {
  const secret = env.AUTH_HASH_PEPPER ?? env.DEVICE_TOKEN_PEPPER;
  if (!secret) throw new ApiError("cursor_signing_unavailable", 503);
  return secret;
}

async function signCursor(env: Env, principal: OAuthPrincipal, cursor: string): Promise<string> {
  const payload = base64url(new TextEncoder().encode(JSON.stringify({ cursor, profile: principal.profileId })));
  return `${payload}.${await cursorSignature(cursorSecret(env), payload)}`;
}

async function verifyCursor(env: Env, principal: OAuthPrincipal, token: unknown): Promise<string | undefined> {
  if (token === undefined) return undefined;
  if (typeof token !== "string" || token.length > 1024) throw new ApiError("invalid_cursor", 400);
  const [payload, signature, extra] = token.split(".");
  if (!payload || !signature || extra !== undefined || await cursorSignature(cursorSecret(env), payload) !== signature) throw new ApiError("invalid_cursor", 400);
  try {
    const decoded = JSON.parse(new TextDecoder().decode(decodeBase64url(payload))) as Record<string, unknown>;
    if (decoded.profile !== principal.profileId || typeof decoded.cursor !== "string") throw new Error("owner");
    return decoded.cursor;
  } catch {
    throw new ApiError("invalid_cursor", 400);
  }
}

function toolsFor(principal: OAuthPrincipal): RpcTool[] {
  const tools: RpcTool[] = [];
  if (hasOAuthScope(principal, "training:read")) {
    tools.push(
      { name: "training_summary", description: "Read a bounded 7, 30, or 90 day summary", inputSchema: { type: "object", properties: { days: { enum: [7, 30, 90] } }, additionalProperties: false } },
      { name: "trend_compare", description: "Compare two bounded rolling windows", inputSchema: { type: "object", properties: { recent_days: { enum: [7, 30, 90] }, baseline_days: { enum: [7, 30, 90] } }, additionalProperties: false } },
      { name: "missing_data", description: "Read bounded data-quality metadata", inputSchema: { type: "object", properties: { days: { enum: [7, 30, 90] } }, additionalProperties: false } },
    );
  }
  if (hasOAuthScope(principal, "plays:read")) tools.push({ name: "play_history", description: "Read profile-owned play history with signed cursor paging", inputSchema: { type: "object", properties: { limit: { type: "integer", minimum: 1, maximum: 100 }, cursor: { type: "string", maxLength: 1024 } }, additionalProperties: false } });
  if (hasOAuthScope(principal, "recommendations:read")) tools.push({ name: "recommendation_reason", description: "Read recommendation metadata without storage keys", inputSchema: { type: "object", properties: {}, additionalProperties: false } });
  if (hasOAuthScope(principal, "advisor:read")) tools.push({ name: "advisor_context_export", description: "Export bounded summaries for advisor context", inputSchema: { type: "object", properties: { days: { enum: [7, 30, 90] } }, additionalProperties: false } });
  if (hasOAuthScope(principal, "advisor:propose")) tools.push({ name: "advisor_propose", description: "Create a pending advisor proposal", inputSchema: { type: "object", required: ["provider_name", "title", "payload"], properties: { provider_name: { type: "string", maxLength: 120 }, title: { type: "string", maxLength: 240 }, payload: { type: "object" } }, additionalProperties: false } });
  return tools;
}

export async function handleMcp(request: Request, env: Env): Promise<Response> {
  let rpcId: unknown = null;
  if (request.method !== "POST" && request.method !== "GET") return rpcError(null, -32600, "method_not_allowed", 405);
  const origin = request.headers.get("Origin");
  if (origin && !env.CORS_ORIGINS.split(",").map((item) => item.trim()).includes(origin)) return rpcError(null, -32001, "origin_not_allowed", 403);
  try {
    const principal = await authenticateOAuthToken(env.CONTROL_DB, bearer(request));
    if (request.method === "GET") {
      requestScope(principal, "profile:read");
      return rpc(null, { protocolVersion: MCP_PROTOCOL_VERSION, serverInfo: { name: "oraja-training", version: env.BUILD_VERSION } });
    }
    const body = await readRequest(request);
    rpcId = body.id;
    const method = typeof body.method === "string" ? body.method : "";
    if (body.jsonrpc !== "2.0" || rpcId === undefined) return rpcError(rpcId, -32600, "invalid_request");
    if (method === "initialize") {
      requestScope(principal, "profile:read");
      return rpc(rpcId, { protocolVersion: MCP_PROTOCOL_VERSION, capabilities: { resources: { subscribe: false }, tools: { listChanged: false } }, serverInfo: { name: "oraja-training", version: env.BUILD_VERSION } });
    }
    if (method === "resources/list") {
      const resources: Record<string, unknown>[] = [];
      if (hasOAuthScope(principal, "profile:read")) resources.push({ uri: "oraja://profile", name: "Profile", mimeType: "application/json" });
      if (hasOAuthScope(principal, "training:read")) {
        for (const days of [7, 30, 90]) resources.push({ uri: `oraja://training/summary/${days}d`, name: `${days} day training summary`, mimeType: "application/json" });
        resources.push({ uri: "oraja://training/model-status", name: "Model status", mimeType: "application/json" }, { uri: "oraja://training/data-quality", name: "Data quality", mimeType: "application/json" });
      }
      if (hasOAuthScope(principal, "recommendations:read")) resources.push({ uri: "oraja://recommendations/latest", name: "Latest recommendations", mimeType: "application/json" });
      if (hasOAuthScope(principal, "advisor:read")) resources.push({ uri: "oraja://advisor/approved", name: "Approved advisor journal", mimeType: "application/json" });
      return rpc(rpcId, { resources });
    }
    if (method === "tools/list") return rpc(rpcId, { tools: toolsFor(principal) });
    if (method === "resources/read") {
      const uri = params(body.params).uri;
      if (uri === "oraja://profile") {
        requestScope(principal, "profile:read");
        const profile = await env.CONTROL_DB.prepare("SELECT public_id, display_name, timezone, status FROM profiles WHERE account_id = ?1 AND id = ?2 AND status <> 'deleted'").bind(principal.accountId, principal.profileId).first<Record<string, unknown>>();
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(profile ?? {}) }] });
      }
      const summaryMatch = typeof uri === "string" ? /^oraja:\/\/training\/summary\/(7|30|90)d$/.exec(uri) : null;
      if (summaryMatch) {
        requestScope(principal, "training:read");
        const summary = await profileDoJson(env, principal, `/internal/training-summary?days=${summaryMatch[1]}`);
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(summary) }] });
      }
      if (uri === "oraja://training/model-status") {
        requestScope(principal, "training:read");
        const jobs = await env.CONTROL_DB.prepare("SELECT status, COUNT(*) AS count, MAX(updated_at) AS last_updated_at FROM jobs WHERE account_id = ?1 AND profile_id = ?2 GROUP BY status").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify({ jobs: jobs.results }) }] });
      }
      if (uri === "oraja://training/data-quality") {
        requestScope(principal, "training:read");
        const summary = await profileDoJson(env, principal, "/internal/training-summary?days=90");
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(summary.data_quality ?? {}) }] });
      }
      if (uri === "oraja://recommendations/latest") {
        requestScope(principal, "recommendations:read");
        const latest = await env.CONTROL_DB.prepare("SELECT table_kind, revision, content_hash, updated_at FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2 ORDER BY table_kind").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(latest.results) }] });
      }
      if (uri === "oraja://advisor/approved") {
        requestScope(principal, "advisor:read");
        const rows = await env.CONTROL_DB.prepare("SELECT id, provider_name, title, proposal_hash, status, created_at, decided_at FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 AND status = 'approved' ORDER BY created_at DESC LIMIT 100").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(rpcId, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(rows.results) }] });
      }
      throw new ApiError("resource_not_found", 404);
    }
    if (method === "tools/call") {
      const call = params(body.params);
      const name = call.name;
      const args = params(call.arguments);
      if (name === "training_summary" || name === "missing_data") {
        requestScope(principal, "training:read");
        const days = integer(args.days, 30, [7, 30, 90]);
        const summary = await profileDoJson(env, principal, `/internal/training-summary?days=${days}`);
        return rpc(rpcId, textResult(name === "missing_data" ? summary.data_quality ?? {} : summary));
      }
      if (name === "trend_compare") {
        requestScope(principal, "training:read");
        const recentDays = integer(args.recent_days, 7, [7, 30, 90]);
        const baselineDays = integer(args.baseline_days, 30, [7, 30, 90]);
        const [recent, baseline] = await Promise.all([profileDoJson(env, principal, `/internal/training-summary?days=${recentDays}`), profileDoJson(env, principal, `/internal/training-summary?days=${baselineDays}`)]);
        return rpc(rpcId, textResult({ recent, baseline }));
      }
      if (name === "play_history") {
        requestScope(principal, "plays:read");
        const limit = Number(args.limit ?? 50);
        if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new ApiError("invalid_arguments", 400);
        const rawCursor = await verifyCursor(env, principal, args.cursor);
        const query = new URLSearchParams({ limit: String(limit) });
        if (rawCursor) query.set("cursor", rawCursor);
        const page = await profileDoJson(env, principal, `/internal/plays?${query}`);
        if (typeof page.next_cursor === "string") page.next_cursor = await signCursor(env, principal, page.next_cursor);
        return rpc(rpcId, textResult(page));
      }
      if (name === "recommendation_reason") {
        requestScope(principal, "recommendations:read");
        const latest = await env.CONTROL_DB.prepare("SELECT table_kind, revision, content_hash, updated_at FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2 ORDER BY table_kind").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(rpcId, textResult({ artifacts: latest.results, raw_database: false }));
      }
      if (name === "advisor_context_export") {
        requestScope(principal, "advisor:read");
        const days = integer(args.days, 30, [7, 30, 90]);
        const summary = await profileDoJson(env, principal, `/internal/training-summary?days=${days}`);
        return rpc(rpcId, textResult({ window_days: days, training_summary: summary, excludes: ["raw_database", "object_keys", "credentials", "conversation_text"] }));
      }
      if (name === "advisor_propose") {
        requestScope(principal, "advisor:propose");
        const proposal = await createAdvisorProposal(env.CONTROL_DB, principal, { providerName: args.provider_name, title: args.title, payload: args.payload });
        return rpc(rpcId, textResult(proposal));
      }
      throw new ApiError("tool_not_found", 404);
    }
    return rpcError(rpcId, -32601, "method_not_found", 404);
  } catch (error) {
    if (error instanceof ApiError) return rpcError(rpcId, -32000, error.code, error.status);
    return rpcError(rpcId, -32603, "internal_error", 500);
  }
}
