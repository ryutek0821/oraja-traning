import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function loadTypeScriptModule(relativePath, replacements) {
  let source = await readFile(new URL(relativePath, root), "utf8");
  for (const [pattern, replacement] of replacements) source = source.replace(pattern, replacement);
  source = stripTypeScriptTypes(source, { mode: "strip" });
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}

function storedPlay({ ex = 23, clear = 5, minbp = 2, course = false, sha = "a".repeat(64) } = {}) {
  const pg = Math.floor(ex / 2);
  return {
    event_id: "018bcfe5-6800-7000-8000-000000000001",
    payload_digest: "f".repeat(64),
    profile_internal_id: "profile-internal",
    device_internal_id: "device-internal",
    job_id: "018bcfe5-6800-7000-8000-000000000002",
    revision: 1,
    accepted_at: 1_700_000_001,
    enqueued_at: 1_700_000_002,
    event: {
      event_id: "018bcfe5-6800-7000-8000-000000000001",
      occurred_at: "2023-11-14T22:13:20Z",
      game_mode: "SP7",
      rule: "BEATORAJA-SP7",
      is_course: course,
      chart: { sha256: sha, ln_mode: 0 },
      play: {
        clear,
        gauge_kind: "HARD",
        assist_kind: "NONE",
        notes: 100,
        passnotes: 100,
        minbp,
        ex_score: ex,
        combo: 80,
        options: ["RANDOM"],
        seed: 123,
        random: 0,
        judgements: { epg: pg, lpg: 0, egr: ex - pg * 2, lgr: 0, egd: 0, lgd: 0, ebd: 0, lbd: 0, epr: 0, lpr: 0, ems: 0, lms: 0 },
      },
    },
  };
}

test("Profile DO projects only a bounded owner best-score shape and excludes courses/internal fields", async () => {
  const profile = await loadTypeScriptModule("worker/src/profile-do.ts", [
    ['import { DurableObject } from "cloudflare:workers";', "class DurableObject { constructor() {} }"],
  ]);
  const scores = profile.projectIrScores([
    storedPlay({ ex: 20, clear: 4, minbp: 8 }),
    storedPlay({ ex: 24, clear: 5, minbp: 2 }),
    storedPlay({ ex: 200, course: true }),
  ], "profile-public", "a".repeat(64), 0);
  assert.equal(scores.length, 1);
  assert.equal(scores[0].id, "profile-public");
  assert.equal(scores[0].epg * 2 + scores[0].egr, 24);
  assert.equal(scores[0].option, 2);
  assert.equal(scores[0].gauge, 3);
  assert.equal(scores[0].device_internal_id, undefined);
  assert.equal(scores[0].event_id, undefined);
});

test("IR read methods bind D1 and Durable Object reads to the authenticated device owner", async () => {
  const ir = await loadTypeScriptModule("worker/src/ir-api.ts", [
    [/import \{ AuthError, sha256Hex \} from "\.\/auth";/, `class AuthError extends Error {}
      const sha256Hex = async () => "0".repeat(64);`],
    [/export class ApiError extends Error \{\n  constructor\(\n    public readonly code: string,\n    public readonly status = 400,\n    public readonly retryable = false,\n    public readonly retryAfterSeconds\?: number,\n  \) \{\n    super\(code\);\n    this\.name = "ApiError";\n  \}\n\}/, `export class ApiError extends Error {
      constructor(code, status = 400, retryable = false, retryAfterSeconds) {
        super(code); this.code = code; this.status = status;
        this.retryable = retryable; this.retryAfterSeconds = retryAfterSeconds;
      }
    }`],
  ]);
  const identity = {
    accountId: "account-internal",
    profileId: "profile-internal",
    profilePublicId: "018bcfe5-6800-7000-8000-000000000010",
    deviceId: "device-internal",
    devicePublicId: "018bcfe5-6800-7000-8000-000000000011",
    scopes: ["plays:write"],
    tokenHash: "hash",
  };
  let selectedDo;
  const dependencies = {
    buildVersion: "build-1",
    db: {
      prepare(sql) {
        assert.match(sql, /id = \?1 AND account_id = \?2 AND public_id = \?3/);
        return { bind(...values) {
          assert.deepEqual(values, [identity.profileId, identity.accountId, identity.profilePublicId]);
          return { async first() { return { public_id: identity.profilePublicId, display_name: "Owner" }; } };
        } };
      },
    },
    profileDo: {
      idFromName(value) { selectedDo = value; return `do:${value}`; },
      get(id) {
        assert.equal(id, `do:${identity.profileId}`);
        return { async fetch(url) {
          const parsed = new URL(url);
          assert.equal(parsed.searchParams.get("profile_id"), identity.profilePublicId);
          assert.equal(parsed.searchParams.get("sha256"), "a".repeat(64));
          return new Response(JSON.stringify({ contract: "ir-read.v1", scores: [], truncated: false, scanned_limit: 1000 }));
        } };
      },
    },
  };

  const player = await ir.readIrMethod(new URL("https://example.test/v1/ir/player"), identity, dependencies);
  assert.deepEqual(player.player, { id: identity.profilePublicId, name: "Owner", rank: "" });
  const scores = await ir.readIrMethod(new URL(`https://example.test/v1/ir/play-data?player_id=${identity.profilePublicId}&sha256=${"a".repeat(64)}&ln_mode=0`), identity, dependencies);
  assert.deepEqual(scores.scores, []);
  assert.equal(selectedDo, identity.profileId);

  await assert.rejects(
    ir.readIrMethod(new URL("https://example.test/v1/ir/play-data?player_id=someone-else"), identity, dependencies),
    (error) => error.code === "forbidden" && error.status === 403,
  );
  assert.deepEqual((await ir.readIrMethod(new URL("https://example.test/v1/ir/rivals"), identity, dependencies)).players, []);
  assert.deepEqual((await ir.readIrMethod(new URL("https://example.test/v1/ir/tables"), identity, dependencies)).tables, []);
  const version = (await ir.readIrMethod(new URL("https://example.test/v1/ir/version?current_version=client"), identity, dependencies)).version;
  assert.equal(version.version, "client");
  assert.equal(version.server_build, "build-1");
});
