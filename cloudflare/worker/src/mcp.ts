import { ApiError } from "./ir-api";
import { authenticateOAuthToken, hasOAuthScope, type OAuthPrincipal, type OAuthScope } from "./oauth";
import type { Env } from "./index";

type JsonRpcRequest = {
  jsonrpc?: unknown;
  id?: unknown;
  method?: unknown;
  params?: unknown;
};

function rpc(id: unknown, result: unknown): Response {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: id ?? null, result }) + "\n", {
    status: 200,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
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
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) throw new ApiError("unsupported_media_type", 415);
  const value: unknown = await request.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_jsonrpc", 400);
  return value as JsonRpcRequest;
}

function bearer(request: Request): string | null {
  const value = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+([^\s]+)$/i.exec(value);
  return match?.[1] ?? null;
}

export async function handleMcp(request: Request, env: Env): Promise<Response> {
  if (request.method !== "POST" && request.method !== "GET") return rpcError(null, -32600, "method_not_allowed", 405);
  const origin = request.headers.get("Origin");
  if (origin && !env.CORS_ORIGINS.split(",").map((item) => item.trim()).includes(origin)) {
    return rpcError(null, -32001, "origin_not_allowed", 403);
  }
  try {
    const principal = await authenticateOAuthToken(env.CONTROL_DB, bearer(request));
    if (request.method === "GET") {
      requestScope(principal, "profile:read");
      return rpc(null, { protocolVersion: "2025-11-25", serverInfo: { name: "oraja-training", version: env.BUILD_VERSION } });
    }
    const body = await readRequest(request);
    const id = body.id;
    const method = typeof body.method === "string" ? body.method : "";
    if (body.jsonrpc !== "2.0" || id === undefined) return rpcError(id, -32600, "invalid_request");
    if (method === "initialize") {
      requestScope(principal, "profile:read");
      return rpc(id, {
        protocolVersion: "2025-11-25",
        capabilities: { resources: { subscribe: false }, tools: { listChanged: false } },
        serverInfo: { name: "oraja-training", version: env.BUILD_VERSION },
      });
    }
    if (method === "resources/list") {
      requestScope(principal, "profile:read");
      return rpc(id, {
        resources: [
          { uri: "oraja://profile", name: "Profile", mimeType: "application/json" },
          { uri: "oraja://recommendations/latest", name: "Latest recommendations", mimeType: "application/json" },
          { uri: "oraja://advisor/approved", name: "Approved advisor journal", mimeType: "application/json" },
        ],
      });
    }
    if (method === "tools/list") {
      requestScope(principal, "training:read");
      return rpc(id, {
        tools: [
          { name: "training_summary", description: "Read a bounded profile summary", inputSchema: { type: "object", properties: {} } },
          { name: "recommendation_reason", description: "Read recommendation metadata", inputSchema: { type: "object", properties: {} } },
          ...(hasOAuthScope(principal, "advisor:propose") ? [{ name: "advisor_propose", description: "Create a pending advisor proposal", inputSchema: { type: "object" } }] : []),
        ],
      });
    }
    if (method === "resources/read") {
      const params = body.params && typeof body.params === "object" ? body.params as Record<string, unknown> : {};
      const uri = params.uri;
      if (uri === "oraja://profile") {
        requestScope(principal, "profile:read");
        const profile = await env.CONTROL_DB.prepare("SELECT public_id, display_name, timezone, status FROM profiles WHERE account_id = ?1 AND id = ?2 AND status <> 'deleted'").bind(principal.accountId, principal.profileId).first<Record<string, unknown>>();
        return rpc(id, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(profile ?? {}) }] });
      }
      if (uri === "oraja://recommendations/latest") {
        requestScope(principal, "recommendations:read");
        const latest = await env.CONTROL_DB.prepare("SELECT table_kind, revision, content_hash, object_key, updated_at FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2 ORDER BY table_kind").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(id, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(latest.results) }] });
      }
      if (uri === "oraja://advisor/approved") {
        requestScope(principal, "advisor:read");
        const rows = await env.CONTROL_DB.prepare("SELECT id, provider_name, title, proposal_hash, status, created_at, decided_at FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 AND status = 'approved' ORDER BY created_at DESC LIMIT 100").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(id, { contents: [{ uri, mimeType: "application/json", text: JSON.stringify(rows.results) }] });
      }
      throw new ApiError("resource_not_found", 404);
    }
    if (method === "tools/call") {
      requestScope(principal, "training:read");
      const params = body.params && typeof body.params === "object" ? body.params as Record<string, unknown> : {};
      if (params.name === "training_summary") {
        const summary = await env.CONTROL_DB.prepare("SELECT status, COUNT(*) AS count FROM jobs WHERE account_id = ?1 AND profile_id = ?2 GROUP BY status").bind(principal.accountId, principal.profileId).all<Record<string, unknown>>();
        return rpc(id, { content: [{ type: "text", text: JSON.stringify(summary.results) }] });
      }
      if (params.name === "recommendation_reason") {
        requestScope(principal, "recommendations:read");
        return rpc(id, { content: [{ type: "text", text: JSON.stringify({ profile_id: principal.profileId, raw_database: false }) }] });
      }
      throw new ApiError("tool_not_found", 404);
    }
    return rpcError(id, -32601, "method_not_found", 404);
  } catch (error) {
    if (error instanceof ApiError) return rpcError(null, -32000, error.code, error.status);
    return rpcError(null, -32603, "internal_error", 500);
  }
}
