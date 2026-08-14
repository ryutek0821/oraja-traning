import { ApiError } from "./ir-api";
import { uuidV7 } from "./job-ledger";

export type TableCapabilityKind = "recommend" | "today";

type ProfileSettingsRow = {
  profile_id: string;
  timezone: string;
  target_judged: number | null;
  reserve_judged: number | null;
  readiness: string | null;
  settings_revision: number | null;
  updated_at: number;
};

const DEFAULT_TARGET = 100_000;
const DEFAULT_RESERVE = 10_000;

function integer(value: unknown, name: string, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new ApiError(`invalid_${name}`, 400);
  }
  return value;
}

function timezone(value: unknown): string {
  if (typeof value !== "string" || value.length < 1 || value.length > 64) throw new ApiError("invalid_timezone", 400);
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format(new Date(0));
  } catch {
    throw new ApiError("invalid_timezone", 400);
  }
  return value;
}

function readiness(value: unknown): "normal" | "tired" {
  if (value !== "normal" && value !== "tired") throw new ApiError("invalid_readiness", 400);
  return value;
}

async function ownedProfile(db: D1Database, accountId: string): Promise<ProfileSettingsRow> {
  const row = await db.prepare(
    `SELECT p.id AS profile_id, p.timezone,
            s.target_judged, s.reserve_judged, s.readiness,
            s.settings_revision, COALESCE(s.updated_at, p.updated_at) AS updated_at
       FROM profiles p
       LEFT JOIN profile_settings s
         ON s.account_id = p.account_id AND s.profile_id = p.id
      WHERE p.account_id = ?1 AND p.status = 'active'
      ORDER BY p.created_at LIMIT 1`,
  ).bind(accountId).first<ProfileSettingsRow>();
  if (!row) throw new ApiError("profile_not_found", 404);
  return row;
}

function publicSettings(row: ProfileSettingsRow): Record<string, unknown> {
  return {
    timezone: row.timezone,
    target_judged: row.target_judged ?? DEFAULT_TARGET,
    reserve_judged: row.reserve_judged ?? DEFAULT_RESERVE,
    readiness: row.readiness ?? "normal",
    revision: row.settings_revision ?? 1,
    updated_at: row.updated_at,
  };
}

export async function readProfileSettings(db: D1Database, accountId: string): Promise<Record<string, unknown>> {
  return publicSettings(await ownedProfile(db, accountId));
}

export async function updateProfileSettings(
  db: D1Database,
  accountId: string,
  input: Record<string, unknown>,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const allowed = new Set(["timezone", "target_judged", "reserve_judged", "readiness"]);
  if (Object.keys(input).length === 0 || Object.keys(input).some((key) => !allowed.has(key))) {
    throw new ApiError("invalid_settings", 400);
  }
  const current = await ownedProfile(db, accountId);
  const nextTimezone = input.timezone === undefined ? current.timezone : timezone(input.timezone);
  const nextTarget = input.target_judged === undefined
    ? current.target_judged ?? DEFAULT_TARGET
    : integer(input.target_judged, "target_judged", 10_000, 1_000_000);
  const nextReserve = input.reserve_judged === undefined
    ? current.reserve_judged ?? DEFAULT_RESERVE
    : integer(input.reserve_judged, "reserve_judged", 0, 500_000);
  const nextReadiness = input.readiness === undefined
    ? readiness(current.readiness ?? "normal")
    : readiness(input.readiness);
  if (nextReserve > nextTarget) throw new ApiError("invalid_reserve_judged", 400);
  await db.batch([
    db.prepare(
      `UPDATE profiles SET timezone = ?1, updated_at = ?2
        WHERE account_id = ?3 AND id = ?4 AND status = 'active'`,
    ).bind(nextTimezone, now, accountId, current.profile_id),
    db.prepare(
      `INSERT INTO profile_settings(
         account_id, profile_id, target_judged, reserve_judged,
         readiness, settings_revision, updated_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6)
       ON CONFLICT(account_id, profile_id) DO UPDATE SET
         target_judged = excluded.target_judged,
         reserve_judged = excluded.reserve_judged,
         readiness = excluded.readiness,
         settings_revision = profile_settings.settings_revision + 1,
         updated_at = excluded.updated_at`,
    ).bind(accountId, current.profile_id, nextTarget, nextReserve, nextReadiness, now),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type,
         reason_code, request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, ?3, 'account', 'profile.settings.updated', NULL,
                 NULL, NULL, NULL, 'succeeded', ?4)`,
    ).bind(uuidV7(), accountId, current.profile_id, now),
  ]);
  return readProfileSettings(db, accountId);
}

function randomSecret(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function secretHash(secret: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function capabilityKind(value: string): TableCapabilityKind {
  if (value !== "recommend" && value !== "today") throw new ApiError("invalid_capability_kind", 400);
  return value;
}

export async function listTableCapabilities(db: D1Database, accountId: string): Promise<Record<string, unknown>[]> {
  const profile = await ownedProfile(db, accountId);
  const rows = await db.prepare(
    `SELECT capability_kind, created_at, last_used_at, revoked_at
       FROM capability_hashes
      WHERE account_id = ?1 AND profile_id = ?2
        AND capability_kind IN ('recommend', 'today')
      ORDER BY capability_kind, created_at DESC`,
  ).bind(accountId, profile.profile_id).all<Record<string, unknown>>();
  return rows.results.map((row) => ({
    kind: row.capability_kind,
    active: row.revoked_at === null,
    created_at: row.created_at,
    last_used_at: row.last_used_at,
    revoked_at: row.revoked_at,
  }));
}

export async function rotateTableCapability(
  db: D1Database,
  accountId: string,
  rawKind: string,
  publicOrigin: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const kind = capabilityKind(rawKind);
  const profile = await ownedProfile(db, accountId);
  const secret = randomSecret();
  const hash = await secretHash(secret);
  const id = uuidV7();
  await db.batch([
    db.prepare(
      `UPDATE capability_hashes SET revoked_at = COALESCE(revoked_at, ?1)
        WHERE account_id = ?2 AND profile_id = ?3
          AND capability_kind = ?4 AND revoked_at IS NULL`,
    ).bind(now, accountId, profile.profile_id, kind),
    db.prepare(
      `INSERT INTO capability_hashes(
         id, account_id, profile_id, capability_kind, secret_hash,
         created_at, expires_at, revoked_at, last_used_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL, NULL)`,
    ).bind(id, accountId, profile.profile_id, kind, hash, now),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type,
         reason_code, request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, ?3, 'account', 'capability.rotated', ?4,
                 NULL, ?5, NULL, 'succeeded', ?6)`,
    ).bind(uuidV7(), accountId, profile.profile_id, kind, hash, now),
  ]);
  const origin = new URL(publicOrigin).origin;
  return {
    kind,
    capability_url: `${origin}/t/${secret}/${kind}/header.json`,
    displayed_once: true,
    created_at: now,
  };
}

export async function revokeTableCapability(
  db: D1Database,
  accountId: string,
  rawKind: string,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  const kind = capabilityKind(rawKind);
  const profile = await ownedProfile(db, accountId);
  const result = await db.prepare(
    `UPDATE capability_hashes SET revoked_at = ?1
      WHERE account_id = ?2 AND profile_id = ?3
        AND capability_kind = ?4 AND revoked_at IS NULL`,
  ).bind(now, accountId, profile.profile_id, kind).run();
  if ((result.meta?.changes ?? 0) < 1) throw new ApiError("capability_not_found", 404);
  await db.prepare(
    `INSERT INTO audit_events(
       id, account_id, profile_id, actor_kind, event_type,
       reason_code, request_id, resource_hash, input_hash, status, occurred_at
     ) VALUES (?1, ?2, ?3, 'account', 'capability.revoked', ?4,
               NULL, NULL, NULL, 'succeeded', ?5)`,
  ).bind(uuidV7(), accountId, profile.profile_id, kind, now).run();
  return { kind, revoked: true, revoked_at: now };
}
