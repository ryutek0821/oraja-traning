import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const advisor = await readFile(new URL("worker/src/advisor.ts", root), "utf8");
const mcp = await readFile(new URL("worker/src/mcp.ts", root), "utf8");
const index = await readFile(new URL("worker/src/index.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0012_advisor_journal.sql", root), "utf8");

test("advisor proposals use a strict bounded self-reported schema", () => {
  assert.match(advisor, /\["body", "evidence_period", "model", "provider_self_reported"\]/);
  assert.match(advisor, /provider_self_report_required/);
  assert.match(advisor, /MAX_EVIDENCE_SECONDS = 90/);
  assert.match(advisor, /unsafe_proposal_markup/);
  assert.match(advisor, /secret_shaped_proposal/);
  assert.match(advisor, /conversation_dump_not_allowed/);
  assert.match(mcp, /provider_self_reported: \{ const: true \}/);
});

test("pending proposals expire and decisions remain owner scoped", () => {
  assert.match(advisor, /status = 'expired'/);
  assert.match(advisor, /expires_at <= \?3/);
  assert.match(advisor, /account_id = \?7 AND profile_id = \?8 AND status = 'pending'/);
  assert.match(migration, /CHECK \(status IN \('pending', 'approved', 'rejected', 'expired'\)\)/);
});

test("approval atomically creates immutable journal and append-only audit", () => {
  assert.match(advisor, /await db\.batch\(statements\)/);
  assert.match(advisor, /INSERT INTO advisor_journal/);
  assert.match(advisor, /INSERT INTO advisor_decision_audits/);
  assert.match(migration, /advisor_journal_is_append_only/);
  assert.match(migration, /advisor_decision_audits_are_append_only/);
  assert.match(mcp, /FROM advisor_journal WHERE account_id = \?1 AND profile_id = \?2/);
  assert.match(index, /requireWebAccount\(request, env, Boolean\(match\[1\]\)\)/);
  assert.match(index, /Third-party OAuth clients may propose/);
});

test("normal deletes are blocked while confirmed privacy erasure may purge advisor data", () => {
  assert.match(migration, /advisor_journal_append_only_delete[\s\S]*status = 'confirmed'/);
  assert.match(migration, /advisor_decision_audits_append_only_delete[\s\S]*status = 'confirmed'/);
});
