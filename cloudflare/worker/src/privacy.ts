import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";

const DELETE_GRACE_SECONDS = 7 * 24 * 60 * 60;
const BACKUP_RETENTION_SECONDS = 30 * 24 * 60 * 60;

export async function requestDataExport(
  db: D1Database,
  accountId: string,
  profileId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const id = crypto.randomUUID();
  const objectKey = `profiles/${profileId}/exports/${id}.json`;
  await db
    .prepare(
      `INSERT INTO exports(id, account_id, profile_id, export_kind, object_key_hash,
         object_sha256, created_at, expires_at, status)
       VALUES (?1, ?2, ?3, 'data', ?4, ?5, ?6, ?7, 'queued')`,
    )
    .bind(id, accountId, profileId, await sha256Hex(objectKey), await sha256Hex(`${accountId}:${profileId}:${now}`), now, now + 24 * 60 * 60)
    .run();
  return { export_id: id, status: "queued", expires_at: now + 24 * 60 * 60 };
}

export async function requestDeletion(
  db: D1Database,
  accountId: string,
  profileId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const cancelUntil = now + DELETE_GRACE_SECONDS;
  const id = crypto.randomUUID();
  try {
    await db
      .prepare(
        `INSERT INTO deletion_requests(id, account_id, profile_id, requested_at, cancel_until, status)
         VALUES (?1, ?2, ?3, ?4, ?5, 'pending')`,
      )
      .bind(id, accountId, profileId, now, cancelUntil)
      .run();
    await db
      .prepare("UPDATE profiles SET status = 'deletion_pending', updated_at = ?1 WHERE account_id = ?2 AND id = ?3 AND status <> 'deleted'")
      .bind(now, accountId, profileId)
      .run();
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
  const result = await db
    .prepare(
      `UPDATE deletion_requests SET status = 'cancelled', cancelled_at = ?1
        WHERE account_id = ?2 AND profile_id = ?3 AND status = 'pending' AND cancel_until >= ?1`,
    )
    .bind(now, accountId, profileId)
    .run();
  if ((result.meta?.changes ?? 0) !== 1) throw new ApiError("deletion_not_cancellable", 409);
  await db
    .prepare("UPDATE profiles SET status = 'active', updated_at = ?1 WHERE account_id = ?2 AND id = ?3 AND status = 'deletion_pending'")
    .bind(now, accountId, profileId)
    .run();
  return { status: "cancelled" };
}

export async function purgeDueProfiles(
  db: D1Database,
  now = Math.floor(Date.now() / 1000),
): Promise<{ profiles: number; backupsExpireBefore: number }> {
  const due = await db
    .prepare("SELECT id, account_id, profile_id FROM deletion_requests WHERE status = 'pending' AND cancel_until < ?1 LIMIT 100")
    .bind(now)
    .all<{ id: string; account_id: string; profile_id: string }>();
  for (const row of due.results) {
    await db.batch([
      db.prepare("UPDATE deletion_requests SET status = 'completed', confirmed_at = ?1 WHERE id = ?2").bind(now, row.id),
      db.prepare("UPDATE profiles SET status = 'deleted', deleted_at = ?1, updated_at = ?1 WHERE account_id = ?2 AND id = ?3").bind(now, row.account_id, row.profile_id),
      db.prepare("DELETE FROM oauth_tokens WHERE account_id = ?1 AND profile_id = ?2").bind(row.account_id, row.profile_id),
      db.prepare("DELETE FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2").bind(row.account_id, row.profile_id),
    ]);
  }
  return { profiles: due.results.length, backupsExpireBefore: now - BACKUP_RETENTION_SECONDS };
}
