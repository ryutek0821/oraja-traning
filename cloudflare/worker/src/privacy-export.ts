import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";
import { claimPrivacyTasks, completeExportTask, recordPrivacyTaskResult, type PrivacyTask } from "./privacy";
import type { Env } from "./index";

const EXPORT_SCHEMA = "oraja.profile-export.v1";
const MAX_EXPORT_BYTES = 16 * 1024 * 1024;
const MAX_EXPORT_PLAYS = 50_000;

async function exportedPlays(env: Env, profileId: string): Promise<unknown[]> {
  const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(profileId));
  const result: unknown[] = [];
  let cursor: string | null = null;
  do {
    const query = new URLSearchParams({ limit: "500" });
    if (cursor) query.set("cursor", cursor);
    const response = await stub.fetch(`https://profile.internal/internal/privacy/export?${query}`);
    if (!response.ok) throw new ApiError("profile_export_failed", 503);
    const page = await response.json<{ plays?: unknown[]; next_cursor?: unknown }>();
    if (!Array.isArray(page.plays)) throw new ApiError("profile_export_invalid", 503);
    result.push(...page.plays);
    if (result.length > MAX_EXPORT_PLAYS) throw new ApiError("profile_export_too_large", 413);
    cursor = typeof page.next_cursor === "string" ? page.next_cursor : null;
  } while (cursor);
  return result;
}

async function buildExport(task: PrivacyTask, env: Env, now: number): Promise<string> {
  const [profile, devices, consents, jobs, artifacts, proposals, plays] = await Promise.all([
    env.CONTROL_DB.prepare(
      "SELECT public_id, display_name, timezone, status, created_at, updated_at FROM profiles WHERE account_id = ?1 AND id = ?2 AND status <> 'deleted'",
    ).bind(task.accountId, task.profileId).first<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT public_id, label, created_at, revoked_at FROM devices WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at",
    ).bind(task.accountId, task.profileId).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT scope, terms_version, granted_at, revoked_at FROM consents WHERE account_id = ?1 AND profile_id = ?2 ORDER BY granted_at",
    ).bind(task.accountId, task.profileId).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT id AS job_id, kind, status, revision, created_at, updated_at, terminal_reason FROM jobs WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at",
    ).bind(task.accountId, task.profileId).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT table_kind, revision, content_hash, updated_at FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2 ORDER BY table_kind",
    ).bind(task.accountId, task.profileId).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT id, provider_name, title, proposal_hash, payload_json, status, created_at, decided_at, decision_reason FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at",
    ).bind(task.accountId, task.profileId).all<Record<string, unknown>>(),
    exportedPlays(env, task.profileId),
  ]);
  if (!profile) throw new ApiError("profile_not_found", 404);
  const value = {
    contract: EXPORT_SCHEMA,
    schema_version: "1",
    exported_at: new Date(now * 1000).toISOString(),
    profile,
    devices: devices.results,
    consents: consents.results,
    jobs: jobs.results,
    artifacts: artifacts.results,
    advisor_proposals: proposals.results.map((row) => ({
      ...row,
      payload: (() => { try { return JSON.parse(String(row.payload_json)); } catch { return { invalid: true }; } })(),
      payload_json: undefined,
    })),
    plays,
    exclusions: ["credential_hashes", "oauth_tokens", "device_tokens", "storage_object_keys", "raw_replay", "conversation_text"],
  };
  const serialized = JSON.stringify(value) + "\n";
  if (new TextEncoder().encode(serialized).byteLength > MAX_EXPORT_BYTES) throw new ApiError("profile_export_too_large", 413);
  return serialized;
}

export async function processExportSnapshots(env: Env, now: number, limit = 5): Promise<number> {
  const tasks = await claimPrivacyTasks(env.CONTROL_DB, now, limit, "export");
  let completed = 0;
  for (const task of tasks) {
    try {
      const body = await buildExport(task, env, now);
      const digest = await sha256Hex(body);
      await env.BACKUP_BUCKET.put(task.targetKey, body, {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        customMetadata: { schema: EXPORT_SCHEMA, sha256: digest },
      });
      const stored = await env.BACKUP_BUCKET.head(task.targetKey);
      if (!stored || stored.customMetadata?.sha256 !== digest) throw new ApiError("export_write_unverified", 503);
      await completeExportTask(env.CONTROL_DB, task.id, digest, now);
      completed += 1;
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "export_snapshot_failed";
      await recordPrivacyTaskResult(env.CONTROL_DB, task.id, false, code, now);
    }
  }
  return completed;
}
