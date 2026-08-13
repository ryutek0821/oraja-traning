import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const tables = await readFile(new URL("worker/src/tables.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0007_tables.sql", root), "utf8");

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
