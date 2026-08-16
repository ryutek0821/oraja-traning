import { argon2id, argon2Verify } from "hash-wasm";
import {
  type AuthEmailSender,
  decryptEmail,
  encryptEmail,
} from "./email";

export const AUTH_POLICY = {
  passwordParamsVersion: 1,
  password: {
    memorySize: 64 * 1024,
    iterations: 3,
    parallelism: 1,
    hashLength: 32,
  },
  recoveryCodeCount: 10,
  sessionTtlSeconds: 30 * 24 * 60 * 60,
  emailTokenTtlSeconds: 30 * 60,
  resetTokenTtlSeconds: 15 * 60,
  rateLimitWindowSeconds: 15 * 60,
  maxBackoffSeconds: 15 * 60,
} as const;

const USER_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{2,31}$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const textEncoder = new TextEncoder();

export class AuthError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 400,
    public readonly retryAfterSeconds?: number,
  ) {
    super(code);
    this.name = "AuthError";
  }
}

export type AuthOptions = {
  now?: number;
  requestId?: string;
  ipAddress?: string;
  publicOrigin?: string;
  emailSender?: AuthEmailSender;
  emailEncryptionSecret?: string;
  hashingSecret?: string;
};

export type RegisterInput = {
  userId: unknown;
  password: unknown;
  termsVersion: unknown;
  privacyVersion: unknown;
  termsConsent?: unknown;
  privacyConsent?: unknown;
  displayName?: unknown;
  timezone?: unknown;
  email?: unknown;
};

export type RegisterResult = {
  accountPublicId: string;
  profilePublicId: string;
  recoveryCodes: string[];
  emailConfirmationSent: boolean;
};

export type EmailChangeResult = {
  emailConfirmationSent: boolean;
};

export type LoginResult = {
  accountPublicId: string;
  sessionToken: string;
  csrfToken: string;
  expiresAt: number;
};

type SessionResult = {
  sessionToken: string;
  csrfToken: string;
  expiresAt: number;
  sessionId: string;
};

type SessionMaterial = SessionResult & {
  accountId: string;
  sessionHash: string;
  csrfHash: string;
};

type AccountRow = {
  id: string;
  public_id: string;
  status: string;
  password_hash: string;
  password_params_version: number;
  password_rehash_required: number;
};

type SessionRow = {
  id: string;
  account_id: string;
  public_id: string;
  csrf_hash: string;
  expires_at: number;
};

type RecoveryRow = {
  id: string;
  salt: string;
  code_hash: string;
};

type EmailTokenRow = {
  id: string;
  email_id: string;
  account_id: string;
  encrypted_email: string | null;
  verified_at: number | null;
  expires_at: number;
  used_at: number | null;
};

function nowSeconds(options?: AuthOptions): number {
  return options?.now ?? Math.floor(Date.now() / 1000);
}

function utf8(value: string): Uint8Array {
  return textEncoder.encode(value);
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

function arrayBuffer(value: Uint8Array): ArrayBuffer {
  return Uint8Array.from(value).buffer;
}

function encodeBase64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(normalized);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function randomBytes(length: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(length));
}

function randomToken(): string {
  return encodeBase64Url(randomBytes(32));
}

function randomId(): string {
  return crypto.randomUUID();
}

function hex(value: Uint8Array): string {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function sha256Hex(value: string | Uint8Array): Promise<string> {
  const input = typeof value === "string" ? utf8(value) : value;
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", arrayBuffer(input))));
}

async function keyedHashHex(secret: string | undefined, value: string): Promise<string> {
  if (!secret || secret.length < 16) throw new AuthError("auth_hash_secret_missing", 500);
  const key = await crypto.subtle.importKey(
    "raw",
    arrayBuffer(utf8(secret)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, arrayBuffer(utf8(value)));
  return hex(new Uint8Array(signature));
}

async function saltedHashHex(secret: string, salt: Uint8Array): Promise<string> {
  return sha256Hex(concatBytes(salt, utf8(secret)));
}

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = utf8(left);
  const rightBytes = utf8(right);
  let difference = leftBytes.length ^ rightBytes.length;
  const length = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (leftBytes[index] ?? 0) ^ (rightBytes[index] ?? 0);
  }
  return difference === 0;
}

/**
 * This service deliberately chooses a strict ASCII policy. It avoids Unicode
 * confusables and makes case-folding collisions explicit at the D1 boundary.
 */
export function normalizeUserId(value: unknown): string {
  if (typeof value !== "string") throw new AuthError("invalid_user_id", 400);
  const normalized = value.normalize("NFKC").trim().toLowerCase();
  if (!USER_ID_PATTERN.test(normalized)) throw new AuthError("invalid_user_id", 400);
  return normalized;
}

export function validatePassword(value: unknown): string {
  if (typeof value !== "string") throw new AuthError("invalid_password", 400);
  const length = Array.from(value).length;
  if (length < 12 || length > 128) throw new AuthError("invalid_password", 400);
  if ([...value].some((character) => character === "\u0000" || /\p{Cc}/u.test(character))) {
    throw new AuthError("invalid_password", 400);
  }
  return value;
}

function normalizeEmail(value: unknown): string {
  if (typeof value !== "string") throw new AuthError("invalid_email", 400);
  const normalized = value.trim().toLowerCase();
  if (normalized.length > 320 || !EMAIL_PATTERN.test(normalized)) {
    throw new AuthError("invalid_email", 400);
  }
  return normalized;
}

function consentGiven(value: unknown): boolean {
  return value === true || value === "true" || value === "on" || value === "1";
}

function safeDisplayName(value: unknown, fallback: string): string {
  if (value === undefined) return fallback;
  if (typeof value !== "string") throw new AuthError("invalid_profile", 400);
  const normalized = value.trim();
  if (normalized.length < 1 || normalized.length > 80) throw new AuthError("invalid_profile", 400);
  return normalized;
}

function safeTimezone(value: unknown): string {
  if (value === undefined) return "Asia/Tokyo";
  if (typeof value !== "string" || value.length < 1 || value.length > 64 || !/^[A-Za-z0-9_+./-]+$/.test(value)) {
    throw new AuthError("invalid_timezone", 400);
  }
  return value;
}

function documentVersion(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 128 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)
  ) {
    throw publicError("terms_required", 400);
  }
  return value;
}

export async function hashPassword(password: string): Promise<string> {
  validatePassword(password);
  const result = await argon2id({
    password,
    salt: randomBytes(16),
    memorySize: AUTH_POLICY.password.memorySize,
    iterations: AUTH_POLICY.password.iterations,
    parallelism: AUTH_POLICY.password.parallelism,
    hashLength: AUTH_POLICY.password.hashLength,
    outputType: "encoded",
  });
  return String(result);
}

export async function verifyPassword(password: string, encodedHash: string): Promise<boolean> {
  try {
    return await argon2Verify({ password, hash: encodedHash });
  } catch {
    return false;
  }
}

export type RecoveryCodeRecord = { salt: string; hash: string };

export async function generateRecoveryCodes(
  count = AUTH_POLICY.recoveryCodeCount,
): Promise<{ codes: string[]; records: RecoveryCodeRecord[] }> {
  if (!Number.isInteger(count) || count < 1 || count > 20) throw new Error("invalid_recovery_code_count");
  const codes: string[] = [];
  const records: RecoveryCodeRecord[] = [];
  for (let index = 0; index < count; index += 1) {
    const code = encodeBase64Url(randomBytes(32));
    const salt = randomBytes(16);
    codes.push(code);
    records.push({ salt: encodeBase64Url(salt), hash: await saltedHashHex(code, salt) });
  }
  return { codes, records };
}

export function sessionCookie(token: string, maxAge = AUTH_POLICY.sessionTtlSeconds): string {
  return `__Host-oraja_session=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

export function csrfCookie(token: string, maxAge = AUTH_POLICY.sessionTtlSeconds): string {
  return `oraja_csrf=${token}; Path=/; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

export function expiredSessionCookie(): string {
  return "__Host-oraja_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0";
}

export function expiredCsrfCookie(): string {
  return "oraja_csrf=; Path=/; Secure; SameSite=Lax; Max-Age=0";
}

export function readCookie(request: Request, name: string): string | null {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) continue;
    if (part.slice(0, separator).trim() === name) return part.slice(separator + 1).trim() || null;
  }
  return null;
}

function publicError(code: string, status = 400, retryAfterSeconds?: number): AuthError {
  return new AuthError(code, status, retryAfterSeconds);
}

async function rateKey(scope: string, value: string, secret?: string): Promise<string> {
  return keyedHashHex(secret, `rate:${scope}:${value}`);
}

async function enforceRateLimit(
  db: D1Database,
  scope: "ip" | "account" | "recovery" | "email",
  value: string,
  now: number,
  hashingSecret?: string,
): Promise<void> {
  const keyHash = await rateKey(scope, value, hashingSecret);
  const row = await db
    .prepare("SELECT blocked_until, last_failed_at FROM auth_rate_limits WHERE scope = ?1 AND key_hash = ?2")
    .bind(scope, keyHash)
    .first<{ blocked_until: number; last_failed_at: number }>();
  if (
    row &&
    now - row.last_failed_at <= AUTH_POLICY.rateLimitWindowSeconds &&
    row.blocked_until > now
  ) {
    throw publicError("rate_limited", 429, row.blocked_until - now);
  }
}

function backoffSeconds(failures: number): number {
  return Math.min(AUTH_POLICY.maxBackoffSeconds, 2 ** Math.min(Math.max(failures - 1, 0), 10));
}

async function recordFailure(
  db: D1Database,
  scope: "ip" | "account" | "recovery" | "email",
  value: string,
  now: number,
  hashingSecret?: string,
): Promise<void> {
  const keyHash = await rateKey(scope, value, hashingSecret);
  const row = await db
    .prepare("SELECT failure_count, first_failed_at, last_failed_at FROM auth_rate_limits WHERE scope = ?1 AND key_hash = ?2")
    .bind(scope, keyHash)
    .first<{ failure_count: number; first_failed_at: number; last_failed_at: number }>();
  const failureCount = (row?.failure_count ?? 0) + 1;
  const withinWindow = row && now - row.last_failed_at <= AUTH_POLICY.rateLimitWindowSeconds;
  const nextFailureCount = withinWindow ? failureCount : 1;
  const firstFailedAt = withinWindow ? row.first_failed_at : now;
  const blockedUntil = now + backoffSeconds(nextFailureCount);
  await db
    .prepare(
      `INSERT INTO auth_rate_limits(scope, key_hash, failure_count, first_failed_at, last_failed_at, blocked_until)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6)
       ON CONFLICT(scope, key_hash) DO UPDATE SET
         failure_count = excluded.failure_count,
         first_failed_at = excluded.first_failed_at,
         last_failed_at = excluded.last_failed_at,
         blocked_until = excluded.blocked_until`,
    )
    .bind(scope, keyHash, nextFailureCount, firstFailedAt, now, blockedUntil)
    .run();
}

async function clearFailure(
  db: D1Database,
  scope: "ip" | "account" | "recovery" | "email",
  value: string,
  hashingSecret?: string,
): Promise<void> {
  await db
    .prepare("DELETE FROM auth_rate_limits WHERE scope = ?1 AND key_hash = ?2")
    .bind(scope, await rateKey(scope, value, hashingSecret))
    .run();
}

function idHash(scope: string, value: string, secret?: string): Promise<string> {
  return keyedHashHex(secret, `identifier:${scope}:${value}`);
}

function changes(result: { meta?: { changes?: number } }): number {
  return result.meta?.changes ?? 0;
}

async function createSession(
  db: D1Database,
  accountId: string,
  now: number,
): Promise<SessionResult> {
  const session = await newSession(accountId, now);
  await insertSession(db, session, now).run();
  return session;
}

async function newSession(accountId: string, now: number): Promise<SessionMaterial> {
  const sessionToken = randomToken();
  const csrfToken = randomToken();
  const sessionId = randomId();
  const expiresAt = now + AUTH_POLICY.sessionTtlSeconds;
  const sessionHash = await sha256Hex(sessionToken);
  const csrfHash = await sha256Hex(`${sessionToken}:${csrfToken}`);
  return { accountId, sessionToken, csrfToken, expiresAt, sessionId, sessionHash, csrfHash };
}

function insertSession(
  db: D1Database,
  session: SessionMaterial,
  now: number,
): D1PreparedStatement {
  return db
    .prepare(
      `INSERT INTO sessions(
         id, account_id, session_hash, csrf_hash, created_at, expires_at,
         last_seen_at, rotated_at, revoked_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?5, NULL, NULL)`,
    )
    .bind(
      session.sessionId,
      session.accountId,
      session.sessionHash,
      session.csrfHash,
      now,
      session.expiresAt,
    );
}

async function requireActiveSession(
  db: D1Database,
  sessionToken: string,
  now: number,
): Promise<SessionRow> {
  if (!sessionToken || sessionToken.length < 32) throw publicError("unauthorized", 401);
  const row = await db
    .prepare(
      `SELECT s.id, s.account_id, a.public_id, s.csrf_hash, s.expires_at
         FROM sessions s
         JOIN accounts a ON a.id = s.account_id
        WHERE s.session_hash = ?1
          AND s.revoked_at IS NULL
          AND s.expires_at > ?2
          AND a.status = 'active'`,
    )
    .bind(await sha256Hex(sessionToken), now)
    .first<SessionRow>();
  if (!row) throw publicError("unauthorized", 401);
  return row;
}

export async function verifyCsrf(
  db: D1Database,
  sessionToken: string,
  csrfToken: string | null | undefined,
  now = nowSeconds(),
): Promise<boolean> {
  if (!csrfToken) return false;
  try {
    const session = await requireActiveSession(db, sessionToken, now);
    const actual = await sha256Hex(`${sessionToken}:${csrfToken}`);
    return constantTimeEqual(actual, session.csrf_hash);
  } catch {
    return false;
  }
}

export async function registerAccount(
  db: D1Database,
  input: RegisterInput,
  options: AuthOptions = {},
): Promise<RegisterResult> {
  const now = nowSeconds(options);
  if (options.ipAddress) await enforceRateLimit(db, "ip", options.ipAddress, now, options.hashingSecret);
  try {
    const userId = normalizeUserId(input.userId);
    const password = validatePassword(input.password);
    const termsVersion = documentVersion(input.termsVersion);
    const privacyVersion = documentVersion(input.privacyVersion);
    if (!consentGiven(input.termsConsent) || !consentGiven(input.privacyConsent)) {
      throw publicError("terms_required", 400);
    }
    const terms = await db
      .prepare("SELECT version FROM terms_versions WHERE version = ?1 AND document_kind = 'terms'")
      .bind(termsVersion)
      .first<{ version: string }>();
    const privacy = await db
      .prepare("SELECT version FROM terms_versions WHERE version = ?1 AND document_kind = 'privacy'")
      .bind(privacyVersion)
      .first<{ version: string }>();
    if (!terms || !privacy) throw publicError("terms_required", 400);

    const email = input.email === undefined ? null : normalizeEmail(input.email);
    if (email && !options.emailEncryptionSecret) {
      throw publicError("email_not_configured", 503);
    }
    const displayName = safeDisplayName(input.displayName, userId);
    const timezone = safeTimezone(input.timezone);
    const passwordHash = await hashPassword(password);
    const { codes: recoveryCodes, records: recoveryRecords } = await generateRecoveryCodes();
    const accountId = randomId();
    const accountPublicId = randomId();
    const profileId = randomId();
    const profilePublicId = randomId();
    const emailId = email ? randomId() : null;
    const emailToken = email ? randomToken() : null;
    const encryptedEmail = email
      ? await encryptEmail(email, options.emailEncryptionSecret as string)
      : null;

    const statements: D1PreparedStatement[] = [
      db.prepare(
        `INSERT INTO accounts(id, public_id, username_normalized, status, created_at, updated_at)
         VALUES (?1, ?2, ?3, 'active', ?4, ?4)`,
      ).bind(accountId, accountPublicId, userId, now),
      db.prepare(
        `INSERT INTO profile_limits(account_id, max_profiles, updated_at) VALUES (?1, 1, ?2)`,
      ).bind(accountId, now),
      db.prepare(
        `INSERT INTO profiles(
           id, public_id, account_id, display_name, timezone, status, created_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6, ?6)`,
      ).bind(profileId, profilePublicId, accountId, displayName, timezone, now),
      db.prepare(
        `INSERT INTO credentials(
           account_id, password_hash, password_params_version, password_rehash_required,
           created_at, updated_at, revoked_at
         ) VALUES (?1, ?2, ?3, 0, ?4, ?4, NULL)`,
      ).bind(accountId, passwordHash, AUTH_POLICY.passwordParamsVersion, now),
      db.prepare(
        `INSERT INTO consents(id, account_id, profile_id, scope, terms_version, granted_at, revoked_at)
         VALUES (?1, ?2, ?3, 'terms', ?4, ?5, NULL)`,
      ).bind(randomId(), accountId, profileId, termsVersion, now),
      db.prepare(
        `INSERT INTO consents(id, account_id, profile_id, scope, terms_version, granted_at, revoked_at)
         VALUES (?1, ?2, ?3, 'privacy', ?4, ?5, NULL)`,
      ).bind(randomId(), accountId, profileId, privacyVersion, now),
      db.prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type, reason_code,
           request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, 'account', 'register', 'registration', ?4, ?5, NULL, 'success', ?6)`,
      ).bind(randomId(), accountId, profileId, options.requestId ?? null, await idHash("user", userId, options.hashingSecret), now),
    ];

    for (const [index, record] of recoveryRecords.entries()) {
      statements.push(
        db.prepare(
          `INSERT INTO recovery_codes(id, account_id, code_hash, salt, created_at, used_at)
           VALUES (?1, ?2, ?3, ?4, ?5, NULL)`,
        ).bind(randomId(), accountId, record.hash, record.salt, now),
      );
      if (index >= AUTH_POLICY.recoveryCodeCount) break;
    }

    if (email && emailId && emailToken) {
      statements.push(
        db.prepare(
          `INSERT INTO emails(
             id, account_id, email_hash, encrypted_email, verified_at, created_at, revoked_at
           ) VALUES (?1, ?2, ?3, ?4, NULL, ?5, NULL)`,
        ).bind(emailId, accountId, await idHash("email", email, options.hashingSecret), encryptedEmail, now),
        db.prepare(
          `INSERT INTO email_tokens(
             id, email_id, token_hash, purpose, created_at, expires_at, used_at
           ) VALUES (?1, ?2, ?3, 'verify', ?4, ?5, NULL)`,
        ).bind(randomId(), emailId, await sha256Hex(emailToken), now, now + AUTH_POLICY.emailTokenTtlSeconds),
        db.prepare(
          `INSERT INTO audit_events(
             id, account_id, profile_id, actor_kind, event_type, reason_code,
             request_id, resource_hash, input_hash, status, occurred_at
           ) VALUES (?1, ?2, ?3, 'account', 'email_confirmation_requested', 'registration', ?4, ?5, NULL, 'success', ?6)`,
        ).bind(randomId(), accountId, profileId, options.requestId ?? null, await idHash("email", email, options.hashingSecret), now),
      );
    }

    try {
      await db.batch(statements);
    } catch {
      throw publicError("registration_failed", 409);
    }

    let emailConfirmationSent = false;
    if (email && emailToken && options.emailSender && options.publicOrigin) {
      try {
        await options.emailSender.send({
          to: email,
          purpose: "verify",
          token: emailToken,
          origin: options.publicOrigin,
        });
        emailConfirmationSent = true;
      } catch {
        // The account remains usable without email recovery. Do not expose the
        // provider error or include the address/token in a response or audit log.
        emailConfirmationSent = false;
      }
    }
    return { accountPublicId, profilePublicId, recoveryCodes, emailConfirmationSent };
  } catch (error) {
    if (options.ipAddress && error instanceof AuthError && error.status < 500) {
      await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    }
    throw error;
  }
}

export async function loginAccount(
  db: D1Database,
  userIdInput: unknown,
  passwordInput: unknown,
  options: AuthOptions = {},
): Promise<LoginResult> {
  const now = nowSeconds(options);
  if (options.ipAddress) await enforceRateLimit(db, "ip", options.ipAddress, now, options.hashingSecret);
  let userId: string;
  let password: string;
  try {
    userId = normalizeUserId(userIdInput);
    password = validatePassword(passwordInput);
  } catch (error) {
    if (options.ipAddress && error instanceof AuthError) {
      await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    }
    throw error;
  }
  const row = await db
    .prepare(
      `SELECT a.id, a.public_id, a.status, c.password_hash,
              c.password_params_version, c.password_rehash_required
         FROM accounts a
         JOIN credentials c ON c.account_id = a.id
        WHERE a.username_normalized = ?1`,
    )
    .bind(userId)
    .first<AccountRow>();
  if (row) await enforceRateLimit(db, "account", row.id, now, options.hashingSecret);
  let valid = false;
  if (row && row.status === "active") valid = await verifyPassword(password, row.password_hash);
  else await hashPassword(password);
  if (!valid || !row) {
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    if (row) await recordFailure(db, "account", row.id, now, options.hashingSecret);
    throw publicError("invalid_credentials", 401);
  }

  const session = await newSession(row.id, now);
  const statements: D1PreparedStatement[] = [
    insertSession(db, session, now),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'login', 'password', ?3, NULL, NULL, 'success', ?4)`,
    ).bind(randomId(), row.id, options.requestId ?? null, now),
  ];
  if (
    row.password_params_version !== AUTH_POLICY.passwordParamsVersion ||
    row.password_rehash_required === 1
  ) {
    statements.push(
      db.prepare(
        `UPDATE credentials
            SET password_hash = ?1, password_params_version = ?2,
                password_rehash_required = 0, updated_at = ?3
          WHERE account_id = ?4`,
      ).bind(await hashPassword(password), AUTH_POLICY.passwordParamsVersion, now, row.id),
    );
  }
  await db.batch(statements);
  if (options.ipAddress) await clearFailure(db, "ip", options.ipAddress, options.hashingSecret);
  await clearFailure(db, "account", row.id, options.hashingSecret);
  return {
    accountPublicId: row.public_id,
    sessionToken: session.sessionToken,
    csrfToken: session.csrfToken,
    expiresAt: session.expiresAt,
  };
}

export async function logoutSession(
  db: D1Database,
  sessionToken: string | null | undefined,
  options: AuthOptions = {},
): Promise<void> {
  if (!sessionToken) return;
  const now = nowSeconds(options);
  const sessionHash = await sha256Hex(sessionToken);
  const row = await db
    .prepare("SELECT account_id FROM sessions WHERE session_hash = ?1 AND revoked_at IS NULL")
    .bind(sessionHash)
    .first<{ account_id: string }>();
  if (!row) return;
  await db.batch([
    db.prepare("UPDATE sessions SET revoked_at = ?1 WHERE session_hash = ?2 AND revoked_at IS NULL").bind(now, sessionHash),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'logout', 'session', ?3, NULL, NULL, 'success', ?4)`,
    ).bind(randomId(), row.account_id, options.requestId ?? null, now),
  ]);
}

export async function revokeAllSessions(
  db: D1Database,
  accountId: string,
  options: AuthOptions = {},
): Promise<void> {
  const now = nowSeconds(options);
  await db
    .prepare("UPDATE sessions SET revoked_at = ?1 WHERE account_id = ?2 AND revoked_at IS NULL")
    .bind(now, accountId)
    .run();
}

async function revokeOAuthGrantsForAccount(db: D1Database, accountId: string, reason: string, now: number): Promise<void> {
  await db.batch([
    db.prepare(
      `UPDATE oauth_grants
          SET revoked_at = COALESCE(revoked_at, ?1), revoke_reason = COALESCE(revoke_reason, ?2)
        WHERE account_id = ?3 AND revoked_at IS NULL`,
    ).bind(now, reason, accountId),
    db.prepare(
      `UPDATE oauth_tokens
          SET revoked_at = COALESCE(revoked_at, ?1)
        WHERE account_id = ?2 AND revoked_at IS NULL`,
    ).bind(now, accountId),
    db.prepare(
      `UPDATE oauth_authorization_codes
          SET used_at = COALESCE(used_at, ?1)
        WHERE account_id = ?2 AND used_at IS NULL`,
    ).bind(now, accountId),
  ]);
}

export async function rotateSession(
  db: D1Database,
  sessionToken: string,
  options: AuthOptions = {},
): Promise<LoginResult> {
  const now = nowSeconds(options);
  const current = await requireActiveSession(db, sessionToken, now);
  const revoked = await db
    .prepare("UPDATE sessions SET revoked_at = ?1, rotated_at = ?1 WHERE id = ?2 AND revoked_at IS NULL")
    .bind(now, current.id)
    .run();
  if (changes(revoked) !== 1) throw publicError("unauthorized", 401);
  const next = await createSession(db, current.account_id, now);
  await db
    .prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'session_rotated', 'session', ?3, NULL, NULL, 'success', ?4)`,
    )
    .bind(randomId(), current.account_id, options.requestId ?? null, now)
    .run();
  return {
    accountPublicId: current.public_id,
    sessionToken: next.sessionToken,
    csrfToken: next.csrfToken,
    expiresAt: next.expiresAt,
  };
}

export async function useRecoveryCode(
  db: D1Database,
  userIdInput: unknown,
  code: unknown,
  options: AuthOptions = {},
): Promise<LoginResult> {
  const now = nowSeconds(options);
  if (options.ipAddress) await enforceRateLimit(db, "recovery", options.ipAddress, now, options.hashingSecret);
  let userId: string;
  try {
    userId = normalizeUserId(userIdInput);
  } catch (error) {
    if (options.ipAddress && error instanceof AuthError) {
      await recordFailure(db, "recovery", options.ipAddress, now, options.hashingSecret);
    }
    throw error;
  }
  if (typeof code !== "string" || code.length < 32 || code.length > 128) {
    if (options.ipAddress) await recordFailure(db, "recovery", options.ipAddress, now, options.hashingSecret);
    throw publicError("invalid_recovery_code", 401);
  }
  const account = await db
    .prepare("SELECT id, public_id FROM accounts WHERE username_normalized = ?1 AND status = 'active'")
    .bind(userId)
    .first<{ id: string; public_id: string }>();
  if (!account) {
    if (options.ipAddress) await recordFailure(db, "recovery", options.ipAddress, now, options.hashingSecret);
    throw publicError("invalid_recovery_code", 401);
  }
  await enforceRateLimit(db, "account", account.id, now, options.hashingSecret);
  const rows = await db
    .prepare("SELECT id, salt, code_hash FROM recovery_codes WHERE account_id = ?1 AND used_at IS NULL")
    .bind(account.id)
    .all<RecoveryRow>();
  let match: RecoveryRow | null = null;
  for (const row of rows.results) {
    let candidate = "";
    try {
      candidate = await saltedHashHex(code, decodeBase64Url(row.salt));
    } catch {
      candidate = await sha256Hex(code);
    }
    if (constantTimeEqual(candidate, row.code_hash)) match = row;
  }
  if (!match) {
    if (options.ipAddress) await recordFailure(db, "recovery", options.ipAddress, now, options.hashingSecret);
    await recordFailure(db, "account", account.id, now, options.hashingSecret);
    throw publicError("invalid_recovery_code", 401);
  }
  const consumed = await db
    .prepare("UPDATE recovery_codes SET used_at = ?1 WHERE id = ?2 AND used_at IS NULL")
    .bind(now, match.id)
    .run();
  if (changes(consumed) !== 1) throw publicError("invalid_recovery_code", 401);
  await revokeAllSessions(db, account.id, options);
  await revokeOAuthGrantsForAccount(db, account.id, "account_recovery", now);
  const session = await createSession(db, account.id, now);
  await db
    .prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'recovery', 'recovery_code', ?3, NULL, NULL, 'success', ?4)`,
    )
    .bind(randomId(), account.id, options.requestId ?? null, now)
    .run();
  if (options.ipAddress) await clearFailure(db, "recovery", options.ipAddress, options.hashingSecret);
  await clearFailure(db, "account", account.id, options.hashingSecret);
  return {
    accountPublicId: account.public_id,
    sessionToken: session.sessionToken,
    csrfToken: session.csrfToken,
    expiresAt: session.expiresAt,
  };
}

/**
 * Starts an authenticated email change. The caller still has to enforce the
 * route-level CSRF check; this service only binds the request to an active
 * account session and records an opaque, purpose-limited confirmation token.
 */
export async function requestEmailChange(
  db: D1Database,
  sessionToken: string,
  emailInput: unknown,
  options: AuthOptions = {},
): Promise<EmailChangeResult> {
  const now = nowSeconds(options);
  const session = await requireActiveSession(db, sessionToken, now);
  if (options.ipAddress) await enforceRateLimit(db, "ip", options.ipAddress, now, options.hashingSecret);
  await enforceRateLimit(db, "account", session.account_id, now, options.hashingSecret);

  let email: string;
  try {
    email = normalizeEmail(emailInput);
  } catch (error) {
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    throw error;
  }
  if (!options.emailEncryptionSecret) throw publicError("email_not_configured", 503);
  await enforceRateLimit(db, "email", email, now, options.hashingSecret);

  const existing = await db
    .prepare("SELECT id, account_id FROM emails WHERE email_hash = ?1")
    .bind(await idHash("email", email, options.hashingSecret))
    .first<{ id: string; account_id: string }>();
  if (existing && existing.account_id !== session.account_id) {
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    await recordFailure(db, "email", email, now, options.hashingSecret);
    throw publicError("email_unavailable", 409);
  }

  const emailId = existing?.id ?? randomId();
  const token = randomToken();
  const encryptedEmail = await encryptEmail(email, options.emailEncryptionSecret);
  const statements: D1PreparedStatement[] = [
    existing
      ? db.prepare(
          `UPDATE emails
              SET encrypted_email = ?1, verified_at = NULL, created_at = ?2, revoked_at = NULL
            WHERE id = ?3 AND account_id = ?4`,
        ).bind(encryptedEmail, now, emailId, session.account_id)
      : db.prepare(
          `INSERT INTO emails(
             id, account_id, email_hash, encrypted_email, verified_at, created_at, revoked_at
           ) VALUES (?1, ?2, ?3, ?4, NULL, ?5, NULL)`,
        ).bind(emailId, session.account_id, await idHash("email", email, options.hashingSecret), encryptedEmail, now),
    db.prepare(
      `UPDATE email_tokens SET used_at = ?1
        WHERE email_id = ?2 AND purpose = 'verify' AND used_at IS NULL`,
    ).bind(now, emailId),
    db.prepare(
      `INSERT INTO email_tokens(
         id, email_id, token_hash, purpose, created_at, expires_at, used_at
       ) VALUES (?1, ?2, ?3, 'verify', ?4, ?5, NULL)`,
    ).bind(randomId(), emailId, await sha256Hex(token), now, now + AUTH_POLICY.emailTokenTtlSeconds),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'email_change_requested', 'email', ?3, ?4, NULL, 'success', ?5)`,
    ).bind(randomId(), session.account_id, options.requestId ?? null, await idHash("email", email, options.hashingSecret), now),
  ];

  try {
    await db.batch(statements);
  } catch {
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    await recordFailure(db, "email", email, now, options.hashingSecret);
    throw publicError("email_change_failed", 409);
  }

  let emailConfirmationSent = false;
  if (options.emailSender && options.publicOrigin) {
    try {
      await options.emailSender.send({
        to: email,
        purpose: "verify",
        token,
        origin: options.publicOrigin,
      });
      emailConfirmationSent = true;
    } catch {
      emailConfirmationSent = false;
    }
  }
  if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
  await recordFailure(db, "email", email, now, options.hashingSecret);
  await recordFailure(db, "account", session.account_id, now, options.hashingSecret);
  return { emailConfirmationSent };
}

export async function requestPasswordReset(
  db: D1Database,
  emailInput: unknown,
  options: AuthOptions = {},
): Promise<void> {
  const now = nowSeconds(options);
  if (options.ipAddress) await enforceRateLimit(db, "ip", options.ipAddress, now, options.hashingSecret);
  let email: string;
  try {
    email = normalizeEmail(emailInput);
  } catch {
    // The endpoint intentionally returns the same result for unknown and
    // malformed addresses, avoiding an account-existence oracle.
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    return;
  }
  await enforceRateLimit(db, "email", email, now, options.hashingSecret);
  const row = await db
    .prepare(
      `SELECT e.id, e.account_id, e.encrypted_email
         FROM emails e JOIN accounts a ON a.id = e.account_id
        WHERE e.email_hash = ?1 AND e.revoked_at IS NULL
          AND e.verified_at IS NOT NULL AND a.status = 'active'`,
    )
    .bind(await idHash("email", email, options.hashingSecret))
    .first<{ id: string; account_id: string; encrypted_email: string | null }>();
  if (row) await enforceRateLimit(db, "account", row.account_id, now, options.hashingSecret);
  if (!row) {
    if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
    await recordFailure(db, "email", email, now, options.hashingSecret);
    return;
  }
  const token = randomToken();
  await db.batch([
    db.prepare(
      `UPDATE email_tokens SET used_at = ?1
        WHERE email_id = ?2 AND purpose = 'password_reset' AND used_at IS NULL`,
    ).bind(now, row.id),
    db.prepare(
      `INSERT INTO email_tokens(
         id, email_id, token_hash, purpose, created_at, expires_at, used_at
       ) VALUES (?1, ?2, ?3, 'password_reset', ?4, ?5, NULL)`,
    ).bind(randomId(), row.id, await sha256Hex(token), now, now + AUTH_POLICY.resetTokenTtlSeconds),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'password_reset_requested', 'email', ?3, ?4, NULL, 'success', ?5)`,
    ).bind(randomId(), row.account_id, options.requestId ?? null, await idHash("email", email, options.hashingSecret), now),
  ]);
  if (options.emailSender && options.publicOrigin && options.emailEncryptionSecret && row.encrypted_email) {
    try {
      const destination = await decryptEmail(row.encrypted_email, options.emailEncryptionSecret);
      await options.emailSender.send({ to: destination, purpose: "password_reset", token, origin: options.publicOrigin });
    } catch {
      // Keep the request indistinguishable from an unknown address.
    }
  }
  if (options.ipAddress) await recordFailure(db, "ip", options.ipAddress, now, options.hashingSecret);
  await recordFailure(db, "email", email, now, options.hashingSecret);
  await recordFailure(db, "account", row.account_id, now, options.hashingSecret);
}

async function findEmailToken(
  db: D1Database,
  token: string,
  purpose: "verify" | "password_reset",
  now: number,
): Promise<EmailTokenRow | null> {
  return db
    .prepare(
      `SELECT t.id, t.email_id, e.account_id, e.encrypted_email, e.verified_at, t.expires_at, t.used_at
         FROM email_tokens t
         JOIN emails e ON e.id = t.email_id
         JOIN accounts a ON a.id = e.account_id
        WHERE t.token_hash = ?1 AND t.purpose = ?2
          AND t.used_at IS NULL AND t.expires_at > ?3
          AND e.revoked_at IS NULL AND a.status = 'active'`,
    )
    .bind(await sha256Hex(token), purpose, now)
    .first<EmailTokenRow>();
}

export async function confirmEmail(
  db: D1Database,
  tokenInput: unknown,
  options: AuthOptions = {},
): Promise<void> {
  const now = nowSeconds(options);
  if (typeof tokenInput !== "string" || tokenInput.length < 32) throw publicError("invalid_email_token", 400);
  const token = await findEmailToken(db, tokenInput, "verify", now);
  if (!token) throw publicError("invalid_email_token", 400);
  const consumed = await db
    .prepare("UPDATE email_tokens SET used_at = ?1 WHERE id = ?2 AND purpose = 'verify' AND used_at IS NULL AND expires_at > ?1")
    .bind(now, token.id)
    .run();
  if (changes(consumed) !== 1) throw publicError("invalid_email_token", 400);
  const previousEmail = await db
    .prepare(
      `SELECT id FROM emails
        WHERE account_id = ?1 AND id <> ?2 AND verified_at IS NOT NULL AND revoked_at IS NULL`,
    )
    .bind(token.account_id, token.email_id)
    .first<{ id: string }>();
  const eventType = previousEmail ? "email_changed" : "email_confirmed";
  await db.batch([
    db.prepare(
      `UPDATE email_tokens SET used_at = ?1
        WHERE email_id = ?2 AND purpose = 'verify' AND used_at IS NULL`,
    ).bind(now, token.email_id),
    db.prepare("UPDATE emails SET verified_at = ?1 WHERE id = ?2 AND revoked_at IS NULL").bind(now, token.email_id),
    db.prepare(
      `UPDATE emails SET revoked_at = ?1
        WHERE account_id = ?2 AND id <> ?3 AND revoked_at IS NULL`,
    ).bind(now, token.account_id, token.email_id),
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', ?3, 'email_token', ?4, NULL, NULL, 'success', ?5)`,
    ).bind(randomId(), token.account_id, eventType, options.requestId ?? null, now),
  ]);
}

export async function completePasswordReset(
  db: D1Database,
  tokenInput: unknown,
  newPasswordInput: unknown,
  options: AuthOptions = {},
): Promise<LoginResult> {
  const now = nowSeconds(options);
  const newPassword = validatePassword(newPasswordInput);
  if (typeof tokenInput !== "string" || tokenInput.length < 32) throw publicError("invalid_reset_token", 400);
  const token = await findEmailToken(db, tokenInput, "password_reset", now);
  if (!token) throw publicError("invalid_reset_token", 400);
  const passwordHash = await hashPassword(newPassword);
  const consumed = await db
    .prepare("UPDATE email_tokens SET used_at = ?1 WHERE id = ?2 AND purpose = 'password_reset' AND used_at IS NULL AND expires_at > ?1")
    .bind(now, token.id)
    .run();
  if (changes(consumed) !== 1) throw publicError("invalid_reset_token", 400);
  const resetState = await db.batch([
    db.prepare(
      `UPDATE credentials
          SET password_hash = ?1, password_params_version = ?2,
              password_rehash_required = 0, updated_at = ?3, revoked_at = NULL
        WHERE account_id = ?4`,
    ).bind(passwordHash, AUTH_POLICY.passwordParamsVersion, now, token.account_id),
    db.prepare("UPDATE sessions SET revoked_at = ?1 WHERE account_id = ?2 AND revoked_at IS NULL")
      .bind(now, token.account_id),
  ]);
  if (changes(resetState[0] ?? {}) !== 1) throw publicError("invalid_reset_token", 400);
  await revokeOAuthGrantsForAccount(db, token.account_id, "password_reset", now);
  const session = await createSession(db, token.account_id, now);
  await db.batch([
    db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, NULL, 'account', 'password_reset_completed', 'email_token', ?3, NULL, NULL, 'success', ?4)`,
    ).bind(randomId(), token.account_id, options.requestId ?? null, now),
  ]);
  const account = await db
    .prepare("SELECT public_id FROM accounts WHERE id = ?1")
    .bind(token.account_id)
    .first<{ public_id: string }>();
  if (!account) throw publicError("invalid_reset_token", 400);
  return {
    accountPublicId: account.public_id,
    sessionToken: session.sessionToken,
    csrfToken: session.csrfToken,
    expiresAt: session.expiresAt,
  };
}

export async function changePassword(
  db: D1Database,
  sessionToken: string,
  currentPasswordInput: unknown,
  newPasswordInput: unknown,
  options: AuthOptions = {},
): Promise<LoginResult> {
  const now = nowSeconds(options);
  const session = await requireActiveSession(db, sessionToken, now);
  const currentPassword = validatePassword(currentPasswordInput);
  const newPassword = validatePassword(newPasswordInput);
  await enforceRateLimit(db, "account", session.account_id, now, options.hashingSecret);
  const credential = await db.prepare(
    "SELECT password_hash FROM credentials WHERE account_id = ?1 AND revoked_at IS NULL",
  ).bind(session.account_id).first<{ password_hash: string }>();
  if (!credential || !(await verifyPassword(currentPassword, credential.password_hash))) {
    await recordFailure(db, "account", session.account_id, now, options.hashingSecret);
    throw publicError("invalid_credentials", 401);
  }
  const passwordHash = await hashPassword(newPassword);
  const updated = await db.prepare(
    `UPDATE credentials
        SET password_hash = ?1, password_params_version = ?2,
            password_rehash_required = 0, updated_at = ?3
      WHERE account_id = ?4 AND revoked_at IS NULL`,
  ).bind(passwordHash, AUTH_POLICY.passwordParamsVersion, now, session.account_id).run();
  if (changes(updated) !== 1) throw publicError("invalid_credentials", 401);
  await revokeAllSessions(db, session.account_id, options);
  await revokeOAuthGrantsForAccount(db, session.account_id, "password_change", now);
  const replacement = await createSession(db, session.account_id, now);
  await db.prepare(
    `INSERT INTO audit_events(
       id, account_id, profile_id, actor_kind, event_type, reason_code,
       request_id, resource_hash, input_hash, status, occurred_at
     ) VALUES (?1, ?2, NULL, 'account', 'password_changed', 'current_password', ?3, NULL, NULL, 'success', ?4)`,
  ).bind(randomId(), session.account_id, options.requestId ?? null, now).run();
  await clearFailure(db, "account", session.account_id, options.hashingSecret);
  const account = await db.prepare("SELECT public_id FROM accounts WHERE id = ?1")
    .bind(session.account_id).first<{ public_id: string }>();
  if (!account) throw publicError("unauthorized", 401);
  return {
    accountPublicId: account.public_id,
    sessionToken: replacement.sessionToken,
    csrfToken: replacement.csrfToken,
    expiresAt: replacement.expiresAt,
  };
}
