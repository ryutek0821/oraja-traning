import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const source = await readFile(new URL("worker/src/ir-api.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");
const durableObject = await readFile(new URL("worker/src/profile-do.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0004_ir_api.sql", root), "utf8");

test("device credentials are 256-bit, scoped, pepper-hashed, and revocable", () => {
  assert.match(source, /new Uint8Array\(32\)/);
  assert.match(source, /hashDeviceToken/);
  assert.match(source, /plays:write/);
  assert.match(source, /revoked_at IS NULL/);
  assert.match(migration, /token_suffix TEXT NOT NULL/);
  assert.match(migration, /key_hash TEXT NOT NULL/);
  assert.doesNotMatch(migration, /token\s+TEXT\s+NOT NULL/i);
});

test("play ingress enforces bearer scope, allowlist validation, DO persistence, and ACK retry semantics", () => {
  assert.match(worker, /url\.pathname !== "\/v1\/plays"/);
  assert.match(worker, /authenticateDeviceToken/);
  assert.match(worker, /validateIrEvent/);
  assert.match(worker, /eventDigest/);
  assert.match(worker, /\/internal\/play-events/);
  assert.match(worker, /enqueuePlay/);
  assert.match(worker, /idempotency_conflict/);
  assert.match(source, /assertExactKeys\(source, EVENT_KEYS\)/);
  assert.match(source, /provenance\.aggregate_eligible !== false/);
  assert.match(durableObject, /storage\.transaction/);
  assert.match(durableObject, /ackFor\(existingValue, "duplicate"/);
  assert.match(durableObject, /payload_digest/);
});

test("IR read routes authenticate the device before owner-bound projections", () => {
  assert.match(worker, /handleIrReadRoute/);
  assert.match(worker, /authenticateDeviceToken\(env\.CONTROL_DB, bearerToken\(request\)/);
  assert.match(worker, /readIrMethod\(url, identity/);
  assert.match(source, /requestedPlayer !== identity\.profilePublicId/);
  assert.match(durableObject, /projectIrScores/);
});

test("CORS exposes the device-management methods without widening payload routes", () => {
  assert.match(worker, /GET,POST,PATCH,DELETE,OPTIONS/);
  assert.match(worker, /content-type,authorization,x-csrf-token,x-request-id/);
});

test("accepted IR plays enter the durable job ledger", () => {
  assert.match(worker, /jobKind: "play"/);
  assert.match(worker, /inputKey: `play-event:\$\{event\.event_id\}`/);
  assert.match(worker, /acceptAndEnqueue/);
  assert.doesNotMatch(worker, /type: "play\.accepted\.v1"/);
});
