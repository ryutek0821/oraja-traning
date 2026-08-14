import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function loadMcp() {
  const source = await readFile(new URL("worker/src/mcp.ts", root), "utf8");
  let code = stripTypeScriptTypes(source, { mode: "strip" });
  code = code
    .replace('import { createAdvisorProposal } from "./advisor";', "const createAdvisorProposal = async () => ({});")
    .replace('import { ApiError } from "./ir-api";', `class ApiError extends Error {
      constructor(code, status) { super(code); this.code = code; this.status = status; }
    }`)
    .replace(
      /import \{ authenticateOAuthToken, hasOAuthScope, mcpOAuthResource,[^}]*\} from "\.\/oauth";/,
      `const authenticateOAuthToken = async (_db, _token, _scope, _now, resource) => {
        globalThis.__mcpRequiredResource = resource;
        return globalThis.__mcpPrincipal;
      };
      const hasOAuthScope = (principal, scope) => principal.scopes.includes(scope);
      const mcpOAuthResource = (origin) => new URL("/mcp", new URL(origin).origin).toString();`,
    );
  return import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
}

class SessionDatabase {
  #sessions = new Map();

  prepare(sql) {
    const database = this;
    return {
      bind(...values) {
        return {
          async run() {
            if (sql.startsWith("INSERT INTO sessions")) {
              const [, accountId, hash, , expiresAt] = values;
              database.#sessions.set(hash, { accountId, expiresAt, revokedAt: null });
              return { success: true };
            }
            if (sql.startsWith("UPDATE sessions SET revoked_at")) {
              const [revokedAt, accountId, hash] = values;
              const row = database.#sessions.get(hash);
              if (row && row.accountId === accountId && row.revokedAt === null) row.revokedAt = revokedAt;
              return { success: true };
            }
            throw new Error(`unexpected run: ${sql}`);
          },
          async first() {
            if (sql.startsWith("SELECT expires_at FROM sessions")) {
              const [accountId, hash] = values;
              const row = database.#sessions.get(hash);
              return row && row.accountId === accountId && row.revokedAt === null ? { expires_at: row.expiresAt } : null;
            }
            throw new Error(`unexpected first: ${sql}`);
          },
        };
      },
    };
  }
}

function post(body, extraHeaders = {}) {
  return new Request("https://training.example/mcp", {
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      authorization: `Bearer ${"x".repeat(40)}`,
      "content-type": "application/json",
      ...extraHeaders,
    },
    body: JSON.stringify(body),
  });
}

function transportRequest(method, sessionId, accept = "text/event-stream") {
  return new Request("https://training.example/mcp", {
    method,
    headers: {
      accept,
      authorization: `Bearer ${"x".repeat(40)}`,
      "mcp-protocol-version": "2026-07-28",
      "mcp-session-id": sessionId,
    },
  });
}

test("Streamable HTTP negotiates a fixed protocol and binds an opaque session to the OAuth principal", async () => {
  const mcp = await loadMcp();
  const now = Math.floor(Date.now() / 1000);
  const principal = {
    clientId: "client-secret-id",
    accountId: "account-secret-id",
    profileId: "profile-secret-id",
    scopes: ["profile:read", "training:read"],
    resource: "https://training.example/mcp",
    grantId: "grant-secret-id",
    expiresAt: now + 3600,
  };
  globalThis.__mcpPrincipal = principal;
  const env = {
    AUTH_HASH_PEPPER: "test-signing-secret-with-sufficient-entropy",
    BUILD_VERSION: "test",
    CORS_ORIGINS: "https://client.example",
    PUBLIC_ORIGIN: "https://training.example",
    CONTROL_DB: new SessionDatabase(),
  };

  const badAccept = await mcp.handleMcp(post({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2026-07-28" } }, { accept: "application/json" }), env);
  assert.equal(badAccept.status, 406);

  const badNegotiation = await mcp.handleMcp(post({ jsonrpc: "2.0", id: 2, method: "initialize", params: { protocolVersion: "2025-11-25" } }), env);
  assert.equal(badNegotiation.status, 400);
  assert.equal((await badNegotiation.json()).error.message, "unsupported_protocol_version");

  const initialized = await mcp.handleMcp(post({
    jsonrpc: "2.0",
    id: 3,
    method: "initialize",
    params: { protocolVersion: "2026-07-28", capabilities: {}, clientInfo: { name: "test", version: "1" } },
  }), env);
  assert.equal(initialized.status, 200);
  assert.equal(globalThis.__mcpRequiredResource, "https://training.example/mcp");
  assert.equal(initialized.headers.get("mcp-protocol-version"), "2026-07-28");
  const sessionId = initialized.headers.get("mcp-session-id");
  assert.ok(sessionId);
  const decodedPayload = Buffer.from(sessionId.split(".")[0], "base64url").toString("utf8");
  for (const secret of [principal.clientId, principal.accountId, principal.profileId, principal.grantId]) {
    assert.equal(decodedPayload.includes(secret), false);
  }

  const wrongVersion = await mcp.handleMcp(post(
    { jsonrpc: "2.0", id: 4, method: "resources/list" },
    { "mcp-session-id": sessionId, "mcp-protocol-version": "2025-11-25" },
  ), env);
  assert.equal(wrongVersion.status, 400);

  globalThis.__mcpPrincipal = { ...principal, profileId: "other-profile" };
  const wrongPrincipal = await mcp.handleMcp(post(
    { jsonrpc: "2.0", id: 5, method: "resources/list" },
    { "mcp-session-id": sessionId, "mcp-protocol-version": "2026-07-28" },
  ), env);
  assert.equal(wrongPrincipal.status, 404);
  globalThis.__mcpPrincipal = principal;

  const notification = await mcp.handleMcp(post(
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { "mcp-session-id": sessionId, "mcp-protocol-version": "2026-07-28" },
  ), env);
  assert.equal(notification.status, 202);
  assert.equal(await notification.text(), "");

  const stream = await mcp.handleMcp(transportRequest("GET", sessionId), env);
  assert.equal(stream.status, 200);
  assert.match(stream.headers.get("content-type"), /^text\/event-stream/);
  assert.equal(await stream.text(), "retry: 30000\n: stream-ready\n\n");

  const terminated = await mcp.handleMcp(transportRequest("DELETE", sessionId, "*/*"), env);
  assert.equal(terminated.status, 204);
  const afterTermination = await mcp.handleMcp(transportRequest("GET", sessionId), env);
  assert.equal(afterTermination.status, 404);
});

test("session validation rejects tampering and expiry", async () => {
  const mcp = await loadMcp();
  const now = Math.floor(Date.now() / 1000);
  const principal = {
    clientId: "client", accountId: "account", profileId: "profile", grantId: "grant",
    scopes: ["profile:read"], resource: "https://training.example/mcp", expiresAt: now + 10,
  };
  const env = { AUTH_HASH_PEPPER: "test-secret", CONTROL_DB: new SessionDatabase() };
  const sessionId = await mcp.issueMcpSession(env, principal, now);
  const valid = new Request("https://training.example/mcp", { headers: { "mcp-session-id": sessionId } });
  await assert.doesNotReject(mcp.validateMcpSession(valid, env, principal, now));

  const tamperedId = `${sessionId[0] === "A" ? "B" : "A"}${sessionId.slice(1)}`;
  const tampered = new Request("https://training.example/mcp", { headers: { "mcp-session-id": tamperedId } });
  await assert.rejects(mcp.validateMcpSession(tampered, env, principal, now), (error) => error.code === "invalid_session" && error.status === 404);
  await assert.rejects(mcp.validateMcpSession(valid, env, principal, now + 11), (error) => error.code === "invalid_session" && error.status === 404);
});
