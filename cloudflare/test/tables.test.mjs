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
