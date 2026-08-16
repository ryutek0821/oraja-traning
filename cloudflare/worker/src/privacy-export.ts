import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";
import { encryptExport, EXPORT_CIPHER } from "./privacy-crypto";
import { claimPrivacyTasks, completeExportTask, recordPrivacyTaskResult, type PrivacyTask } from "./privacy";
import { profileScope } from "./upload-protocol";
import type { Env } from "./index";

const EXPORT_SCHEMA = "oraja.profile-export.v2";
const MAX_EXPORT_BYTES = 16 * 1024 * 1024;
const MAX_EXPORT_PLAYS = 50_000;
const MAX_ARTIFACT_BYTES = 8 * 1024 * 1024;

type Row = Record<string, unknown>;

function ownerNeutral(rows: Row[]): Row[] {
  return rows.map((row) => Object.fromEntries(Object.entries(row).filter(([key]) =>
    !["account_id", "profile_id", "object_key", "artifact_key", "input_key", "target_key"].includes(key),
  )));
}

async function rows(db: D1Database, sql: string, ...bindings: unknown[]): Promise<Row[]> {
  return (await db.prepare(sql).bind(...bindings).all<Row>()).results;
}

async function exportedProfileState(env: Env, profileId: string): Promise<Row> {
  const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(profileId));
  const result: unknown[] = [];
  let cursor: string | null = null;
  do {
    const query = new URLSearchParams({ limit: "500" });
    if (cursor) query.set("cursor", cursor);
    const response = await stub.fetch(`https://profile.internal/internal/privacy/export?${query}`);
    if (!response.ok) throw new ApiError("profile_export_failed", 503);
    const page = await response.json<{ entries?: unknown[]; next_cursor?: unknown }>();
    if (!Array.isArray(page.entries)) throw new ApiError("profile_export_invalid", 503);
    result.push(...page.entries);
    if (result.length > MAX_EXPORT_PLAYS + 1_000) throw new ApiError("profile_export_too_large", 413);
    cursor = typeof page.next_cursor === "string" ? page.next_cursor : null;
  } while (cursor);
  return { entries: result, entry_count: result.length, digest: await sha256Hex(JSON.stringify(result)) };
}

async function artifactContents(env: Env, owner: { accountId: string; profileId: string }): Promise<Row[]> {
  const revisions = await env.CONTROL_DB.prepare(
    `SELECT table_kind, revision, content_hash, object_key, previous_revision, created_at
       FROM artifact_revisions WHERE account_id = ?1 AND profile_id = ?2
      ORDER BY table_kind, revision`,
  ).bind(owner.accountId, owner.profileId).all<{
    table_kind: string; revision: number; content_hash: string; object_key: string;
    previous_revision: number | null; created_at: number;
  }>();
  const exported: Row[] = [];
  let total = 0;
  for (const revision of revisions.results) {
    if (!revision.object_key.endsWith("/header.json")) throw new ApiError("artifact_key_invalid", 503);
    const prefix = revision.object_key.slice(0, -"header.json".length);
    const files: Row = {};
    for (const name of ["header.json", "score.json"] as const) {
      const object = await env.ARTIFACT_BUCKET.get(`${prefix}${name}`);
      if (!object) throw new ApiError("artifact_missing", 503);
      const text = await object.text();
      total += new TextEncoder().encode(text).byteLength;
      if (total > MAX_ARTIFACT_BYTES) throw new ApiError("profile_export_too_large", 413);
      let content: unknown;
      try { content = JSON.parse(text); } catch { throw new ApiError("artifact_invalid_json", 503); }
      files[name] = { sha256: await sha256Hex(text), content };
    }
    exported.push({
      table_kind: revision.table_kind,
      revision: revision.revision,
      previous_revision: revision.previous_revision,
      content_hash: revision.content_hash,
      created_at: revision.created_at,
      files,
    });
  }
  return exported;
}

async function d1Snapshot(task: PrivacyTask, env: Env): Promise<Row> {
  const db = env.CONTROL_DB;
  const accountId = task.accountId;
  const profileId = task.profileId;
  const scope = await profileScope(profileId);
  const [profile, settings, devices, consents, capabilities, grants, deletionHistory, exportHistory,
    jobs, legacyAttempts, events, counters, revisions, outcomes, pointers,
    attempts, deliveries, dispatches, requeues, schedule, audits, proposals, journal, decisions,
    uploadSessions, uploadFiles, uploadParts, dedup] = await Promise.all([
    db.prepare("SELECT public_id, display_name, timezone, status, created_at, updated_at FROM profiles WHERE account_id = ?1 AND id = ?2 AND status <> 'deleted'").bind(accountId, profileId).first<Row>(),
    rows(db, "SELECT target_judged, reserve_judged, readiness, settings_revision, updated_at FROM profile_settings WHERE account_id = ?1 AND profile_id = ?2", accountId, profileId),
    rows(db, "SELECT public_id, label, created_at, revoked_at FROM devices WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at", accountId, profileId),
    rows(db, "SELECT scope, terms_version, granted_at, revoked_at FROM consents WHERE account_id = ?1 AND profile_id = ?2 ORDER BY granted_at", accountId, profileId),
    rows(db, "SELECT capability_kind, created_at, expires_at, revoked_at, last_used_at FROM capability_hashes WHERE account_id = ?1 AND profile_id = ?2 ORDER BY capability_kind, created_at", accountId, profileId),
    rows(db, "SELECT resource, scope, created_at, revoked_at, revoke_reason, reuse_detected_at FROM oauth_grants WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at", accountId, profileId),
    rows(db, "SELECT requested_at, cancel_until, cancelled_at, confirmed_at, status, started_at, completed_at, failure_code FROM deletion_requests WHERE account_id = ?1 AND profile_id = ?2 ORDER BY requested_at", accountId, profileId),
    rows(db, "SELECT export_kind, object_sha256, created_at, expires_at, status, schema_version, ready_at, downloaded_at FROM exports WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at", accountId, profileId),
    rows(db, "SELECT * FROM jobs WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at", accountId, profileId),
    rows(db, "SELECT a.* FROM job_attempts a JOIN jobs j ON j.id = a.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY a.started_at", accountId, profileId),
    rows(db, "SELECT e.* FROM job_events e WHERE e.account_id = ?1 AND e.profile_id = ?2 ORDER BY accepted_at", accountId, profileId),
    rows(db, "SELECT trust_domain, next_revision, updated_at FROM profile_revision_counters WHERE account_id = ?1 AND profile_id = ?2 ORDER BY trust_domain", accountId, profileId),
    rows(db, "SELECT * FROM job_revisions WHERE account_id = ?1 AND profile_id = ?2 ORDER BY revision", accountId, profileId),
    rows(db, "SELECT o.* FROM revision_outcomes o JOIN jobs j ON j.id = o.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY o.recorded_at", accountId, profileId),
    rows(db, "SELECT * FROM latest_pointers WHERE account_id = ?1 AND profile_id = ?2 ORDER BY trust_domain", accountId, profileId),
    rows(db, "SELECT a.* FROM workflow_step_attempts a JOIN jobs j ON j.id = a.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY a.started_at", accountId, profileId),
    rows(db, "SELECT d.* FROM job_deliveries d JOIN jobs j ON j.id = d.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY d.received_at", accountId, profileId),
    rows(db, "SELECT d.* FROM job_dispatches d JOIN jobs j ON j.id = d.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY d.updated_at", accountId, profileId),
    rows(db, "SELECT r.* FROM job_requeues r JOIN jobs j ON j.id = r.job_id WHERE j.account_id = ?1 AND j.profile_id = ?2 ORDER BY r.requested_at", accountId, profileId),
    rows(db, "SELECT * FROM schedule_runs WHERE profile_id = ?1 ORDER BY scheduled_at", profileId),
    rows(db, "SELECT actor_kind, event_type, reason_code, request_id, resource_hash, input_hash, status, occurred_at FROM audit_events WHERE account_id = ?1 AND profile_id = ?2 ORDER BY occurred_at", accountId, profileId),
    rows(db, "SELECT * FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at", accountId, profileId),
    rows(db, "SELECT * FROM advisor_journal WHERE account_id = ?1 AND profile_id = ?2 ORDER BY approved_at", accountId, profileId),
    rows(db, "SELECT * FROM advisor_decision_audits WHERE account_id = ?1 AND profile_id = ?2 ORDER BY occurred_at", accountId, profileId),
    rows(db, "SELECT upload_id, manifest_id, manifest_sha256, month, submission_kind, source_generation, manifest_json, state, created_at, expires_at, completed_at FROM upload_sessions WHERE profile_scope = ?1 ORDER BY created_at", scope),
    rows(db, "SELECT f.upload_id, f.file_name, f.sha256, f.size_bytes, f.state FROM upload_files f JOIN upload_sessions s ON s.upload_id = f.upload_id WHERE s.profile_scope = ?1 ORDER BY f.upload_id, f.file_name", scope),
    rows(db, "SELECT p.upload_id, p.file_name, p.part_number, p.sha256, p.size_bytes FROM upload_parts p JOIN upload_sessions s ON s.upload_id = p.upload_id WHERE s.profile_scope = ?1 ORDER BY p.upload_id, p.file_name, p.part_number", scope),
    rows(db, "SELECT sha256, size_bytes, key_version, created_at FROM upload_dedup WHERE profile_scope = ?1 ORDER BY sha256", scope),
  ]);
  if (!profile) throw new ApiError("profile_not_found", 404);
  return {
    profile,
    profile_settings: settings,
    devices,
    consents,
    capabilities,
    oauth_grants: grants,
    deletion_requests: deletionHistory,
    exports: exportHistory,
    jobs: ownerNeutral(jobs),
    job_attempts: ownerNeutral(legacyAttempts),
    job_events: ownerNeutral(events),
    profile_revision_counters: counters,
    job_revisions: ownerNeutral(revisions),
    revision_outcomes: ownerNeutral(outcomes),
    latest_pointers: ownerNeutral(pointers),
    workflow_step_attempts: ownerNeutral(attempts),
    job_deliveries: ownerNeutral(deliveries),
    job_dispatches: ownerNeutral(dispatches),
    job_requeues: ownerNeutral(requeues),
    schedule_runs: ownerNeutral(schedule),
    audit_events: audits,
    advisor_proposals: ownerNeutral(proposals).map((row) => ({ ...row, payload_json: undefined, payload: JSON.parse(String(row.payload_json)) })),
    advisor_journal: ownerNeutral(journal),
    advisor_decisions: ownerNeutral(decisions),
    raw_inventory: { upload_sessions: uploadSessions, files: uploadFiles, parts: uploadParts, deduplicated: dedup },
  };
}

async function buildExport(task: PrivacyTask, env: Env, now: number): Promise<Uint8Array> {
  if (!task.exportId) throw new ApiError("export_id_missing", 503);
  const owner = { accountId: task.accountId, profileId: task.profileId };
  const [d1, profileState, artifacts] = await Promise.all([
    d1Snapshot(task, env), exportedProfileState(env, task.profileId), artifactContents(env, owner),
  ]);
  const sections = {
    history: d1,
    profile_state: profileState,
    tables: artifacts,
    journal: { advisor_journal: d1.advisor_journal, advisor_decisions: d1.advisor_decisions },
  };
  const sectionDigests = Object.fromEntries(await Promise.all(Object.entries(sections).map(async ([name, value]) =>
    [name, await sha256Hex(JSON.stringify(value))],
  )));
  const value = {
    contract: EXPORT_SCHEMA,
    schema_version: "2",
    portable_owner: "profile:primary",
    exported_at: new Date(now * 1000).toISOString(),
    sections,
    verification: { section_sha256: sectionDigests },
    exclusions: ["credential_hashes", "oauth_tokens", "device_tokens", "capability_secrets", "storage_object_keys", "raw_replay", "conversation_text"],
  };
  const serialized = new TextEncoder().encode(JSON.stringify(value) + "\n");
  if (serialized.byteLength > MAX_EXPORT_BYTES) throw new ApiError("profile_export_too_large", 413);
  return serialized;
}

export async function processExportSnapshots(env: Env, now: number, limit = 5): Promise<number> {
  const tasks = await claimPrivacyTasks(env.CONTROL_DB, now, limit, "export");
  let completed = 0;
  for (const task of tasks) {
    try {
      if (!task.exportId) throw new ApiError("export_id_missing", 503);
      const plaintext = await buildExport(task, env, now);
      const encrypted = await encryptExport(plaintext, task.exportId, env.EXPORT_KEK);
      const plaintextDigest = await sha256Hex(plaintext);
      const ciphertextDigest = await sha256Hex(encrypted.ciphertext);
      await env.BACKUP_BUCKET.put(task.targetKey, encrypted.ciphertext, {
        httpMetadata: { contentType: "application/vnd.oraja.profile-export+encrypted" },
        customMetadata: { schema: EXPORT_SCHEMA, cipher: EXPORT_CIPHER, sha256: ciphertextDigest },
      });
      const stored = await env.BACKUP_BUCKET.head(task.targetKey);
      if (!stored || stored.customMetadata?.sha256 !== ciphertextDigest) throw new ApiError("export_write_unverified", 503);
      await completeExportTask(env.CONTROL_DB, task.id, ciphertextDigest, {
        algorithm: EXPORT_CIPHER,
        contentIv: encrypted.contentIv,
        wrappedKey: encrypted.wrappedKey,
        wrapIv: encrypted.wrapIv,
        plaintextSha256: plaintextDigest,
        archiveBytes: encrypted.ciphertext.byteLength,
      }, now);
      completed += 1;
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "export_snapshot_failed";
      await recordPrivacyTaskResult(env.CONTROL_DB, task.id, false, code, now);
    }
  }
  return completed;
}
