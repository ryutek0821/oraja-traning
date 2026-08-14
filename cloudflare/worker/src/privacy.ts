import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";
import { profileScope } from "./upload-protocol";

const DELETE_GRACE_SECONDS = 7 * 24 * 60 * 60;
const EXPORT_RETENTION_SECONDS = 24 * 60 * 60;
const BACKUP_RETENTION_SECONDS = 30 * 24 * 60 * 60;
const EXPORT_SCHEMA_VERSION = "oraja.profile-export.v1";

type Owner = { account_id: string; profile_id: string };
type DueDeletion = Owner & { id: string; status?: "pending" | "confirmed" };
type PrivacyTaskRow = Owner & {
  id: string;
  deletion_id: string | null;
  export_id: string | null;
  task_kind: "export_snapshot" | "do_purge" | "r2_delete";
  target_kind: "profile_do" | "raw_r2" | "artifact_r2" | "export_r2";
  target_key: string;
};

export type PrivacyTask = {
  id: string;
  accountId: string;
  profileId: string;
  deletionId: string | null;
  exportId: string | null;
  taskKind: PrivacyTaskRow["task_kind"];
  targetKind: PrivacyTaskRow["target_kind"];
  targetKey: string;
};

type PurgeEnv = {
  PROFILE_DO: DurableObjectNamespace;
  RAW_BUCKET: R2Bucket;
  ARTIFACT_BUCKET: R2Bucket;
  BACKUP_BUCKET: R2Bucket;
};

function auditInsert(
  db: D1Database,
  owner: Owner,
  eventType: string,
  status: string,
  now: number,
  reasonCode: string | null = null,
) {
  return db.prepare(
    `INSERT INTO audit_events(
       id, account_id, profile_id, actor_kind, event_type, reason_code,
       request_id, resource_hash, input_hash, status, occurred_at
     ) VALUES (?1, ?2, ?3, 'service', ?4, ?5, NULL, NULL, NULL, ?6, ?7)`,
  ).bind(crypto.randomUUID(), owner.account_id, owner.profile_id, eventType, reasonCode, status, now);
}

async function activeOwner(db: D1Database, accountId: string, profileId: string): Promise<Owner> {
  const owner = await db.prepare(
    `SELECT account_id, id AS profile_id FROM profiles
      WHERE account_id = ?1 AND id = ?2 AND status = 'active'`,
  ).bind(accountId, profileId).first<Owner>();
  if (!owner) throw new ApiError("profile_not_active", 409);
  return owner;
}

async function taskStatement(
  db: D1Database,
  owner: Owner,
  operationKey: string,
  deletionId: string | null,
  exportId: string | null,
  taskKind: PrivacyTaskRow["task_kind"],
  targetKind: PrivacyTaskRow["target_kind"],
  targetKey: string,
  now: number,
) {
  return db.prepare(
    `INSERT OR IGNORE INTO privacy_tasks(
       id, operation_key, account_id, profile_id, deletion_id, export_id,
       task_kind, target_kind, target_key, target_hash, status, created_at, updated_at
     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'pending', ?11, ?11)`,
  ).bind(
    crypto.randomUUID(), operationKey, owner.account_id, owner.profile_id,
    deletionId, exportId, taskKind, targetKind, targetKey, await sha256Hex(targetKey), now,
  );
}

export async function requestDataExport(
  db: D1Database,
  accountId: string,
  profileId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const owner = await activeOwner(db, accountId, profileId);
  const id = crypto.randomUUID();
  const objectKey = `profiles/${profileId}/exports/${id}.json`;
  const expiresAt = now + EXPORT_RETENTION_SECONDS;
  await db.batch([
    db.prepare(
      `INSERT INTO exports(id, account_id, profile_id, export_kind, object_key_hash,
         object_sha256, created_at, expires_at, status, schema_version)
       VALUES (?1, ?2, ?3, 'data', ?4, ?5, ?6, ?7, 'queued', ?8)`,
    ).bind(id, accountId, profileId, await sha256Hex(objectKey), "0".repeat(64), now, expiresAt, EXPORT_SCHEMA_VERSION),
    db.prepare(
      `INSERT INTO privacy_export_jobs(
         export_id, account_id, profile_id, state, schema_version, requested_at
       ) VALUES (?1, ?2, ?3, 'queued', ?4, ?5)`,
    ).bind(id, accountId, profileId, EXPORT_SCHEMA_VERSION, now),
    await taskStatement(db, owner, `export:${id}`, null, id, "export_snapshot", "export_r2", objectKey, now),
    auditInsert(db, owner, "privacy.export.requested", "accepted", now),
  ]);
  return { export_id: id, schema_version: EXPORT_SCHEMA_VERSION, status: "queued", expires_at: expiresAt };
}

export async function requestDeletion(
  db: D1Database,
  accountId: string,
  profileId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const owner = await activeOwner(db, accountId, profileId);
  const cancelUntil = now + DELETE_GRACE_SECONDS;
  const id = crypto.randomUUID();
  try {
    await db.batch([
      db.prepare(
        `INSERT INTO deletion_requests(id, account_id, profile_id, requested_at, cancel_until, status)
         VALUES (?1, ?2, ?3, ?4, ?5, 'pending')`,
      ).bind(id, accountId, profileId, now, cancelUntil),
      db.prepare(
        `UPDATE profiles SET status = 'deletion_pending', updated_at = ?1
          WHERE account_id = ?2 AND id = ?3 AND status = 'active'`,
      ).bind(now, accountId, profileId),
      db.prepare(
        `UPDATE accounts SET status = 'deletion_pending', updated_at = ?1
          WHERE id = ?2 AND status = 'active'`,
      ).bind(now, accountId),
      auditInsert(db, owner, "privacy.deletion.requested", "accepted", now),
    ]);
  } catch (error) {
    throw new ApiError(error instanceof Error && error.message.includes("UNIQUE") ? "deletion_already_pending" : "deletion_request_failed", 409);
  }
  return { deletion_id: id, status: "pending", cancel_until: cancelUntil };
}

export async function cancelDeletion(
  db: D1Database,
  accountId: string,
  profileId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const owner = { account_id: accountId, profile_id: profileId };
  const results = await db.batch([
    db.prepare(
      `UPDATE deletion_requests SET status = 'cancelled', cancelled_at = ?1
        WHERE account_id = ?2 AND profile_id = ?3
          AND status = 'pending' AND cancel_until >= ?1`,
    ).bind(now, accountId, profileId),
    db.prepare(
      `UPDATE profiles SET status = 'active', updated_at = ?1
        WHERE account_id = ?2 AND id = ?3 AND status = 'deletion_pending'
          AND EXISTS (SELECT 1 FROM deletion_requests
                       WHERE account_id = ?2 AND profile_id = ?3
                         AND status = 'cancelled' AND cancelled_at = ?1)`,
    ).bind(now, accountId, profileId),
    db.prepare(
      `UPDATE accounts SET status = 'active', updated_at = ?1
        WHERE id = ?2 AND status = 'deletion_pending'
          AND EXISTS (SELECT 1 FROM deletion_requests
                       WHERE account_id = ?2 AND profile_id = ?3
                         AND status = 'cancelled' AND cancelled_at = ?1)`,
    ).bind(now, accountId, profileId),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at)
       SELECT ?1, ?2, ?3, 'service', 'privacy.deletion.cancelled', NULL,
              NULL, NULL, NULL, 'succeeded', ?4
        WHERE EXISTS (SELECT 1 FROM deletion_requests
                       WHERE account_id = ?2 AND profile_id = ?3
                         AND status = 'cancelled' AND cancelled_at = ?4)`,
    ).bind(crypto.randomUUID(), owner.account_id, owner.profile_id, now),
  ]);
  if ((results[0].meta?.changes ?? 0) !== 1) throw new ApiError("deletion_not_cancellable", 409);
  return { status: "cancelled" };
}

async function enqueueDeletionTasks(db: D1Database, row: DueDeletion, now: number): Promise<void> {
  const owner = { account_id: row.account_id, profile_id: row.profile_id };
  const scope = await profileScope(row.profile_id);
  const [raw, artifacts, exports] = await Promise.all([
    db.prepare(
      `SELECT DISTINCT f.object_key FROM upload_files f
         JOIN upload_sessions s ON s.upload_id = f.upload_id
        WHERE s.profile_scope = ?1
       UNION SELECT f.created_object_key FROM upload_files f
         JOIN upload_sessions s ON s.upload_id = f.upload_id
        WHERE s.profile_scope = ?1 AND f.created_object_key IS NOT NULL
       UNION SELECT object_key FROM upload_dedup WHERE profile_scope = ?1`,
    ).bind(scope).all<{ object_key: string }>(),
    db.prepare(
      `SELECT object_key FROM artifact_revisions
        WHERE account_id = ?1 AND profile_id = ?2
       UNION SELECT artifact_key FROM jobs
        WHERE account_id = ?1 AND profile_id = ?2 AND artifact_key IS NOT NULL`,
    ).bind(row.account_id, row.profile_id).all<{ object_key: string }>(),
    db.prepare(
      `SELECT t.target_key AS object_key FROM privacy_tasks t
        WHERE t.account_id = ?1 AND t.profile_id = ?2 AND t.export_id IS NOT NULL`,
    ).bind(row.account_id, row.profile_id).all<{ object_key: string }>(),
  ]);
  const statements: D1PreparedStatement[] = [
    await taskStatement(db, owner, `delete:${row.id}:do`, row.id, null, "do_purge", "profile_do", row.profile_id, now),
  ];
  for (const { object_key: key } of raw.results) {
    statements.push(await taskStatement(db, owner, `delete:${row.id}:raw:${await sha256Hex(key)}`, row.id, null, "r2_delete", "raw_r2", key, now));
  }
  for (const { object_key: key } of artifacts.results) {
    const keys = key.endsWith("/header.json") ? [key, `${key.slice(0, -"header.json".length)}score.json`] : [key];
    for (const objectKey of keys) {
      statements.push(await taskStatement(db, owner, `delete:${row.id}:artifact:${await sha256Hex(objectKey)}`, row.id, null, "r2_delete", "artifact_r2", objectKey, now));
    }
  }
  for (const { object_key: key } of exports.results) {
    statements.push(await taskStatement(db, owner, `delete:${row.id}:export:${await sha256Hex(key)}`, row.id, null, "r2_delete", "export_r2", key, now));
  }
  await db.batch(statements);
}

export async function purgeDueProfiles(
  db: D1Database,
  now = Math.floor(Date.now() / 1000),
): Promise<{ profiles: number; backupsExpireBefore: number; deletionIds: string[] }> {
  const due = await db.prepare(
    `SELECT id, account_id, profile_id, status FROM deletion_requests
      WHERE status IN ('pending', 'confirmed') AND cancel_until < ?1 AND profile_id IS NOT NULL
      ORDER BY cancel_until LIMIT 100`,
  ).bind(now).all<DueDeletion>();
  let claimed = 0;
  for (const row of due.results) {
    if (row.status === "pending") {
      const claim = await db.prepare(
        `UPDATE deletion_requests SET status = 'confirmed', confirmed_at = ?1, started_at = ?1
          WHERE id = ?2 AND status = 'pending' AND cancel_until < ?1`,
      ).bind(now, row.id).run();
      if ((claim.meta?.changes ?? 0) !== 1) continue;
    }
    claimed += 1;
    const deletionHash = await sha256Hex(row.id);
    await db.batch([
      db.prepare("UPDATE sessions SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2").bind(now, row.account_id),
      db.prepare("UPDATE credentials SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2").bind(now, row.account_id),
      db.prepare("UPDATE recovery_codes SET used_at = COALESCE(used_at, ?1) WHERE account_id = ?2").bind(now, row.account_id),
      db.prepare("UPDATE emails SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2").bind(now, row.account_id),
      db.prepare("UPDATE email_tokens SET used_at = COALESCE(used_at, ?1) WHERE email_id IN (SELECT id FROM emails WHERE account_id = ?2)").bind(now, row.account_id),
      db.prepare("UPDATE devices SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2 AND profile_id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare("UPDATE device_token_hashes SET revoked_at = COALESCE(revoked_at, ?1) WHERE device_id IN (SELECT id FROM devices WHERE account_id = ?2 AND profile_id = ?3)").bind(now, row.account_id, row.profile_id),
      db.prepare("UPDATE capability_hashes SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2 AND profile_id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare("UPDATE oauth_authorization_codes SET used_at = COALESCE(used_at, ?1) WHERE account_id = ?2 AND profile_id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare("UPDATE oauth_grants SET revoked_at = COALESCE(revoked_at, ?1), revoke_reason = COALESCE(revoke_reason, 'privacy_deletion') WHERE account_id = ?2 AND profile_id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare("UPDATE oauth_tokens SET revoked_at = COALESCE(revoked_at, ?1) WHERE account_id = ?2 AND profile_id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at)
         SELECT ?1, ?2, ?3, 'service', 'privacy.deletion.started', NULL,
                NULL, ?5, NULL, 'succeeded', ?4
          WHERE NOT EXISTS (SELECT 1 FROM audit_events
                             WHERE account_id = ?2 AND profile_id = ?3
                               AND event_type = 'privacy.deletion.started'
                               AND resource_hash = ?5)`,
      ).bind(crypto.randomUUID(), row.account_id, row.profile_id, now, deletionHash),
    ]);
    await enqueueDeletionTasks(db, row, now);
  }
  return {
    profiles: claimed,
    backupsExpireBefore: now - BACKUP_RETENTION_SECONDS,
    deletionIds: due.results.map((row) => row.id),
  };
}

export async function claimPrivacyTasks(
  db: D1Database,
  now = Math.floor(Date.now() / 1000),
  limit = 25,
  includeExportSnapshots = false,
): Promise<PrivacyTask[]> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new ApiError("invalid_task_limit", 400);
  const candidates = await db.prepare(
    `SELECT id FROM privacy_tasks
      WHERE (status IN ('pending', 'failed') OR (status = 'running' AND lease_until <= ?1))
        AND (next_attempt_at IS NULL OR next_attempt_at <= ?1)
        AND (?3 = 1 OR task_kind <> 'export_snapshot')
      ORDER BY created_at LIMIT ?2`,
  ).bind(now, limit, includeExportSnapshots ? 1 : 0).all<{ id: string }>();
  const claimed: PrivacyTask[] = [];
  for (const candidate of candidates.results) {
    const result = await db.prepare(
      `UPDATE privacy_tasks SET status = 'running', attempts = attempts + 1,
          lease_until = ?1, updated_at = ?2
        WHERE id = ?3 AND (status IN ('pending', 'failed') OR (status = 'running' AND lease_until <= ?2))`,
    ).bind(now + 90, now, candidate.id).run();
    if ((result.meta?.changes ?? 0) !== 1) continue;
    const row = await db.prepare(
      `SELECT id, account_id, profile_id, deletion_id, export_id,
              task_kind, target_kind, target_key
         FROM privacy_tasks WHERE id = ?1 AND status = 'running'`,
    ).bind(candidate.id).first<PrivacyTaskRow>();
    if (row) {
      if (row.task_kind === "export_snapshot" && row.export_id) {
        await db.prepare(
          `UPDATE privacy_export_jobs SET state = 'snapshotting', started_at = COALESCE(started_at, ?1)
            WHERE export_id = ?2 AND state = 'queued'`,
        ).bind(now, row.export_id).run();
      }
      claimed.push({
        id: row.id, accountId: row.account_id, profileId: row.profile_id,
        deletionId: row.deletion_id, exportId: row.export_id,
        taskKind: row.task_kind, targetKind: row.target_kind, targetKey: row.target_key,
      });
    }
  }
  return claimed;
}

export async function runPurgeTask(task: PrivacyTask, env: PurgeEnv): Promise<void> {
  if (task.taskKind === "export_snapshot") throw new ApiError("export_processor_required", 503);
  if (task.targetKind === "profile_do") {
    const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(task.profileId));
    const response = await stub.fetch("https://profile.internal/internal/privacy/purge", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ profile_internal_id: task.profileId, deletion_id: task.deletionId }),
    });
    if (!response.ok) throw new ApiError("profile_do_purge_failed", 503);
    return;
  }
  const bucket = task.targetKind === "raw_r2" ? env.RAW_BUCKET
    : task.targetKind === "artifact_r2" ? env.ARTIFACT_BUCKET : env.BACKUP_BUCKET;
  await bucket.delete(task.targetKey);
  if (await bucket.head(task.targetKey)) throw new ApiError("r2_purge_unverified", 503);
}

export async function recordPrivacyTaskResult(
  db: D1Database,
  taskId: string,
  succeeded: boolean,
  errorCode: string | null,
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  if (succeeded) {
    const task = await db.prepare("SELECT task_kind FROM privacy_tasks WHERE id = ?1").bind(taskId)
      .first<{ task_kind: PrivacyTaskRow["task_kind"] }>();
    if (task?.task_kind === "export_snapshot") throw new ApiError("export_digest_required", 409);
  }
  const result = await db.prepare(
    `UPDATE privacy_tasks
        SET status = ?1, completed_at = CASE WHEN ?1 = 'succeeded' THEN ?2 ELSE NULL END,
            lease_until = NULL, next_attempt_at = CASE WHEN ?1 = 'failed' THEN ?2 + 60 ELSE NULL END,
            last_error_code = CASE WHEN ?1 = 'failed' THEN ?3 ELSE NULL END, updated_at = ?2
      WHERE id = ?4 AND status = 'running'`,
  ).bind(succeeded ? "succeeded" : "failed", now, errorCode, taskId).run();
  if ((result.meta?.changes ?? 0) !== 1) throw new ApiError("privacy_task_lease_lost", 409);
}

export async function completeExportTask(
  db: D1Database,
  taskId: string,
  archiveSha256: string,
  now = Math.floor(Date.now() / 1000),
): Promise<void> {
  if (!/^[0-9a-f]{64}$/.test(archiveSha256)) throw new ApiError("invalid_export_digest", 400);
  const task = await db.prepare(
    `SELECT id, account_id, profile_id, export_id, task_kind
       FROM privacy_tasks
      WHERE id = ?1 AND status = 'running' AND export_id IS NOT NULL`,
  ).bind(taskId).first<PrivacyTaskRow>();
  if (!task || task.task_kind !== "export_snapshot" || !task.export_id) {
    throw new ApiError("privacy_task_lease_lost", 409);
  }
  const owner = { account_id: task.account_id, profile_id: task.profile_id };
  const results = await db.batch([
    db.prepare(
      `UPDATE privacy_tasks SET status = 'succeeded', completed_at = ?1,
          lease_until = NULL, next_attempt_at = NULL, last_error_code = NULL, updated_at = ?1
        WHERE id = ?2 AND status = 'running'
          AND EXISTS (SELECT 1 FROM exports
                       WHERE id = ?3 AND status = 'queued' AND expires_at > ?1)`,
    ).bind(now, taskId, task.export_id),
    db.prepare(
      `UPDATE privacy_export_jobs SET state = 'ready', completed_at = ?1,
          archive_sha256 = ?2, failure_code = NULL
        WHERE export_id = ?3 AND state IN ('queued', 'snapshotting')
          AND EXISTS (SELECT 1 FROM privacy_tasks
                       WHERE id = ?4 AND status = 'succeeded' AND completed_at = ?1)`,
    ).bind(now, archiveSha256, task.export_id, taskId),
    db.prepare(
      `UPDATE exports SET status = 'ready', object_sha256 = ?1, ready_at = ?2
        WHERE id = ?3 AND status = 'queued' AND expires_at > ?2
          AND EXISTS (SELECT 1 FROM privacy_export_jobs
                       WHERE export_id = ?3 AND state = 'ready' AND archive_sha256 = ?1)`,
    ).bind(archiveSha256, now, task.export_id),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at)
       SELECT ?1, ?2, ?3, 'service', 'privacy.export.ready', NULL,
              NULL, NULL, NULL, 'succeeded', ?4
        WHERE EXISTS (SELECT 1 FROM exports
                       WHERE id = ?5 AND status = 'ready' AND object_sha256 = ?6)`,
    ).bind(crypto.randomUUID(), owner.account_id, owner.profile_id, now, task.export_id, archiveSha256),
  ]);
  if (results.slice(0, 3).some((result) => (result.meta?.changes ?? 0) !== 1)) {
    throw new ApiError("export_state_conflict", 409);
  }
}

export async function claimExportDownload(
  db: D1Database,
  accountId: string,
  profileId: string,
  exportId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<{ targetKey: string }> {
  const task = await db.prepare(
    `SELECT t.target_key FROM privacy_tasks t
       JOIN exports e ON e.id = t.export_id
      WHERE e.id = ?1 AND e.account_id = ?2 AND e.profile_id = ?3
        AND e.status = 'ready' AND e.downloaded_at IS NULL AND e.expires_at > ?4
        AND t.task_kind = 'export_snapshot' AND t.status = 'succeeded'`,
  ).bind(exportId, accountId, profileId, now).first<{ target_key: string }>();
  if (!task) throw new ApiError("export_not_downloadable", 409);
  const result = await db.prepare(
    `UPDATE exports SET downloaded_at = ?1
      WHERE id = ?2 AND account_id = ?3 AND profile_id = ?4
        AND status = 'ready' AND downloaded_at IS NULL AND expires_at > ?1`,
  ).bind(now, exportId, accountId, profileId).run();
  if ((result.meta?.changes ?? 0) !== 1) throw new ApiError("export_not_downloadable", 409);
  return { targetKey: task.target_key };
}

export async function expireDataExports(
  db: D1Database,
  now = Math.floor(Date.now() / 1000),
): Promise<number> {
  const result = await db.batch([
    db.prepare("UPDATE exports SET status = 'expired' WHERE status IN ('queued', 'ready') AND expires_at <= ?1").bind(now),
    db.prepare(
      `UPDATE privacy_export_jobs SET state = 'expired'
        WHERE export_id IN (SELECT id FROM exports WHERE status = 'expired')
          AND state IN ('queued', 'snapshotting', 'ready')`,
    ),
  ]);
  const expired = await db.prepare(
    `SELECT e.id, e.account_id, e.profile_id, t.target_key
       FROM exports e JOIN privacy_tasks t ON t.export_id = e.id
      WHERE e.status = 'expired' AND t.task_kind = 'export_snapshot'`,
  ).all<{ id: string; account_id: string; profile_id: string; target_key: string }>();
  for (const row of expired.results) {
    await (await taskStatement(
      db,
      { account_id: row.account_id, profile_id: row.profile_id },
      `expire-export:${row.id}`,
      null,
      row.id,
      "r2_delete",
      "export_r2",
      row.target_key,
      now,
    )).run();
  }
  return result[0].meta?.changes ?? 0;
}

export async function finalizeDeletion(
  db: D1Database,
  deletionId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<boolean> {
  const deletion = await db.prepare(
    `SELECT id, account_id, profile_id FROM deletion_requests
      WHERE id = ?1 AND status = 'confirmed' AND profile_id IS NOT NULL`,
  ).bind(deletionId).first<DueDeletion>();
  if (!deletion) return false;
  // Re-inventory immediately before completion. A workflow that was already
  // running when deletion started may have published an additional artifact.
  await enqueueDeletionTasks(db, deletion, now);
  const counts = await db.prepare(
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded
       FROM privacy_tasks WHERE deletion_id = ?1`,
  ).bind(deletionId).first<{ total: number; succeeded: number }>();
  if (!counts || counts.total < 1 || counts.succeeded !== counts.total) return false;
  const verifiedTasks = await db.prepare(
    `SELECT id, target_hash FROM privacy_tasks
      WHERE deletion_id = ?1 AND status = 'succeeded' ORDER BY operation_key`,
  ).bind(deletionId).all<{ id: string; target_hash: string }>();
  if (verifiedTasks.results.length !== counts.total) return false;
  const digest = await sha256Hex(JSON.stringify(verifiedTasks.results));
  const scope = await profileScope(deletion.profile_id);
  await db.batch([
    db.prepare("DELETE FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM artifact_revisions WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM upload_dedup WHERE profile_scope = ?1").bind(scope),
    db.prepare("DELETE FROM upload_profile_envelopes WHERE profile_scope = ?1").bind(scope),
    db.prepare("DELETE FROM upload_sessions WHERE profile_scope = ?1").bind(scope),
    db.prepare("DELETE FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM oauth_authorization_codes WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM oauth_tokens WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM oauth_grants WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM capability_hashes WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("DELETE FROM devices WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    // Immutable job/audit/consent records are retained as non-authenticating
    // tombstones by contract; mutable credentials and export metadata are not.
    db.prepare("DELETE FROM sessions WHERE account_id = ?1").bind(deletion.account_id),
    db.prepare("DELETE FROM recovery_codes WHERE account_id = ?1").bind(deletion.account_id),
    db.prepare("DELETE FROM credentials WHERE account_id = ?1").bind(deletion.account_id),
    db.prepare("DELETE FROM email_tokens WHERE email_id IN (SELECT id FROM emails WHERE account_id = ?1)").bind(deletion.account_id),
    db.prepare("DELETE FROM emails WHERE account_id = ?1").bind(deletion.account_id),
    db.prepare("DELETE FROM exports WHERE account_id = ?1 AND profile_id = ?2").bind(deletion.account_id, deletion.profile_id),
    db.prepare("UPDATE profiles SET status = 'deleted', display_name = 'Deleted profile', deleted_at = ?1, updated_at = ?1 WHERE account_id = ?2 AND id = ?3 AND status = 'deletion_pending'").bind(now, deletion.account_id, deletion.profile_id),
    db.prepare("UPDATE accounts SET status = 'deleted', username_normalized = 'deleted:' || id, updated_at = ?1 WHERE id = ?2 AND status = 'deletion_pending'").bind(now, deletion.account_id),
    db.prepare("UPDATE deletion_requests SET status = 'completed', completed_at = ?1 WHERE id = ?2 AND status = 'confirmed'").bind(now, deletion.id),
    db.prepare(
      `INSERT INTO privacy_completion_audits(
         id, deletion_id, account_id, profile_id, task_count, verification_digest, completed_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
    ).bind(crypto.randomUUID(), deletion.id, deletion.account_id, deletion.profile_id, counts.total, digest, now),
    auditInsert(db, deletion, "privacy.deletion.completed", "succeeded", now),
  ]);
  return true;
}
