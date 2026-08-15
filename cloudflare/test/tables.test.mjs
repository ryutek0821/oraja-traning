import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const tables = await readFile(new URL("worker/src/tables.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0007_tables.sql", root), "utf8");
const settingsMigration = await readFile(new URL("migrations/0013_profile_settings.sql", root), "utf8");
const settings = await readFile(new URL("worker/src/profile-settings.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");
const dashboard = await readFile(new URL("worker/src/dashboard.ts", root), "utf8");
const bridge = await readFile(new URL("worker/src/container-bridge.ts", root), "utf8");
const workflow = await readFile(new URL("worker/src/workflow.ts", root), "utf8");
const ledger = await readFile(new URL("worker/src/job-ledger.ts", root), "utf8");
const catalogMigration = await readFile(new URL("migrations/0014_catalog_levels.sql", root), "utf8");

test("capability table route hashes the secret and never returns object keys", () => {
  assert.match(tables, /crypto\.subtle\.digest\("SHA-256"/);
  assert.match(tables, /recommend\|today/);
  assert.match(tables, /header\\\.json\|score\\\.json/);
  assert.match(tables, /c\.capability_kind = \?2/);
  assert.match(tables, /c\.revoked_at IS NULL/);
  assert.match(tables, /ARTIFACT_BUCKET\.get\(objectKey\)/);
  assert.match(tables, /"cache-control": "private, no-store"/);
  assert.doesNotMatch(tables, /JSON\.stringify\(row/);
});

test("menu dates use the documented 04:00 boundary", () => {
  assert.match(tables, /Number\(value\.hour\) < 4/);
  assert.match(tables, /localDate\.setUTCDate\(localDate\.getUTCDate\(\) - 1\)/);
});

test("latest table pointers reference an immutable artifact revision", () => {
  assert.match(migration, /FOREIGN KEY\(account_id, profile_id, table_kind, revision, content_hash, object_key\)/);
  assert.match(migration, /REFERENCES artifact_revisions/);
});

test("profile settings are owner scoped, bounded, and CSRF protected", () => {
  assert.match(settingsMigration, /PRIMARY KEY\(account_id, profile_id\)/);
  assert.match(settingsMigration, /readiness IN \('normal', 'tired'\)/);
  assert.match(settings, /WHERE p\.account_id = \?1 AND p\.status = 'active'/);
  assert.match(settings, /nextReserve > nextTarget/);
  assert.match(settings, /settings_revision = profile_settings\.settings_revision \+ 1/);
  assert.match(worker, /requireWebAccount\(request, env, mutation\)/);
  assert.match(worker, /request\.method === "PATCH"/);
  assert.match(settings, /JOIN artifact_latest recommend/);
  assert.match(settings, /JOIN artifact_latest today/);
  assert.match(worker, /ARTIFACT_BUCKET\.head\(source\.recommendObjectKey\)/);
  assert.match(worker, /eventId: `settings:\$\{source\.profileId\}:\$\{source\.settingsRevision\}`/);
  assert.match(worker, /jobKind: "regenerate"/);
});

test("table generation consumes a versioned catalog and publishes immutable owner-scoped parts with CAS", () => {
  assert.match(catalogMigration, /ADD COLUMN level TEXT/);
  assert.match(bridge, /v\.status = 'active'/);
  assert.match(bridge, /e\.level IS NOT NULL AND e\.song_mode = 7/);
  assert.match(bridge, /catalog_manifest_sha256/);
  assert.match(workflow, /table\("recommend"\), table\("today"\)/);
  assert.match(workflow, /canonicalJson\(\{ kind: item\.kind, parts: item\.parts \}\)/);
  assert.match(ledger, /INSERT OR IGNORE INTO artifact_revisions/);
  assert.match(ledger, /excluded\.revision > artifact_latest\.revision/);
  assert.match(ledger, /artifact_immutable_conflict/);
});

test("table capability plaintext is returned once and never listed or persisted", () => {
  assert.match(settings, /new Uint8Array\(32\)/);
  assert.match(settings, /secret_hash/);
  assert.match(settings, /capability_url:/);
  assert.match(settingsMigration, /one_live_table_capability/);
  const listBody = settings.slice(settings.indexOf("export async function listTableCapabilities"), settings.indexOf("export async function rotateTableCapability"));
  assert.doesNotMatch(listBody, /secret_hash|capability_url|secret/);
  assert.doesNotMatch(dashboard, /capability_url|secret_hash/);
  assert.match(settings, /capability\.rotated/);
  assert.match(settings, /capability\.revoked/);
});
