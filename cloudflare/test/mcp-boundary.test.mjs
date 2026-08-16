import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

test("MCP follows the canonical protocol and never exposes internal storage identifiers", async () => {
  const source = await readFile(new URL("worker/src/mcp.ts", root), "utf8");
  assert.match(source, /MCP_PROTOCOL_VERSION = "2026-07-28"/);
  assert.doesNotMatch(source, /SELECT[^\n]+object_key/);
  assert.doesNotMatch(source, /JSON\.stringify\(\{ profile_id:/);
  assert.match(source, /principal\.profileId/);
});

test("MCP discovery and calls enforce each sensitive scope", async () => {
  const source = await readFile(new URL("worker/src/mcp.ts", root), "utf8");
  for (const scope of ["profile:read", "training:read", "plays:read", "recommendations:read", "advisor:read", "advisor:propose"]) {
    assert.match(source, new RegExp(`requestScope\\(principal, "${scope.replace(":", "\\:")}"\\)|hasOAuthScope\\(principal, "${scope.replace(":", "\\:")}"\\)`));
  }
  for (const tool of ["training_summary", "trend_compare", "missing_data", "play_history", "recommendation_reason", "advisor_context_export", "advisor_propose"]) {
    assert.match(source, new RegExp(`name: "${tool}"`));
  }
});

test("play history is bounded, profile-owned, projected, and uses signed cursors", async () => {
  const mcp = await readFile(new URL("worker/src/mcp.ts", root), "utf8");
  const profile = await readFile(new URL("worker/src/profile-do.ts", root), "utf8");
  assert.match(mcp, /HMAC/);
  assert.match(mcp, /limit < 1 \|\| limit > 100/);
  assert.match(profile, /limit: limit \+ 1/);
  assert.match(profile, /function publicPlay/);
  for (const forbidden of ["payload_digest", "profile_internal_id", "device_internal_id", "job_id"]) {
    const projection = profile.slice(profile.indexOf("function publicPlay"), profile.indexOf("function ackFor"));
    assert.doesNotMatch(projection, new RegExp(forbidden));
  }
});
