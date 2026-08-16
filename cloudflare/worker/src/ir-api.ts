import { AuthError, sha256Hex } from "./auth";

export const IR_POLICY = {
  maxBodyBytes: 64 * 1024,
  maxPastSeconds: 7 * 24 * 60 * 60,
  maxFutureSeconds: 10 * 60,
  rateLimitWindowSeconds: 60,
  ipRateLimit: 120,
  tokenRateLimit: 240,
} as const;

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UUID_V7_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const MD5_PATTERN = /^[0-9a-f]{32}$/;
const TOKEN_PATTERN = /^ot_[A-Za-z0-9_-]{43}$/;
const CLIENT_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}$/;
const DATE_TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;
const DEVICE_SCOPE = "plays:write";

const EVENT_KEYS = new Set([
  "contract",
  "schema_version",
  "event_id",
  "profile_id",
  "device_id",
  "occurred_at",
  "client_version",
  "game_mode",
  "rule",
  "chart",
  "play",
  "is_course",
  "course_id",
  "provenance",
]);
const CHART_KEYS = new Set(["sha256", "hash", "md5", "title", "artist", "mode", "bpm", "notes", "ln_mode"]);
const PLAY_KEYS = new Set([
  "clear",
  "gauge_kind",
  "assist_kind",
  "notes",
  "passnotes",
  "minbp",
  "ex_score",
  "combo",
  "options",
  "seed",
  "random",
  "judgements",
]);
const JUDGEMENT_KEYS = new Set([
  "epg",
  "lpg",
  "egr",
  "lgr",
  "egd",
  "lgd",
  "ebd",
  "lbd",
  "epr",
  "lpr",
  "ems",
  "lms",
]);
const PROVENANCE_KEYS = new Set(["trust_domain", "source", "aggregate_eligible"]);
const OPTION_VALUES = new Set([
  "NORMAL",
  "MIRROR",
  "RANDOM",
  "R-RANDOM",
  "S-RANDOM",
  "H-RANDOM",
  "SPIRAL",
  "ALL-SCR",
  "EX-RANDOM",
  "EX-S-RANDOM",
  "BATTLE",
  "BATTLE-ASSIST",
]);
const GAUGE_VALUES = new Set(["ASSIST_EASY", "EASY", "NORMAL", "HARD", "EXHARD", "HAZARD"]);
const ASSIST_VALUES = new Set(["NONE", "ASSIST_EASY", "LIGHT_ASSIST_EASY"]);

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 400,
    public readonly retryable = false,
    public readonly retryAfterSeconds?: number,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

export type DeviceIdentity = {
  accountId: string;
  profileId: string;
  profilePublicId: string;
  deviceId: string;
  devicePublicId: string;
  scopes: string[];
  tokenHash: string;
};

export type DeviceView = {
  device_id: string;
  profile_id: string;
  label: string;
  scopes: string[];
  token_suffix: string;
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
};

export type DeviceTokenIssue = DeviceView & {
  token: string;
};

export type NormalizedChart = {
  sha256: string;
  md5?: string;
  title?: string;
  artist?: string;
  mode?: string;
  bpm?: number;
  notes?: number;
  ln_mode?: number;
};

export type NormalizedPlay = {
  clear: number;
  gauge_kind: string;
  assist_kind: string;
  notes: number;
  passnotes: number;
  minbp: number;
  ex_score: number;
  combo: number;
  options: string[];
  seed: number;
  random: number;
  judgements: Record<string, number>;
};

export type NormalizedIrEvent = {
  contract: "ir-event";
  schema_version: "1";
  event_id: string;
  profile_id: string;
  device_id: string;
  occurred_at: string;
  client_version?: string;
  game_mode: "SP7";
  rule: "BEATORAJA-SP7";
  chart: NormalizedChart;
  play: NormalizedPlay;
  is_course: boolean;
  course_id?: string;
  provenance: {
    trust_domain: "official";
    source: "official_ir";
    aggregate_eligible: false;
  };
};

export type PlayAck = {
  status: "accepted" | "duplicate";
  event_id: string;
  idempotency_key: string;
  job_id: string;
  revision: number;
  retryable: false;
  enqueue_required: boolean;
};

export const IR_READ_PATHS = new Set([
  "/v1/ir/player",
  "/v1/ir/rivals",
  "/v1/ir/tables",
  "/v1/ir/play-data",
  "/v1/ir/course-play-data",
  "/v1/ir/version",
  "/v1/ir/illegal-songs",
]);

type IrReadDependencies = {
  db: D1Database;
  profileDo: DurableObjectNamespace;
  buildVersion: string;
};

type ProfileRow = {
  id: string;
  public_id: string;
  account_id: string;
  status: string;
};

type DeviceRow = {
  id: string;
  public_id: string;
  account_id: string;
  profile_id: string;
  profile_public_id: string;
  label: string;
  token_id: string;
  token_hash: string;
  scopes: string;
  token_suffix: string;
  token_created_at: number;
  last_used_at: number | null;
  device_revoked_at: number | null;
  token_revoked_at: number | null;
  device_created_at: number;
};

type SessionAccountRow = {
  account_id: string;
};

type RateLimitRow = {
  window_started_at: number;
  request_count: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertObject(value: unknown, code = "invalid_contract"): Record<string, unknown> {
  if (!isRecord(value)) throw new ApiError(code, 400);
  return value;
}

function assertExactKeys(value: Record<string, unknown>, allowed: Set<string>, code = "disallowed_field"): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new ApiError(code, 422);
  }
}

function assertRequired(value: Record<string, unknown>, required: string[]): void {
  for (const key of required) {
    if (!(key in value)) throw new ApiError("invalid_contract", 400);
  }
}

function stringValue(value: unknown, maxLength: number, field: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new ApiError("invalid_contract", 400);
  }
  if ([...value].some((character) => /\p{Cc}/u.test(character))) {
    throw new ApiError("invalid_contract", 400);
  }
  return value;
}

function integerValue(value: unknown, minimum: number, maximum: number, field: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new ApiError("invalid_contract", 400);
  }
  return value as number;
}

function optionalInteger(
  source: Record<string, unknown>,
  key: string,
  minimum: number,
  maximum: number,
): number | undefined {
  if (!(key in source)) return undefined;
  return integerValue(source[key], minimum, maximum, key);
}

function uuidValue(value: unknown, version7: boolean, field: string): string {
  if (typeof value !== "string" || !(version7 ? UUID_V7_PATTERN : UUID_PATTERN).test(value)) {
    throw new ApiError("invalid_contract", 400);
  }
  return value;
}

function safeScopes(encoded: string): string[] {
  try {
    const parsed: unknown = JSON.parse(encoded);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((scope): scope is string => typeof scope === "string");
  } catch {
    return [];
  }
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export function randomUuidV7(nowMilliseconds = Date.now()): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const timestamp = BigInt(Math.max(0, Math.floor(nowMilliseconds)));
  for (let index = 5; index >= 0; index -= 1) {
    bytes[index] = Number(timestamp >> BigInt((5 - index) * 8) & 0xffn);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function newDeviceToken(): string {
  return `ot_${base64Url(crypto.getRandomValues(new Uint8Array(32)))}`;
}

export async function hashDeviceToken(token: string, pepper: string): Promise<string> {
  if (!TOKEN_PATTERN.test(token) || pepper.length === 0) throw new ApiError("unauthorized", 401);
  return sha256Hex(`${pepper}\u0000${token}`);
}

function validateLabel(value: unknown): string {
  if (typeof value !== "string") throw new ApiError("invalid_label", 400);
  const label = value.trim();
  if (label.length < 1 || label.length > 80 || [...label].some((character) => /\p{Cc}/u.test(character))) {
    throw new ApiError("invalid_label", 400);
  }
  return label;
}

function nowSeconds(now?: number): number {
  return now ?? Math.floor(Date.now() / 1000);
}

function resultChanges(result: D1Result<unknown>): number {
  return result.meta?.changes ?? 0;
}

async function ownedProfile(
  db: D1Database,
  accountId: string,
  profilePublicId: string,
): Promise<ProfileRow> {
  const profile = await db
    .prepare(
      `SELECT id, public_id, account_id, status
         FROM profiles
        WHERE account_id = ?1 AND public_id = ?2 AND status = 'active'`,
    )
    .bind(accountId, profilePublicId)
    .first<ProfileRow>();
  if (!profile) throw new ApiError("not_found", 404);
  return profile;
}

export async function sessionAccount(
  db: D1Database,
  sessionToken: string | null | undefined,
  now = nowSeconds(),
): Promise<string> {
  if (!sessionToken || sessionToken.length < 32) throw new ApiError("unauthorized", 401);
  const row = await db
    .prepare(
      `SELECT s.account_id
         FROM sessions s
         JOIN accounts a ON a.id = s.account_id
        WHERE s.session_hash = ?1
          AND s.revoked_at IS NULL
          AND s.expires_at > ?2
          AND a.status = 'active'`,
    )
    .bind(await sha256Hex(sessionToken), now)
    .first<SessionAccountRow>();
  if (!row) throw new ApiError("unauthorized", 401);
  return row.account_id;
}

export async function issueDeviceToken(
  db: D1Database,
  accountId: string,
  profilePublicId: string,
  labelInput: unknown,
  pepper: string,
  options: { now?: number; requestId?: string } = {},
): Promise<DeviceTokenIssue> {
  const profile = await ownedProfile(db, accountId, profilePublicId);
  const label = validateLabel(labelInput);
  if (pepper.length === 0) throw new ApiError("temporary_unavailable", 503, true, 5);
  const now = nowSeconds(options.now);
  const deviceId = randomUuidV7();
  const devicePublicId = randomUuidV7();
  const token = newDeviceToken();
  const tokenHash = await hashDeviceToken(token, pepper);
  const tokenSuffix = token.slice(-8);
  const scopes = JSON.stringify([DEVICE_SCOPE]);

  await db.batch([
    db
      .prepare(
        `INSERT INTO devices(
           id, public_id, account_id, profile_id, label, created_at, revoked_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL)`,
      )
      .bind(deviceId, devicePublicId, accountId, profile.id, label, now),
    db
      .prepare(
        `INSERT INTO device_token_hashes(
           id, device_id, token_hash, scopes, created_at, token_suffix, last_used_at, revoked_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL, NULL)`,
      )
      .bind(randomUuidV7(), deviceId, tokenHash, scopes, now, tokenSuffix),
    db
      .prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, 'account', 'device.created', 'device_token', ?4, ?5, NULL, 'success', ?6)`,
      )
      .bind(randomUuidV7(), accountId, profile.id, options.requestId ?? null, tokenHash, now),
  ]);

  return {
    device_id: devicePublicId,
    profile_id: profile.public_id,
    label,
    scopes: [DEVICE_SCOPE],
    token_suffix: tokenSuffix,
    created_at: now,
    last_used_at: null,
    revoked_at: null,
    token,
  };
}

function deviceQuery(db: D1Database, accountId: string, devicePublicId: string): D1PreparedStatement {
  return db
    .prepare(
      `SELECT d.id, d.public_id, d.account_id, d.profile_id,
              p.public_id AS profile_public_id, d.label,
              h.id AS token_id, h.token_hash, h.scopes, h.token_suffix,
              h.created_at AS token_created_at, h.last_used_at,
              d.revoked_at AS device_revoked_at, h.revoked_at AS token_revoked_at,
              d.created_at AS device_created_at
         FROM devices d
         JOIN profiles p ON p.id = d.profile_id AND p.account_id = d.account_id
         LEFT JOIN device_token_hashes h ON h.device_id = d.id
        WHERE d.account_id = ?1 AND d.public_id = ?2`,
    )
    .bind(accountId, devicePublicId);
}

function viewFromRow(row: DeviceRow): DeviceView {
  return {
    device_id: row.public_id,
    profile_id: row.profile_public_id,
    label: row.label,
    scopes: safeScopes(row.scopes),
    token_suffix: row.token_suffix,
    created_at: row.device_created_at,
    last_used_at: row.last_used_at,
    revoked_at: row.device_revoked_at ?? row.token_revoked_at,
  };
}

export async function listDevices(
  db: D1Database,
  accountId: string,
  profilePublicId: string,
): Promise<DeviceView[]> {
  const profile = await ownedProfile(db, accountId, profilePublicId);
  const rows = await db
    .prepare(
      `SELECT d.id, d.public_id, d.account_id, d.profile_id,
              p.public_id AS profile_public_id, d.label,
              h.id AS token_id, h.token_hash, h.scopes, h.token_suffix,
              h.created_at AS token_created_at, h.last_used_at,
              d.revoked_at AS device_revoked_at, h.revoked_at AS token_revoked_at,
              d.created_at AS device_created_at
         FROM devices d
         JOIN profiles p ON p.id = d.profile_id AND p.account_id = d.account_id
         LEFT JOIN device_token_hashes h ON h.device_id = d.id
        WHERE d.account_id = ?1 AND d.profile_id = ?2
        ORDER BY d.created_at DESC`,
    )
    .bind(accountId, profile.id)
    .all<DeviceRow>();
  return rows.results.map(viewFromRow);
}

export async function renameDevice(
  db: D1Database,
  accountId: string,
  devicePublicId: string,
  labelInput: unknown,
  options: { now?: number; requestId?: string } = {},
): Promise<DeviceView> {
  const label = validateLabel(labelInput);
  const row = await deviceQuery(db, accountId, devicePublicId).first<DeviceRow>();
  if (!row) throw new ApiError("not_found", 404);
  const now = nowSeconds(options.now);
  await db.batch([
    db.prepare("UPDATE devices SET label = ?1 WHERE id = ?2 AND account_id = ?3").bind(label, row.id, accountId),
    db
      .prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, 'account', 'device.renamed', 'device', ?4, ?5, NULL, 'success', ?6)`,
      )
      .bind(randomUuidV7(), accountId, row.profile_id, options.requestId ?? null, await sha256Hex(devicePublicId), now),
  ]);
  return viewFromRow({ ...row, label });
}

export async function revokeDevice(
  db: D1Database,
  accountId: string,
  devicePublicId: string,
  options: { now?: number; requestId?: string } = {},
): Promise<void> {
  const row = await deviceQuery(db, accountId, devicePublicId).first<DeviceRow>();
  if (!row) throw new ApiError("not_found", 404);
  const now = nowSeconds(options.now);
  await db.batch([
    db.prepare("UPDATE devices SET revoked_at = COALESCE(revoked_at, ?1) WHERE id = ?2 AND account_id = ?3").bind(now, row.id, accountId),
    db.prepare("UPDATE device_token_hashes SET revoked_at = COALESCE(revoked_at, ?1) WHERE device_id = ?2").bind(now, row.id),
    db
      .prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, 'account', 'device.revoked', 'device', ?4, ?5, NULL, 'success', ?6)`,
      )
      .bind(randomUuidV7(), accountId, row.profile_id, options.requestId ?? null, await sha256Hex(devicePublicId), now),
  ]);
}

export async function revokeAllDevices(
  db: D1Database,
  accountId: string,
  profilePublicId: string,
  options: { now?: number; requestId?: string } = {},
): Promise<number> {
  const profile = await ownedProfile(db, accountId, profilePublicId);
  const now = nowSeconds(options.now);
  const devices = await db
    .prepare("SELECT id FROM devices WHERE account_id = ?1 AND profile_id = ?2 AND revoked_at IS NULL")
    .bind(accountId, profile.id)
    .all<{ id: string }>();
  const statements: D1PreparedStatement[] = [
    db.prepare("UPDATE devices SET revoked_at = ?1 WHERE account_id = ?2 AND profile_id = ?3 AND revoked_at IS NULL").bind(now, accountId, profile.id),
    db
      .prepare(
        `UPDATE device_token_hashes SET revoked_at = ?1
          WHERE device_id IN (SELECT id FROM devices WHERE account_id = ?2 AND profile_id = ?3)
            AND revoked_at IS NULL`,
      )
      .bind(now, accountId, profile.id),
    db
      .prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, 'account', 'device.revoked_all', 'profile', ?4, NULL, NULL, 'success', ?5)`,
      )
      .bind(randomUuidV7(), accountId, profile.id, options.requestId ?? null, now),
  ];
  await db.batch(statements);
  return devices.results.length;
}

export async function authenticateDeviceToken(
  db: D1Database,
  tokenInput: string | null | undefined,
  pepper: string,
  options: { now?: number } = {},
): Promise<DeviceIdentity> {
  if (!tokenInput || !TOKEN_PATTERN.test(tokenInput)) throw new ApiError("unauthorized", 401);
  const tokenHash = await hashDeviceToken(tokenInput, pepper);
  const now = nowSeconds(options.now);
  const row = await db
    .prepare(
      `SELECT d.id, d.public_id, d.account_id, d.profile_id,
              p.public_id AS profile_public_id, d.label,
              h.id AS token_id, h.token_hash, h.scopes, h.token_suffix,
              h.created_at AS token_created_at, h.last_used_at,
              d.revoked_at AS device_revoked_at, h.revoked_at AS token_revoked_at,
              d.created_at AS device_created_at
         FROM device_token_hashes h
         JOIN devices d ON d.id = h.device_id
         JOIN profiles p ON p.id = d.profile_id AND p.account_id = d.account_id
         JOIN accounts a ON a.id = d.account_id
        WHERE h.token_hash = ?1
          AND h.revoked_at IS NULL
          AND d.revoked_at IS NULL
          AND p.status = 'active'
          AND a.status = 'active'`,
    )
    .bind(tokenHash)
    .first<DeviceRow>();
  if (!row) throw new ApiError("unauthorized", 401);
  const scopes = safeScopes(row.scopes);
  if (!scopes.includes(DEVICE_SCOPE)) throw new ApiError("insufficient_scope", 403);
  await db
    .prepare("UPDATE device_token_hashes SET last_used_at = ?1 WHERE id = ?2 AND revoked_at IS NULL")
    .bind(now, row.token_id)
    .run();
  return {
    accountId: row.account_id,
    profileId: row.profile_id,
    profilePublicId: row.profile_public_id,
    deviceId: row.id,
    devicePublicId: row.public_id,
    scopes,
    tokenHash,
  };
}

export function bearerToken(request: Request): string | null {
  const header = request.headers.get("Authorization")?.trim() ?? "";
  if (!header) return null;
  const match = /^Bearer\s+(.+)$/i.exec(header);
  return match?.[1]?.trim() ?? null;
}

function boundedIrQuery(value: string | null, maximum: number, code = "invalid_ir_query"): string | undefined {
  if (value === null) return undefined;
  if (value.length < 1 || value.length > maximum || [...value].some((character) => /\p{Cc}/u.test(character))) {
    throw new ApiError(code, 400);
  }
  return value;
}

function requireOwnedIrPlayer(url: URL, identity: DeviceIdentity): void {
  const requestedPlayer = boundedIrQuery(url.searchParams.get("player_id"), 64);
  if (requestedPlayer !== undefined && requestedPlayer !== identity.profilePublicId) {
    throw new ApiError("forbidden", 403);
  }
}

async function irProfile(db: D1Database, identity: DeviceIdentity): Promise<{ public_id: string; display_name: string }> {
  const profile = await db.prepare(
    `SELECT public_id, display_name FROM profiles
      WHERE id = ?1 AND account_id = ?2 AND public_id = ?3 AND status = 'active'`,
  ).bind(identity.profileId, identity.accountId, identity.profilePublicId).first<{ public_id: string; display_name: string }>();
  if (!profile) throw new ApiError("not_found", 404);
  return profile;
}

async function ownerScoreProjection(url: URL, identity: DeviceIdentity, dependencies: IrReadDependencies): Promise<Record<string, unknown>> {
  requireOwnedIrPlayer(url, identity);
  const sha256 = boundedIrQuery(url.searchParams.get("sha256"), 64);
  if (sha256 !== undefined && !SHA256_PATTERN.test(sha256)) throw new ApiError("invalid_ir_query", 400);
  const rawLnMode = url.searchParams.get("ln_mode");
  const lnMode = rawLnMode === null ? undefined : Number(rawLnMode);
  if (lnMode !== undefined && (!Number.isInteger(lnMode) || lnMode < -1 || lnMode > 2)) {
    throw new ApiError("invalid_ir_query", 400);
  }
  const query = new URLSearchParams({ profile_id: identity.profilePublicId });
  if (sha256 !== undefined) query.set("sha256", sha256);
  if (lnMode !== undefined && lnMode >= 0) query.set("ln_mode", String(lnMode));
  const stub = dependencies.profileDo.get(dependencies.profileDo.idFromName(identity.profileId));
  const response = await stub.fetch(`https://profile.internal/internal/ir/scores?${query}`);
  if (!response.ok) throw new ApiError(response.status === 410 ? "profile_deleted" : "temporary_unavailable", response.status === 410 ? 410 : 503, response.status !== 410, 5);
  const result = await response.json<unknown>();
  if (!isRecord(result) || result.contract !== "ir-read.v1" || !Array.isArray(result.scores)
    || result.scores.length > 1000 || typeof result.truncated !== "boolean") {
    throw new ApiError("temporary_unavailable", 503, true, 5);
  }
  return result;
}

export async function readIrMethod(
  url: URL,
  identity: DeviceIdentity,
  dependencies: IrReadDependencies,
): Promise<Record<string, unknown>> {
  if (!IR_READ_PATHS.has(url.pathname)) throw new ApiError("not_found", 404);
  if (url.pathname === "/v1/ir/player") {
    const profile = await irProfile(dependencies.db, identity);
    return { contract: "ir-read.v1", player: { id: profile.public_id, name: profile.display_name, rank: "" } };
  }
  if (url.pathname === "/v1/ir/play-data") return ownerScoreProjection(url, identity, dependencies);
  if (url.pathname === "/v1/ir/course-play-data") {
    requireOwnedIrPlayer(url, identity);
    return { contract: "ir-read.v1", scores: [], truncated: false, unsupported: "course_ranking_v1" };
  }
  if (url.pathname === "/v1/ir/rivals") {
    return { contract: "ir-read.v1", players: [], unsupported: "rival_federation_v1" };
  }
  if (url.pathname === "/v1/ir/tables") {
    return { contract: "ir-read.v1", tables: [], unsupported: "ir_tables_v1" };
  }
  if (url.pathname === "/v1/ir/illegal-songs") {
    return { contract: "ir-read.v1", sha256: [] };
  }
  const currentVersion = boundedIrQuery(url.searchParams.get("current_version"), 64) ?? "";
  const serverBuild = typeof dependencies.buildVersion === "string" && dependencies.buildVersion.length <= 120
    ? dependencies.buildVersion : "unknown";
  return {
    contract: "ir-read.v1",
    version: { version: currentVersion, message: "", download_url: null, server_build: serverBuild },
  };
}

export async function readJsonBody(
  request: Request,
  maxBytes = IR_POLICY.maxBodyBytes,
): Promise<Record<string, unknown>> {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    throw new ApiError("unsupported_media", 415);
  }
  const declaredLength = Number(request.headers.get("Content-Length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new ApiError("payload_too_large", 413);
  }
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > maxBytes) throw new ApiError("payload_too_large", 413);
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new ApiError("invalid_json", 400);
  }
  return assertObject(parsed, "invalid_json");
}

function validateChart(value: unknown): NormalizedChart {
  const source = assertObject(value);
  assertExactKeys(source, CHART_KEYS);
  const rawHash = source.sha256 ?? source.hash;
  const sha256 = stringValue(rawHash, 64, "chart.sha256");
  if (!SHA256_PATTERN.test(sha256)) throw new ApiError("invalid_contract", 400);
  if ("sha256" in source && "hash" in source && source.sha256 !== source.hash) {
    throw new ApiError("invalid_contract", 400);
  }
  const md5 = source.md5 === undefined ? undefined : stringValue(source.md5, 32, "chart.md5");
  if (md5 !== undefined && !MD5_PATTERN.test(md5)) throw new ApiError("invalid_contract", 400);
  const title = source.title === undefined ? undefined : stringValue(source.title, 512, "chart.title");
  const artist = source.artist === undefined ? undefined : stringValue(source.artist, 512, "chart.artist");
  const mode = source.mode === undefined ? undefined : stringValue(source.mode, 32, "chart.mode");
  const bpm = source.bpm === undefined ? undefined : numberValue(source.bpm, 0, 10000, "chart.bpm");
  const notes = optionalInteger(source, "notes", 0, Number.MAX_SAFE_INTEGER);
  const lnMode = optionalInteger(source, "ln_mode", 0, 2);
  return {
    sha256,
    ...(md5 === undefined ? {} : { md5 }),
    ...(title === undefined ? {} : { title }),
    ...(artist === undefined ? {} : { artist }),
    ...(mode === undefined ? {} : { mode }),
    ...(bpm === undefined ? {} : { bpm }),
    ...(notes === undefined ? {} : { notes }),
    ...(lnMode === undefined ? {} : { ln_mode: lnMode }),
  };
}

function numberValue(value: unknown, minimum: number, maximum: number, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new ApiError("invalid_contract", 400);
  }
  return value;
}

function validateJudgements(value: unknown): Record<string, number> {
  const source = assertObject(value);
  assertExactKeys(source, JUDGEMENT_KEYS);
  assertRequired(source, [...JUDGEMENT_KEYS]);
  const result: Record<string, number> = {};
  for (const key of JUDGEMENT_KEYS) result[key] = integerValue(source[key], 0, Number.MAX_SAFE_INTEGER, `play.judgements.${key}`);
  return result;
}

function validatePlay(value: unknown): NormalizedPlay {
  const source = assertObject(value);
  assertExactKeys(source, PLAY_KEYS);
  assertRequired(source, [
    "clear",
    "gauge_kind",
    "assist_kind",
    "notes",
    "passnotes",
    "minbp",
    "ex_score",
    "combo",
    "options",
    "seed",
    "random",
    "judgements",
  ]);
  const options = source.options;
  if (!Array.isArray(options) || options.length < 1 || options.length > 4) throw new ApiError("invalid_contract", 400);
  const normalizedOptions: string[] = [];
  for (const option of options) {
    if (typeof option !== "string" || !OPTION_VALUES.has(option) || normalizedOptions.includes(option)) {
      throw new ApiError("invalid_contract", 400);
    }
    normalizedOptions.push(option);
  }
  const notes = integerValue(source.notes, 0, Number.MAX_SAFE_INTEGER, "play.notes");
  const passnotes = integerValue(source.passnotes, 0, Number.MAX_SAFE_INTEGER, "play.passnotes");
  if (passnotes > notes) throw new ApiError("invalid_contract", 400);
  const gauge = stringValue(source.gauge_kind, 32, "play.gauge_kind");
  const assist = stringValue(source.assist_kind, 32, "play.assist_kind");
  if (!GAUGE_VALUES.has(gauge) || !ASSIST_VALUES.has(assist)) throw new ApiError("invalid_contract", 400);
  return {
    clear: integerValue(source.clear, 0, 10, "play.clear"),
    gauge_kind: gauge,
    assist_kind: assist,
    notes,
    passnotes,
    minbp: integerValue(source.minbp, 0, Number.MAX_SAFE_INTEGER, "play.minbp"),
    ex_score: integerValue(source.ex_score, 0, Number.MAX_SAFE_INTEGER, "play.ex_score"),
    combo: integerValue(source.combo, 0, Number.MAX_SAFE_INTEGER, "play.combo"),
    options: normalizedOptions,
    seed: integerValue(source.seed, 0, Number.MAX_SAFE_INTEGER, "play.seed"),
    random: integerValue(source.random, 0, 2147483647, "play.random"),
    judgements: validateJudgements(source.judgements),
  };
}

function canonicalize(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new ApiError("invalid_contract", 400);
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  if (isRecord(value)) {
    const entries = Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`);
    return `{${entries.join(",")}}`;
  }
  throw new ApiError("invalid_contract", 400);
}

export async function eventDigest(
  event: NormalizedIrEvent,
  identity: Pick<DeviceIdentity, "accountId" | "profileId" | "deviceId" | "scopes">,
): Promise<string> {
  return sha256Hex(canonicalize({
    account_id: identity.accountId,
    profile_internal_id: identity.profileId,
    device_internal_id: identity.deviceId,
    scope: identity.scopes.filter((scope) => scope === DEVICE_SCOPE).sort(),
    contract: event.contract,
    schema_version: event.schema_version,
    event,
  }));
}

export function validateIrEvent(
  value: unknown,
  identity: Pick<DeviceIdentity, "profilePublicId" | "devicePublicId">,
  now = nowSeconds(),
): NormalizedIrEvent {
  const source = assertObject(value);
  assertExactKeys(source, EVENT_KEYS);
  assertRequired(source, [
    "contract",
    "schema_version",
    "event_id",
    "profile_id",
    "device_id",
    "occurred_at",
    "game_mode",
    "rule",
    "chart",
    "play",
    "is_course",
    "provenance",
  ]);
  if (source.contract !== "ir-event" || source.schema_version !== "1") throw new ApiError("invalid_contract", 400);
  const eventId = uuidValue(source.event_id, true, "event_id");
  const profileId = uuidValue(source.profile_id, false, "profile_id");
  const deviceId = uuidValue(source.device_id, false, "device_id");
  if (profileId !== identity.profilePublicId || deviceId !== identity.devicePublicId) throw new ApiError("not_found", 404);
  const occurredAt = stringValue(source.occurred_at, 64, "occurred_at");
  if (!DATE_TIME_PATTERN.test(occurredAt)) throw new ApiError("invalid_contract", 400);
  const occurredMilliseconds = Date.parse(occurredAt);
  if (!Number.isFinite(occurredMilliseconds)) throw new ApiError("invalid_contract", 400);
  const occurredSeconds = Math.floor(occurredMilliseconds / 1000);
  if (occurredSeconds < now - IR_POLICY.maxPastSeconds || occurredSeconds > now + IR_POLICY.maxFutureSeconds) {
    throw new ApiError("clock_skew", 400);
  }
  const clientVersion = source.client_version === undefined
    ? undefined
    : stringValue(source.client_version, 64, "client_version");
  if (clientVersion !== undefined && !CLIENT_VERSION_PATTERN.test(clientVersion)) throw new ApiError("invalid_contract", 400);
  if (source.game_mode !== "SP7") throw new ApiError("unsupported_game_mode", 422);
  if (source.rule !== "BEATORAJA-SP7") throw new ApiError("invalid_contract", 400);
  const provenance = assertObject(source.provenance);
  assertExactKeys(provenance, PROVENANCE_KEYS);
  assertRequired(provenance, ["trust_domain", "source", "aggregate_eligible"]);
  if (
    provenance.trust_domain !== "official" ||
    provenance.source !== "official_ir" ||
    provenance.aggregate_eligible !== false
  ) {
    throw new ApiError("invalid_contract", 400);
  }
  const isCourse = source.is_course;
  if (typeof isCourse !== "boolean") throw new ApiError("invalid_contract", 400);
  const courseId = source.course_id === undefined ? undefined : uuidValue(source.course_id, false, "course_id");
  if (isCourse && courseId === undefined) throw new ApiError("invalid_contract", 400);
  if (!isCourse && courseId !== undefined) throw new ApiError("invalid_contract", 400);
  const normalizedOccurredAt = new Date(occurredMilliseconds).toISOString();
  return {
    contract: "ir-event",
    schema_version: "1",
    event_id: eventId,
    profile_id: identity.profilePublicId,
    device_id: identity.devicePublicId,
    occurred_at: normalizedOccurredAt,
    ...(clientVersion === undefined ? {} : { client_version: clientVersion }),
    game_mode: "SP7",
    rule: "BEATORAJA-SP7",
    chart: validateChart(source.chart),
    play: validatePlay(source.play),
    is_course: isCourse,
    ...(courseId === undefined ? {} : { course_id: courseId }),
    provenance: {
      trust_domain: "official",
      source: "official_ir",
      aggregate_eligible: false,
    },
  };
}

export async function enforceIrRateLimit(
  db: D1Database,
  scope: "ip" | "token",
  key: string,
  pepper: string,
  now = nowSeconds(),
): Promise<void> {
  const keyHash = await sha256Hex(`${pepper}\u0000${scope}\u0000${key}`);
  const limit = scope === "ip" ? IR_POLICY.ipRateLimit : IR_POLICY.tokenRateLimit;
  await db
    .prepare(
      `INSERT INTO ir_rate_limits(scope, key_hash, window_started_at, request_count)
       VALUES (?1, ?2, ?3, 1)
       ON CONFLICT(scope, key_hash) DO UPDATE SET
         request_count = CASE
           WHEN ir_rate_limits.window_started_at + ?4 <= ?3 THEN 1
           ELSE ir_rate_limits.request_count + 1
         END,
         window_started_at = CASE
           WHEN ir_rate_limits.window_started_at + ?4 <= ?3 THEN ?3
           ELSE ir_rate_limits.window_started_at
         END`,
    )
    .bind(scope, keyHash, now, IR_POLICY.rateLimitWindowSeconds)
    .run();
  const row = await db
    .prepare("SELECT window_started_at, request_count FROM ir_rate_limits WHERE scope = ?1 AND key_hash = ?2")
    .bind(scope, keyHash)
    .first<RateLimitRow>();
  if (row && row.request_count > limit) {
    const retryAfter = Math.max(1, row.window_started_at + IR_POLICY.rateLimitWindowSeconds - now);
    throw new ApiError("rate_limited", 429, true, retryAfter);
  }
}

export function authErrorResponse(error: unknown, requestId: string, origin?: string): Response {
  const status = error instanceof ApiError || error instanceof AuthError ? error.status : 500;
  const code = error instanceof ApiError || error instanceof AuthError ? error.code : "internal_error";
  const retryable = error instanceof ApiError ? error.retryable : status >= 500;
  const retryAfterSeconds = error instanceof ApiError ? error.retryAfterSeconds ?? null : null;
  const headers = new Headers({
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  if (origin) {
    headers.set("access-control-allow-origin", origin);
    headers.set("access-control-allow-credentials", "true");
  }
  if (retryAfterSeconds !== null) headers.set("retry-after", String(retryAfterSeconds));
  return new Response(JSON.stringify({
    error: {
      code,
      message: code,
      request_id: requestId,
      retryable,
      retry_after_seconds: retryAfterSeconds,
    },
  }) + "\n", { status, headers });
}

export function assertDeviceScope(identity: DeviceIdentity): void {
  if (!identity.scopes.includes(DEVICE_SCOPE)) throw new ApiError("insufficient_scope", 403);
}
