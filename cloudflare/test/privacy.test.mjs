import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const privacy = await readFile(new URL("worker/src/privacy.ts", root), "utf8");
const profileDo = await readFile(new URL("worker/src/profile-do.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");
const exporter = await readFile(new URL("worker/src/privacy-export.ts", root), "utf8");
const migration = await readFile(new URL("migrations/0010_privacy_lifecycle.sql", root), "utf8");
const encryptionMigration = await readFile(new URL("migrations/0015_encrypted_privacy_exports.sql", root), "utf8");
const cryptoSource = await readFile(new URL("worker/src/privacy-crypto.ts", root), "utf8");

test("export uses a versioned job and owner-scoped durable outbox", () => {
  assert.match(migration, /CREATE TABLE privacy_export_jobs/);
  assert.match(migration, /oraja\.profile-export\.v1/);
  assert.match(privacy, /export_snapshot/);
  assert.match(privacy, /privacy\.export\.requested/);
  assert.match(privacy, /completeExportTask/);
  assert.match(privacy, /archiveSha256/);
  assert.match(privacy, /downloaded_at IS NULL AND e\.expires_at > \?4/);
});

test("deletion remains cancellable for seven days and start is compare-and-set", () => {
  assert.match(privacy, /7 \* 24 \* 60 \* 60/);
  assert.match(privacy, /status = 'pending' AND cancel_until >= \?1/);
  assert.match(privacy, /status = 'confirmed'.*status = 'pending' AND cancel_until < \?1/s);
  assert.match(migration, /deletion_requests_require_active_owner/);
  assert.match(privacy, /status IN \('pending', 'confirmed'\)/);
});

test("deletion start revokes every credential class before purge", () => {
  for (const table of [
    "sessions", "credentials", "recovery_codes", "email_tokens", "devices",
    "device_token_hashes", "capability_hashes", "oauth_authorization_codes",
    "oauth_grants", "oauth_tokens",
  ]) assert.match(privacy, new RegExp(`UPDATE ${table}`));
});

test("DO purge leaves a tombstone and blocks recreation", () => {
  assert.match(profileDo, /await storage\.deleteAll\(\)/);
  assert.match(profileDo, /await storage\.put\("privacy:tombstone"/);
  assert.match(profileDo, /if \(tombstone !== undefined\) throw new Error\("profile_deleted"\)/);
});

test("completion is fail-closed on all purge tasks and writes immutable audit", () => {
  assert.match(privacy, /counts\.total < 1 \|\| counts\.succeeded !== counts\.total/);
  assert.match(migration, /privacy_completion_audits_are_append_only/);
  assert.match(privacy, /r2_purge_unverified/);
  assert.match(privacy, /privacy\.deletion\.completed/);
});

test("scheduled deletion sweep executes only purge tasks and finalizes verified owners", () => {
  assert.match(privacy, /task_kind <> 'export_snapshot'/);
  assert.match(privacy, /task_kind = 'export_snapshot'/);
  for (const operation of ["purgeDueProfiles", "claimPrivacyTasks", "runPurgeTask", "recordPrivacyTaskResult", "finalizeDeletion"]) {
    assert.match(worker, new RegExp(operation));
  }
  assert.match(worker, /schedule === "deletion-sweep"/);
});

test("export download is same-origin authenticated, one-time, and never exposes its object key", () => {
  assert.match(worker, /claimExportDownload/);
  assert.match(worker, /BACKUP_BUCKET\.get\(claimed\.targetKey\)/);
  assert.match(worker, /content-disposition/);
  assert.doesNotMatch(worker, /json\(\{[^}]*targetKey/);
});

test("export snapshot is owner scoped, bounded, digest verified, and connected to backup schedule", () => {
  assert.match(exporter, /MAX_EXPORT_BYTES/);
  assert.match(exporter, /MAX_EXPORT_PLAYS/);
  assert.match(exporter, /account_id = \?1 AND (?:id|profile_id) = \?2/);
  assert.match(exporter, /BACKUP_BUCKET\.head/);
  assert.match(exporter, /completeExportTask/);
  for (const secret of ["credential_hashes", "oauth_tokens", "device_tokens", "storage_object_keys", "raw_replay", "conversation_text"]) {
    assert.match(exporter, new RegExp(secret));
  }
  assert.match(worker, /schedule === "daily-backup".*processExportSnapshots/s);
  assert.match(exporter, /advisor_journal/);
  assert.match(exporter, /advisor_decision_audits/);
  assert.match(privacy, /DELETE FROM advisor_journal/);
  assert.match(privacy, /DELETE FROM advisor_decision_audits/);
});

test("privacy archives are authenticated ciphertext with one-time authenticated key delivery", () => {
  assert.match(cryptoSource, /AES-GCM/);
  assert.match(cryptoSource, /crypto\.getRandomValues\(new Uint8Array\(32\)\)/);
  assert.match(exporter, /env\.EXPORT_KEK/);
  assert.match(exporter, /application\/vnd\.oraja\.profile-export\+encrypted/);
  assert.doesNotMatch(exporter, /customMetadata:[^\n]*wrappedKey/);
  assert.match(encryptionMigration, /wrapped_key_b64/);
  assert.match(encryptionMigration, /privacy_export_ready_requires_encryption/);
  assert.match(worker, /x-oraja-export-key/);
  assert.match(worker, /unwrapExportKey/);
  assert.match(worker, /cache-control": "no-store"/);
});

test("portable snapshot includes complete owner state and bounded artifact bodies without keys", () => {
  for (const entity of ["profile_settings", "job_events", "job_revisions", "revision_outcomes", "workflow_step_attempts", "oauth_grants", "raw_inventory", "advisor_journal"]) {
    assert.match(exporter, new RegExp(entity));
  }
  assert.match(exporter, /MAX_ARTIFACT_BYTES/);
  assert.match(exporter, /object\.text\(\)/);
  assert.match(profileDo, /entries: page\.map/);
  assert.match(profileDo, /portableState/);
});
