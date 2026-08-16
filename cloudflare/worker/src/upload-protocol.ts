import { createSHA256, type IHasher } from "hash-wasm";

/**
 * The upload protocol is deliberately independent from the Worker entrypoint.
 * The entrypoint can inject the authenticated profile context once the auth
 * layer has resolved it; no request field is trusted as an owner identifier.
 */

export const FIVE_DB_FILE_NAMES = [
  "score.db",
  "scoredatalog.db",
  "scorelog.db",
  "songdata.db",
  "songinfo.db",
] as const;

export type FiveDbFileName = (typeof FIVE_DB_FILE_NAMES)[number];

export const MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024 * 1024;
export const MAX_TOTAL_SIZE_BYTES = MAX_FILE_SIZE_BYTES * FIVE_DB_FILE_NAMES.length;
export const MIN_R2_PART_SIZE_BYTES = 5 * 1024 * 1024;
export const MAX_R2_PART_SIZE_BYTES = 5 * 1024 * 1024 * 1024;
export const MAX_R2_PARTS = 10_000;
export const DEFAULT_FRAME_SIZE_BYTES = 1024 * 1024;
export const ENVELOPE_VERSION = "envelope-v1";
export const ENVELOPE_TAG_BITS = 128;
export const SQLITE_HEADER = "SQLite format 3\u0000";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;
const KEY_VERSION_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/;
const textEncoder = new TextEncoder();
const sqliteHeaderBytes = textEncoder.encode(SQLITE_HEADER);
const textDecoder = new TextDecoder();
const ENVELOPE_TAG_BYTES = ENVELOPE_TAG_BITS / 8;

export type SubmissionKind = "initial" | "backfill" | "monthly" | "audit";

export type UploadErrorCode =
  | "unauthorized"
  | "invalid_manifest"
  | "invalid_file_set"
  | "invalid_file_name"
  | "invalid_file_size"
  | "invalid_file_digest"
  | "sqlite_header_required"
  | "snapshot_sidecar_rejected"
  | "invalid_month"
  | "invalid_submission_kind"
  | "invalid_profile"
  | "session_not_found"
  | "session_expired"
  | "session_not_active"
  | "part_number_invalid"
  | "part_conflict"
  | "part_checksum_mismatch"
  | "part_size_mismatch"
  | "part_too_large"
  | "part_size_invalid"
  | "multipart_incomplete"
  | "multipart_part_size_invalid"
  | "object_integrity_failed"
  | "encryption_metadata_invalid"
  | "encryption_key_invalid"
  | "upload_failed"
  | "internal_error";

export class UploadProtocolError extends Error {
  constructor(
    public readonly code: UploadErrorCode,
    public readonly status = 400,
  ) {
    super(code);
    this.name = "UploadProtocolError";
  }
}

export interface UploadFileManifest {
  fileName: FiveDbFileName;
  sha256: string;
  sizeBytes: number;
}

export interface UploadManifest {
  manifestId: string;
  month: string;
  submissionKind: SubmissionKind;
  sourceGeneration: string;
  gameMode: "SP7";
  trustDomain: "official";
  files: readonly UploadFileManifest[];
}

export interface UploadContext {
  /** Resolved from the authenticated session; never from the request body. */
  profileId: string;
  accountId?: string;
}

export interface R2UploadedPartLike {
  partNumber: number;
  etag: string;
}

export interface R2MultipartUploadLike {
  readonly key: string;
  readonly uploadId: string;
  uploadPart(
    partNumber: number,
    value: ReadableStream<Uint8Array>,
  ): Promise<R2UploadedPartLike>;
  complete(parts: R2UploadedPartLike[]): Promise<unknown>;
  abort(): Promise<void>;
}

export interface R2ObjectLike {
  readonly body?: ReadableStream<Uint8Array>;
  readonly customMetadata?: Record<string, string>;
}

export interface R2BucketLike {
  createMultipartUpload(
    key: string,
    options?: { customMetadata?: Record<string, string> },
  ): Promise<R2MultipartUploadLike>;
  resumeMultipartUpload(key: string, uploadId: string): R2MultipartUploadLike;
  get(key: string): Promise<R2ObjectLike | null>;
  delete(key: string): Promise<void>;
}

export interface ProfileEnvelopeRecord {
  keyVersion: string;
  wrapNonce: string;
  wrappedDek: string;
}

export interface ObjectEncryptionMetadata extends ProfileEnvelopeRecord {
  envelopeVersion: typeof ENVELOPE_VERSION;
  profileScope: string;
  fileSha256: string;
  plaintextSizeBytes: number;
  baseNonce: string;
  frameSizeBytes: number;
  tagBits: typeof ENVELOPE_TAG_BITS;
}

export interface UploadPartRecord {
  partNumber: number;
  etag: string;
  sha256: string;
  sizeBytes: number;
}

export interface StoredFileRef {
  profileScope: string;
  sha256: string;
  sizeBytes: number;
  objectKey: string;
  keyVersion: string;
}

export type UploadFileState = "upload_required" | "deduplicated" | "completed";
export type UploadSessionState = "active" | "completed" | "aborted" | "expired";

export interface UploadFileSession {
  fileName: FiveDbFileName;
  sha256: string;
  sizeBytes: number;
  state: UploadFileState;
  objectKey: string;
  /** The object created by this session, if this file was not deduplicated. */
  createdObjectKey?: string;
  r2UploadId?: string;
  encryption?: ObjectEncryptionMetadata;
  dedupRef?: StoredFileRef;
  parts: UploadPartRecord[];
}

export interface UploadSessionRecord {
  uploadId: string;
  profileScope: string;
  manifestId: string;
  manifestSha256: string;
  month: string;
  submissionKind: SubmissionKind;
  sourceGeneration: string;
  envelope: ProfileEnvelopeRecord;
  files: UploadFileSession[];
  state: UploadSessionState;
  createdAt: number;
  expiresAt: number;
  completedAt?: number;
}

export interface UploadSessionStore {
  findSessionByManifest(
    profileScope: string,
    manifestSha256: string,
  ): Promise<UploadSessionRecord | null>;
  getSession(profileScope: string, uploadId: string): Promise<UploadSessionRecord | null>;
  createSession(session: UploadSessionRecord): Promise<void>;
  markFileCompleted(
    profileScope: string,
    uploadId: string,
    fileName: FiveDbFileName,
    ref: StoredFileRef,
  ): Promise<void>;
  recordPart(
    profileScope: string,
    uploadId: string,
    fileName: FiveDbFileName,
    part: UploadPartRecord,
  ): Promise<"inserted" | "existing">;
  markCompleted(profileScope: string, uploadId: string, completedAt: number): Promise<void>;
  markAborted(profileScope: string, uploadId: string, state?: "aborted" | "expired"): Promise<void>;
  getProfileEnvelope(profileScope: string): Promise<ProfileEnvelopeRecord | null>;
  putProfileEnvelope(profileScope: string, envelope: ProfileEnvelopeRecord): Promise<ProfileEnvelopeRecord>;
  findDedup(profileScope: string, sha256: string, sizeBytes: number): Promise<StoredFileRef | null>;
  putDedup(ref: StoredFileRef): Promise<StoredFileRef>;
  removeDedup(ref: StoredFileRef): Promise<void>;
  listExpired(now: number): Promise<UploadSessionRecord[]>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function fail(code: UploadErrorCode, status = 400): never {
  throw new UploadProtocolError(code, status);
}

function readString(value: unknown, code: UploadErrorCode): string {
  if (typeof value !== "string" || value.length === 0) fail(code);
  return value;
}

function readBoolean(value: unknown, code: UploadErrorCode): boolean {
  if (typeof value !== "boolean") fail(code);
  return value;
}

function readInteger(value: unknown, code: UploadErrorCode): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) fail(code);
  return value;
}

function ensureUuid(value: unknown, code: UploadErrorCode): string {
  const result = readString(value, code);
  if (!UUID_PATTERN.test(result)) fail(code);
  return result.toLowerCase();
}

function ensureSha256(value: unknown): string {
  const result = readString(value, "invalid_file_digest");
  if (!SHA256_PATTERN.test(result)) fail("invalid_file_digest");
  return result;
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlDecode(value: string, code: UploadErrorCode = "encryption_metadata_invalid"): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) fail(code);
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  try {
    return Uint8Array.from(atob(normalized), (character) => character.charCodeAt(0));
  } catch {
    fail(code);
  }
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((sum, part) => sum + part.byteLength, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.byteLength;
  }
  return result;
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.slice().buffer;
}

function toUint8Array(value: unknown): Uint8Array {
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  fail("part_size_mismatch", 422);
}

function asReadableStream(value: ReadableStream<Uint8Array> | Uint8Array): ReadableStream<Uint8Array> {
  if (value instanceof ReadableStream) return value;
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(value);
      controller.close();
    },
  });
}

export async function sha256Hex(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? textEncoder.encode(value) : value;
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", toArrayBuffer(bytes)));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  fail("invalid_manifest");
}

export function normalizeUploadManifest(value: unknown): UploadManifest {
  if (!isRecord(value)) fail("invalid_manifest");
  if ("account_id" in value || "profile_id" in value || "object_key" in value) {
    fail("invalid_manifest");
  }

  const manifestId = ensureUuid(value.manifest_id, "invalid_manifest");
  const sourceGeneration = ensureUuid(value.source_generation, "invalid_manifest");
  const month = readString(value.month, "invalid_month");
  if (!MONTH_PATTERN.test(month)) fail("invalid_month");
  const submissionKind = readString(value.submission_kind, "invalid_submission_kind") as SubmissionKind;
  if (!["initial", "backfill", "monthly", "audit"].includes(submissionKind)) {
    fail("invalid_submission_kind");
  }
  if (value.game_mode !== "SP7" || value.trust_domain !== "official") fail("invalid_manifest");
  if (readBoolean(value.static_snapshot, "invalid_manifest") !== true) fail("invalid_manifest");
  if (readBoolean(value.wal_present, "snapshot_sidecar_rejected")) fail("snapshot_sidecar_rejected");
  if (readBoolean(value.shm_present, "snapshot_sidecar_rejected")) fail("snapshot_sidecar_rejected");
  if (readBoolean(value.contains_bms, "invalid_manifest")) fail("invalid_manifest");
  if (readBoolean(value.contains_replay, "invalid_manifest")) fail("invalid_manifest");
  if (readBoolean(value.aggregate_eligible, "invalid_manifest")) fail("invalid_manifest");

  if (!Array.isArray(value.files) || value.files.length !== FIVE_DB_FILE_NAMES.length) {
    fail("invalid_file_set");
  }
  const files: UploadFileManifest[] = [];
  const names = new Set<string>();
  let totalSize = 0;
  for (const item of value.files) {
    if (!isRecord(item)) fail("invalid_file_set");
    const fileName = readString(item.file_name, "invalid_file_name");
    if (!(FIVE_DB_FILE_NAMES as readonly string[]).includes(fileName)) fail("invalid_file_name");
    if (names.has(fileName)) fail("invalid_file_set");
    names.add(fileName);
    if ("object_key" in item || fileName.endsWith("-wal") || fileName.endsWith("-shm")) {
      fail("snapshot_sidecar_rejected");
    }
    const sha256 = ensureSha256(item.sha256);
    const sizeBytes = readInteger(item.size_bytes, "invalid_file_size");
    if (sizeBytes < 1 || sizeBytes > MAX_FILE_SIZE_BYTES) fail("invalid_file_size");
    totalSize += sizeBytes;
    files.push({ fileName: fileName as FiveDbFileName, sha256, sizeBytes });
  }
  if (totalSize > MAX_TOTAL_SIZE_BYTES || names.size !== FIVE_DB_FILE_NAMES.length) fail("invalid_file_set");
  for (const expected of FIVE_DB_FILE_NAMES) if (!names.has(expected)) fail("invalid_file_set");

  files.sort((left, right) => FIVE_DB_FILE_NAMES.indexOf(left.fileName) - FIVE_DB_FILE_NAMES.indexOf(right.fileName));
  return {
    manifestId,
    month,
    submissionKind,
    sourceGeneration,
    gameMode: "SP7",
    trustDomain: "official",
    files,
  };
}

export async function manifestSha256(manifest: UploadManifest): Promise<string> {
  return sha256Hex(canonicalJson({
    contract: "five-db-upload-manifest",
    schema_version: "1",
    manifest_id: manifest.manifestId,
    month: manifest.month,
    submission_kind: manifest.submissionKind,
    source_generation: manifest.sourceGeneration,
    game_mode: manifest.gameMode,
    trust_domain: manifest.trustDomain,
    files: manifest.files.map((file) => ({
      file_name: file.fileName,
      sha256: file.sha256,
      size_bytes: file.sizeBytes,
    })),
    snapshot: { static_snapshot: true, wal_present: false, shm_present: false },
    privacy: { contains_bms: false, contains_replay: false, aggregate_eligible: false },
  }));
}

export async function profileScope(profileId: string): Promise<string> {
  if (!UUID_PATTERN.test(profileId)) fail("invalid_profile");
  return (await sha256Hex(`oraja/profile-scope/v1:${profileId.toLowerCase()}`)).slice(0, 32);
}

function randomBytes(length: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(length));
}

function randomUuid(): string {
  return crypto.randomUUID();
}

function validateKeyVersion(value: string): string {
  if (!KEY_VERSION_PATTERN.test(value)) fail("encryption_key_invalid", 500);
  return value;
}

function validateMasterKey(value: Uint8Array): Uint8Array {
  if (value.byteLength !== 32) fail("encryption_key_invalid", 500);
  return value.slice();
}

export function parseEnvelopeMasterKey(value: string): Uint8Array {
  const trimmed = value.trim();
  if (/^[a-f0-9]{64}$/i.test(trimmed)) {
    const bytes = new Uint8Array(32);
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Number.parseInt(trimmed.slice(index * 2, index * 2 + 2), 16);
    }
    return bytes;
  }
  const decoded = base64UrlDecode(trimmed.replace(/\+/g, "-").replace(/\//g, "_"), "encryption_key_invalid");
  if (decoded.byteLength !== 32) fail("encryption_key_invalid", 500);
  return decoded;
}

async function importAesKey(bytes: Uint8Array, usages: KeyUsage[]): Promise<CryptoKey> {
  try {
    return await crypto.subtle.importKey("raw", toArrayBuffer(bytes), { name: "AES-GCM" }, false, usages);
  } catch {
    fail("encryption_key_invalid", 500);
  }
}

function wrapAdditionalData(profileScopeValue: string, keyVersion: string): ArrayBuffer {
  return toArrayBuffer(textEncoder.encode(`${ENVELOPE_VERSION}|profile|${profileScopeValue}|${keyVersion}`));
}

function frameAdditionalData(
  profileScopeValue: string,
  fileSha256: string,
  keyVersion: string,
  partNumber: number,
  frameNumber: number,
): ArrayBuffer {
  return toArrayBuffer(textEncoder.encode(
    `${ENVELOPE_VERSION}|object|${profileScopeValue}|${fileSha256}|${keyVersion}|${partNumber}|${frameNumber}`,
  ));
}

function nonceForFrame(baseNonce: Uint8Array, partNumber: number, frameNumber: number): Uint8Array {
  if (baseNonce.byteLength !== 12 || partNumber < 1 || partNumber > 0xffff || frameNumber > 0xffff) {
    fail("encryption_metadata_invalid", 500);
  }
  const nonce = baseNonce.slice();
  const view = new DataView(nonce.buffer, nonce.byteOffset, nonce.byteLength);
  const counter = ((partNumber & 0xffff) << 16) | (frameNumber & 0xffff);
  view.setUint32(8, view.getUint32(8) ^ counter);
  return nonce;
}

export class EnvelopeCrypto {
  readonly keyVersion: string;
  private readonly masterKey: Uint8Array;
  private readonly kek: Promise<CryptoKey>;

  constructor(options: { keyVersion: string; masterKey: Uint8Array }) {
    this.keyVersion = validateKeyVersion(options.keyVersion);
    this.masterKey = validateMasterKey(options.masterKey);
    this.kek = importAesKey(this.masterKey, ["encrypt", "decrypt"]);
  }

  static fromSecret(secret: string, keyVersion = "kek-v1"): EnvelopeCrypto {
    return new EnvelopeCrypto({ keyVersion, masterKey: parseEnvelopeMasterKey(secret) });
  }

  async createProfileEnvelope(profileScopeValue: string): Promise<ProfileEnvelopeRecord> {
    const wrapNonce = randomBytes(12);
    const dek = randomBytes(32);
    const wrapped = await crypto.subtle.encrypt(
      {
        name: "AES-GCM",
        iv: toArrayBuffer(wrapNonce),
        additionalData: wrapAdditionalData(profileScopeValue, this.keyVersion),
        tagLength: ENVELOPE_TAG_BITS,
      },
      await this.kek,
      toArrayBuffer(dek),
    );
    return {
      keyVersion: this.keyVersion,
      wrapNonce: base64UrlEncode(wrapNonce),
      wrappedDek: base64UrlEncode(new Uint8Array(wrapped)),
    };
  }

  async unwrapProfileDek(profileScopeValue: string, envelope: ProfileEnvelopeRecord): Promise<CryptoKey> {
    if (envelope.keyVersion !== this.keyVersion) fail("encryption_key_invalid", 500);
    const wrapNonce = base64UrlDecode(envelope.wrapNonce);
    const wrappedDek = base64UrlDecode(envelope.wrappedDek);
    if (wrapNonce.byteLength !== 12 || wrappedDek.byteLength !== 32 + ENVELOPE_TAG_BYTES) {
      fail("encryption_metadata_invalid", 500);
    }
    try {
      const rawDek = new Uint8Array(await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(wrapNonce),
          additionalData: wrapAdditionalData(profileScopeValue, envelope.keyVersion),
          tagLength: ENVELOPE_TAG_BITS,
        },
        await this.kek,
        toArrayBuffer(wrappedDek),
      ));
      if (rawDek.byteLength !== 32) fail("encryption_metadata_invalid", 500);
      return await importAesKey(rawDek, ["encrypt", "decrypt"]);
    } catch (error) {
      if (error instanceof UploadProtocolError) throw error;
      fail("encryption_metadata_invalid", 500);
    }
  }

  createObjectMetadata(
    profileScopeValue: string,
    envelope: ProfileEnvelopeRecord,
    file: UploadFileManifest,
  ): ObjectEncryptionMetadata {
    return {
      envelopeVersion: ENVELOPE_VERSION,
      profileScope: profileScopeValue,
      fileSha256: file.sha256,
      plaintextSizeBytes: file.sizeBytes,
      keyVersion: envelope.keyVersion,
      wrapNonce: envelope.wrapNonce,
      wrappedDek: envelope.wrappedDek,
      baseNonce: base64UrlEncode(randomBytes(12)),
      frameSizeBytes: DEFAULT_FRAME_SIZE_BYTES,
      tagBits: ENVELOPE_TAG_BITS,
    };
  }

  toR2Metadata(metadata: ObjectEncryptionMetadata): Record<string, string> {
    return {
      "oraja-envelope": metadata.envelopeVersion,
      "oraja-profile-scope": metadata.profileScope,
      "oraja-key-version": metadata.keyVersion,
      "oraja-wrap-nonce": metadata.wrapNonce,
      "oraja-wrapped-dek": metadata.wrappedDek,
      "oraja-base-nonce": metadata.baseNonce,
      "oraja-frame-size": String(metadata.frameSizeBytes),
      "oraja-tag-bits": String(metadata.tagBits),
      "oraja-plaintext-sha256": metadata.fileSha256,
      "oraja-plaintext-size": String(metadata.plaintextSizeBytes),
    };
  }

  assertR2Metadata(actual: Record<string, string> | undefined, expected: ObjectEncryptionMetadata): void {
    const required = this.toR2Metadata(expected);
    if (!actual) fail("encryption_metadata_invalid", 500);
    for (const [key, value] of Object.entries(required)) {
      if (actual[key] !== value) fail("encryption_metadata_invalid", 500);
    }
  }

  async encryptFrame(
    profileScopeValue: string,
    metadata: ObjectEncryptionMetadata,
    partNumber: number,
    frameNumber: number,
    plaintext: Uint8Array,
  ): Promise<Uint8Array> {
    const key = await this.unwrapProfileDek(profileScopeValue, metadata);
    try {
      const encrypted = await crypto.subtle.encrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(nonceForFrame(base64UrlDecode(metadata.baseNonce), partNumber, frameNumber)),
          additionalData: frameAdditionalData(
            profileScopeValue,
            metadata.fileSha256,
            metadata.keyVersion,
            partNumber,
            frameNumber,
          ),
          tagLength: ENVELOPE_TAG_BITS,
        },
        key,
        toArrayBuffer(plaintext),
      );
      return new Uint8Array(encrypted);
    } catch {
      fail("encryption_metadata_invalid", 500);
    }
  }

  async decryptFrame(
    profileScopeValue: string,
    metadata: ObjectEncryptionMetadata,
    partNumber: number,
    frameNumber: number,
    ciphertext: Uint8Array,
  ): Promise<Uint8Array> {
    const key = await this.unwrapProfileDek(profileScopeValue, metadata);
    try {
      const plaintext = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: toArrayBuffer(nonceForFrame(base64UrlDecode(metadata.baseNonce), partNumber, frameNumber)),
          additionalData: frameAdditionalData(
            profileScopeValue,
            metadata.fileSha256,
            metadata.keyVersion,
            partNumber,
            frameNumber,
          ),
          tagLength: ENVELOPE_TAG_BITS,
        },
        key,
        toArrayBuffer(ciphertext),
      );
      return new Uint8Array(plaintext);
    } catch {
      fail("object_integrity_failed", 422);
    }
  }
}

interface PreparedEncryptedPart {
  stream: ReadableStream<Uint8Array>;
  stats: Promise<{ sha256: string; sizeBytes: number; sqliteHeader: boolean }>;
}

async function prepareEncryptedPart(
  source: ReadableStream<Uint8Array>,
  profileScopeValue: string,
  metadata: ObjectEncryptionMetadata,
  partNumber: number,
  cryptoBox: EnvelopeCrypto,
): Promise<PreparedEncryptedPart> {
  const hasher: IHasher = await createSHA256();
  hasher.init();
  const reader = source.getReader();
  let buffered = new Uint8Array(0);
  let sourceDone = false;
  let frameNumber = 0;
  let sizeBytes = 0;
  let firstBytes = new Uint8Array(0);
  let settled = false;
  let resolveStats!: (value: { sha256: string; sizeBytes: number; sqliteHeader: boolean }) => void;
  let rejectStats!: (error: unknown) => void;
  const stats = new Promise<{ sha256: string; sizeBytes: number; sqliteHeader: boolean }>((resolve, reject) => {
    resolveStats = resolve;
    rejectStats = reject;
  });

  async function nextFrame(): Promise<Uint8Array | null> {
    while (!sourceDone && buffered.byteLength < metadata.frameSizeBytes) {
      const next = await reader.read();
      if (next.done) {
        sourceDone = true;
        break;
      }
      const chunk = toUint8Array(next.value);
      if (chunk.byteLength > 0) buffered = new Uint8Array(concatBytes(buffered, chunk));
    }
    if (buffered.byteLength === 0 && sourceDone) return null;
    const frame = buffered.slice(0, metadata.frameSizeBytes);
    buffered = buffered.slice(frame.byteLength);
    return frame;
  }

  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (settled) {
        controller.close();
        return;
      }
      try {
        const frame = await nextFrame();
        if (!frame) {
          settled = true;
          resolveStats({
            sha256: hasher.digest(),
            sizeBytes,
            sqliteHeader: firstBytes.byteLength >= sqliteHeaderBytes.byteLength
              && firstBytes.slice(0, sqliteHeaderBytes.byteLength).every((byte, index) => byte === sqliteHeaderBytes[index]),
          });
          controller.close();
          return;
        }
        hasher.update(frame);
        sizeBytes += frame.byteLength;
        if (firstBytes.byteLength < sqliteHeaderBytes.byteLength) {
          firstBytes = concatBytes(firstBytes, frame).slice(0, sqliteHeaderBytes.byteLength);
        }
        frameNumber += 1;
        if (frameNumber > 0xffff) fail("part_too_large", 413);
        const encrypted = await cryptoBox.encryptFrame(
          profileScopeValue,
          metadata,
          partNumber,
          frameNumber - 1,
          frame,
        );
        const length = new Uint8Array(4);
        new DataView(length.buffer).setUint32(0, frame.byteLength);
        controller.enqueue(concatBytes(length, encrypted));
      } catch (error) {
        settled = true;
        rejectStats(error);
        controller.error(error);
      }
    },
    async cancel(reason) {
      await reader.cancel(reason);
    },
  });
  return { stream, stats };
}

class StreamByteReader {
  private readonly reader: ReadableStreamDefaultReader<Uint8Array>;
  private buffered = new Uint8Array(0);
  private done = false;

  constructor(source: ReadableStream<Uint8Array>) {
    this.reader = source.getReader();
  }

  async readExact(length: number): Promise<Uint8Array> {
    while (!this.done && this.buffered.byteLength < length) {
      const next = await this.reader.read();
      if (next.done) {
        this.done = true;
        break;
      }
      const chunk = toUint8Array(next.value);
      if (chunk.byteLength > 0) this.buffered = new Uint8Array(concatBytes(this.buffered, chunk));
    }
    if (this.buffered.byteLength < length) fail("object_integrity_failed", 422);
    const result = this.buffered.slice(0, length);
    this.buffered = this.buffered.slice(length);
    return result;
  }

  async assertEof(): Promise<void> {
    if (this.buffered.byteLength > 0) fail("object_integrity_failed", 422);
    while (!this.done) {
      const next = await this.reader.read();
      if (next.done) {
        this.done = true;
        return;
      }
      if (toUint8Array(next.value).byteLength > 0) fail("object_integrity_failed", 422);
    }
  }
}

async function verifyEncryptedObject(
  object: R2ObjectLike,
  file: UploadFileSession,
  profileScopeValue: string,
  cryptoBox: EnvelopeCrypto,
): Promise<void> {
  if (!file.encryption || !object.body) fail("encryption_metadata_invalid", 500);
  cryptoBox.assertR2Metadata(object.customMetadata, file.encryption);
  const reader = new StreamByteReader(object.body);
  const totalHasher: IHasher = await createSHA256();
  totalHasher.init();
  let totalSize = 0;
  let firstBytes = new Uint8Array(0);

  const parts = [...file.parts].sort((left, right) => left.partNumber - right.partNumber);
  for (const part of parts) {
    const partHasher: IHasher = await createSHA256();
    partHasher.init();
    let remaining = part.sizeBytes;
    let frameNumber = 0;
    while (remaining > 0) {
      const header = await reader.readExact(4);
      const frameSize = new DataView(header.buffer, header.byteOffset, header.byteLength).getUint32(0);
      if (frameSize < 1 || frameSize > file.encryption.frameSizeBytes || frameSize > remaining) {
        fail("object_integrity_failed", 422);
      }
      const encrypted = await reader.readExact(frameSize + ENVELOPE_TAG_BYTES);
      const plaintext = await cryptoBox.decryptFrame(
        profileScopeValue,
        file.encryption,
        part.partNumber,
        frameNumber,
        encrypted,
      );
      if (plaintext.byteLength !== frameSize) fail("object_integrity_failed", 422);
      partHasher.update(plaintext);
      totalHasher.update(plaintext);
      totalSize += plaintext.byteLength;
      remaining -= plaintext.byteLength;
      frameNumber += 1;
      if (frameNumber > 0xffff) fail("object_integrity_failed", 422);
      if (firstBytes.byteLength < sqliteHeaderBytes.byteLength) {
        firstBytes = concatBytes(firstBytes, plaintext).slice(0, sqliteHeaderBytes.byteLength);
      }
    }
    if (partHasher.digest() !== part.sha256 || part.sizeBytes < 1) fail("object_integrity_failed", 422);
  }
  await reader.assertEof();
  const sqliteHeaderValid = firstBytes.byteLength >= sqliteHeaderBytes.byteLength
    && firstBytes.slice(0, sqliteHeaderBytes.byteLength).every((byte, index) => byte === sqliteHeaderBytes[index]);
  if (!sqliteHeaderValid) fail("sqlite_header_required", 422);
  if (totalSize !== file.sizeBytes || totalHasher.digest() !== file.sha256) {
    fail("object_integrity_failed", 422);
  }
}

/**
 * Decrypt a completed upload object as a verified plaintext stream.
 *
 * The stream fails closed unless every persisted multipart boundary, frame
 * tag, part digest, total digest, size, and R2 metadata value agrees with the
 * immutable upload session.  Consumers therefore never receive an
 * unverified successful EOF.
 */
export function decryptCompletedUploadObject(
  object: R2ObjectLike,
  file: UploadFileSession,
  profileScopeValue: string,
  cryptoBox: EnvelopeCrypto,
): ReadableStream<Uint8Array> {
  async function* plaintext(): AsyncGenerator<Uint8Array> {
    if (!file.encryption || !object.body) fail("encryption_metadata_invalid", 500);
    cryptoBox.assertR2Metadata(object.customMetadata, file.encryption);
    const reader = new StreamByteReader(object.body);
    const totalHasher: IHasher = await createSHA256();
    totalHasher.init();
    let totalSize = 0;
    const parts = [...file.parts].sort((left, right) => left.partNumber - right.partNumber);
    if (parts.length === 0) fail("object_integrity_failed", 422);
    for (const part of parts) {
      const partHasher: IHasher = await createSHA256();
      partHasher.init();
      let remaining = part.sizeBytes;
      let frameNumber = 0;
      while (remaining > 0) {
        const header = await reader.readExact(4);
        const frameSize = new DataView(header.buffer, header.byteOffset, header.byteLength).getUint32(0);
        if (frameSize < 1 || frameSize > file.encryption.frameSizeBytes || frameSize > remaining) {
          fail("object_integrity_failed", 422);
        }
        const encrypted = await reader.readExact(frameSize + ENVELOPE_TAG_BYTES);
        const plaintextFrame = await cryptoBox.decryptFrame(
          profileScopeValue,
          file.encryption,
          part.partNumber,
          frameNumber,
          encrypted,
        );
        if (plaintextFrame.byteLength !== frameSize) fail("object_integrity_failed", 422);
        partHasher.update(plaintextFrame);
        totalHasher.update(plaintextFrame);
        totalSize += plaintextFrame.byteLength;
        remaining -= plaintextFrame.byteLength;
        frameNumber += 1;
        yield plaintextFrame;
      }
      if (partHasher.digest() !== part.sha256) fail("object_integrity_failed", 422);
    }
    await reader.assertEof();
    if (totalSize !== file.sizeBytes || totalHasher.digest() !== file.sha256) {
      fail("object_integrity_failed", 422);
    }
  }
  const iterator = plaintext();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const item = await iterator.next();
        if (item.done) controller.close();
        else controller.enqueue(item.value);
      } catch (error) {
        controller.error(error);
      }
    },
    async cancel() {
      await iterator.return(undefined);
    },
  });
}

function cloneSession(session: UploadSessionRecord): UploadSessionRecord {
  return JSON.parse(JSON.stringify(session)) as UploadSessionRecord;
}

/** In-memory store used by the no-R2 test harness and local protocol drills. */
export class MemoryUploadSessionStore implements UploadSessionStore {
  private readonly sessions = new Map<string, UploadSessionRecord>();
  private readonly envelopes = new Map<string, ProfileEnvelopeRecord>();
  private readonly dedup = new Map<string, StoredFileRef>();

  async findSessionByManifest(profileScopeValue: string, digest: string): Promise<UploadSessionRecord | null> {
    const matches = [...this.sessions.values()]
      .filter((session) => session.profileScope === profileScopeValue && session.manifestSha256 === digest)
      .sort((left, right) => right.createdAt - left.createdAt);
    return matches[0] ? cloneSession(matches[0]) : null;
  }

  async getSession(profileScopeValue: string, uploadId: string): Promise<UploadSessionRecord | null> {
    const session = this.sessions.get(uploadId);
    if (!session || session.profileScope !== profileScopeValue) return null;
    return cloneSession(session);
  }

  async createSession(session: UploadSessionRecord): Promise<void> {
    this.sessions.set(session.uploadId, cloneSession(session));
  }

  async markFileCompleted(
    profileScopeValue: string,
    uploadId: string,
    fileName: FiveDbFileName,
    ref: StoredFileRef,
  ): Promise<void> {
    const session = this.requireSession(profileScopeValue, uploadId);
    const file = session.files.find((item) => item.fileName === fileName);
    if (!file) fail("session_not_found", 404);
    file.state = "completed";
    file.objectKey = ref.objectKey;
    file.dedupRef = ref;
    this.sessions.set(uploadId, session);
  }

  async recordPart(
    profileScopeValue: string,
    uploadId: string,
    fileName: FiveDbFileName,
    part: UploadPartRecord,
  ): Promise<"inserted" | "existing"> {
    const session = this.requireSession(profileScopeValue, uploadId);
    const file = session.files.find((item) => item.fileName === fileName);
    if (!file) fail("session_not_found", 404);
    const existing = file.parts.find((item) => item.partNumber === part.partNumber);
    if (existing) {
      if (existing.sha256 !== part.sha256 || existing.sizeBytes !== part.sizeBytes) fail("part_conflict", 409);
      return "existing";
    }
    file.parts.push(part);
    file.parts.sort((left, right) => left.partNumber - right.partNumber);
    this.sessions.set(uploadId, session);
    return "inserted";
  }

  async markCompleted(profileScopeValue: string, uploadId: string, completedAt: number): Promise<void> {
    const session = this.requireSession(profileScopeValue, uploadId);
    session.state = "completed";
    session.completedAt = completedAt;
    this.sessions.set(uploadId, session);
  }

  async markAborted(profileScopeValue: string, uploadId: string, state: "aborted" | "expired" = "aborted"): Promise<void> {
    const session = this.requireSession(profileScopeValue, uploadId);
    session.state = state;
    this.sessions.set(uploadId, session);
  }

  async getProfileEnvelope(profileScopeValue: string): Promise<ProfileEnvelopeRecord | null> {
    const envelope = this.envelopes.get(profileScopeValue);
    return envelope ? { ...envelope } : null;
  }

  async putProfileEnvelope(profileScopeValue: string, envelope: ProfileEnvelopeRecord): Promise<ProfileEnvelopeRecord> {
    const existing = this.envelopes.get(profileScopeValue);
    if (existing) return { ...existing };
    this.envelopes.set(profileScopeValue, { ...envelope });
    return { ...envelope };
  }

  async findDedup(profileScopeValue: string, digest: string, sizeBytes: number): Promise<StoredFileRef | null> {
    const ref = this.dedup.get(`${profileScopeValue}:${digest}:${sizeBytes}`);
    return ref ? { ...ref } : null;
  }

  async putDedup(ref: StoredFileRef): Promise<StoredFileRef> {
    const key = `${ref.profileScope}:${ref.sha256}:${ref.sizeBytes}`;
    const existing = this.dedup.get(key);
    if (existing) return { ...existing };
    this.dedup.set(key, { ...ref });
    return { ...ref };
  }

  async removeDedup(ref: StoredFileRef): Promise<void> {
    const key = `${ref.profileScope}:${ref.sha256}:${ref.sizeBytes}`;
    const existing = this.dedup.get(key);
    if (existing?.objectKey === ref.objectKey) this.dedup.delete(key);
  }

  async listExpired(now: number): Promise<UploadSessionRecord[]> {
    return [...this.sessions.values()]
      .filter((session) => session.state === "active" && session.expiresAt <= now)
      .map(cloneSession);
  }

  private requireSession(profileScopeValue: string, uploadId: string): UploadSessionRecord {
    const session = this.sessions.get(uploadId);
    if (!session || session.profileScope !== profileScopeValue) fail("session_not_found", 404);
    return session;
  }
}

export interface UploadServiceOptions {
  now?: () => number;
  sessionTtlSeconds?: number;
  minimumPartSizeBytes?: number;
  frameSizeBytes?: number;
}

export interface UploadFileStatus {
  fileName: FiveDbFileName;
  sha256: string;
  sizeBytes: number;
  state: UploadFileState;
  uploadedParts: number[];
}

export interface UploadStatus {
  uploadId: string;
  manifestSha256: string;
  month: string;
  submissionKind: SubmissionKind;
  state: UploadSessionState;
  expiresAt: number;
  files: UploadFileStatus[];
}

export interface StartUploadResult extends UploadStatus {
  unchanged: boolean;
}

function nowSeconds(options: UploadServiceOptions): number {
  return options.now ? Math.floor(options.now()) : Math.floor(Date.now() / 1000);
}

function statusFromSession(session: UploadSessionRecord, unchanged = false): StartUploadResult {
  return {
    uploadId: session.uploadId,
    manifestSha256: session.manifestSha256,
    month: session.month,
    submissionKind: session.submissionKind,
    state: session.state,
    expiresAt: session.expiresAt,
    unchanged,
    files: session.files.map((file) => ({
      fileName: file.fileName,
      sha256: file.sha256,
      sizeBytes: file.sizeBytes,
      state: file.state,
      uploadedParts: file.parts.map((part) => part.partNumber).sort((left, right) => left - right),
    })),
  };
}

function ensureActive(session: UploadSessionRecord, now: number): void {
  if (session.state === "completed") return;
  if (session.state !== "active") fail("session_not_active", 409);
  if (session.expiresAt <= now) fail("session_expired", 410);
}

function ensurePartNumber(partNumber: number): void {
  if (!Number.isSafeInteger(partNumber) || partNumber < 1 || partNumber > MAX_R2_PARTS) {
    fail("part_number_invalid");
  }
}

function validatePartLayout(file: UploadFileSession, minimumPartSizeBytes: number): void {
  const parts = [...file.parts].sort((left, right) => left.partNumber - right.partNumber);
  if (parts.length === 0) fail("multipart_incomplete", 409);
  let total = 0;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (part.partNumber !== index + 1) fail("multipart_incomplete", 409);
    if (part.sizeBytes < 1 || part.sizeBytes > MAX_R2_PART_SIZE_BYTES) fail("part_too_large", 413);
    if (index < parts.length - 1 && part.sizeBytes < minimumPartSizeBytes) {
      fail("multipart_part_size_invalid", 422);
    }
    total += part.sizeBytes;
  }
  if (total !== file.sizeBytes) fail("part_size_mismatch", 422);
}

export class UploadService {
  private readonly sessionTtlSeconds: number;
  private readonly minimumPartSizeBytes: number;
  private readonly nowProvider: () => number;

  constructor(
    private readonly bucket: R2BucketLike,
    private readonly store: UploadSessionStore,
    private readonly cryptoBox: EnvelopeCrypto,
    options: UploadServiceOptions = {},
  ) {
    this.sessionTtlSeconds = options.sessionTtlSeconds ?? 24 * 60 * 60;
    this.minimumPartSizeBytes = options.minimumPartSizeBytes ?? MIN_R2_PART_SIZE_BYTES;
    this.nowProvider = options.now ?? (() => Date.now() / 1000);
    if (this.minimumPartSizeBytes < 1 || this.minimumPartSizeBytes > MAX_R2_PART_SIZE_BYTES) {
      fail("part_size_invalid", 500);
    }
  }

  async start(context: UploadContext, rawManifest: unknown): Promise<StartUploadResult> {
    const manifest = normalizeUploadManifest(rawManifest);
    const scope = await profileScope(context.profileId);
    const digest = await manifestSha256(manifest);
    const existing = await this.store.findSessionByManifest(scope, digest);
    const now = Math.floor(this.nowProvider());
    if (existing && (existing.state === "active" || existing.state === "completed")) {
      ensureActive(existing, now);
      return statusFromSession(existing, existing.state === "completed");
    }

    const existingEnvelope = await this.store.getProfileEnvelope(scope);
    const envelope = existingEnvelope ?? await this.cryptoBox.createProfileEnvelope(scope);
    const persistedEnvelope = existingEnvelope ?? await this.store.putProfileEnvelope(scope, envelope);
    const uploadId = randomUuid();
    const files: UploadFileSession[] = [];
    const createdMultiparts: R2MultipartUploadLike[] = [];
    try {
      for (const file of manifest.files) {
        const dedup = await this.store.findDedup(scope, file.sha256, file.sizeBytes);
        if (dedup) {
          files.push({
            fileName: file.fileName,
            sha256: file.sha256,
            sizeBytes: file.sizeBytes,
            state: "deduplicated",
            objectKey: dedup.objectKey,
            dedupRef: dedup,
            parts: [],
          });
          continue;
        }
        const objectKey = `uploads/v1/${scope}/${manifest.month}/${manifest.manifestId}/${file.fileName}`;
        const encryption = this.cryptoBox.createObjectMetadata(scope, persistedEnvelope, file);
        const multipart = await this.bucket.createMultipartUpload(objectKey, {
          customMetadata: this.cryptoBox.toR2Metadata(encryption),
        });
        createdMultiparts.push(multipart);
        files.push({
          fileName: file.fileName,
          sha256: file.sha256,
          sizeBytes: file.sizeBytes,
          state: "upload_required",
          objectKey,
          createdObjectKey: objectKey,
          r2UploadId: multipart.uploadId,
          encryption,
          parts: [],
        });
      }

      const session: UploadSessionRecord = {
        uploadId,
        profileScope: scope,
        manifestId: manifest.manifestId,
        manifestSha256: digest,
        month: manifest.month,
        submissionKind: manifest.submissionKind,
        sourceGeneration: manifest.sourceGeneration,
        envelope: persistedEnvelope,
        files,
        state: "active",
        createdAt: now,
        expiresAt: now + this.sessionTtlSeconds,
      };
      await this.store.createSession(session);
      if (files.every((file) => file.state === "deduplicated")) {
        await this.store.markCompleted(scope, uploadId, now);
        session.state = "completed";
        session.completedAt = now;
        return statusFromSession(session, true);
      }
      return statusFromSession(session);
    } catch (error) {
      await Promise.all(createdMultiparts.map((multipart) => multipart.abort().catch(() => undefined)));
      throw error;
    }
  }

  async status(context: UploadContext, uploadId: string): Promise<UploadStatus> {
    const scope = await profileScope(context.profileId);
    const session = await this.store.getSession(scope, uploadId);
    if (!session) fail("session_not_found", 404);
    if (session.state === "active" && session.expiresAt <= Math.floor(this.nowProvider())) fail("session_expired", 410);
    return statusFromSession(session);
  }

  async uploadPart(
    context: UploadContext,
    uploadId: string,
    fileName: FiveDbFileName,
    partNumber: number,
    body: ReadableStream<Uint8Array> | Uint8Array,
    expectedPartSha256?: string,
  ): Promise<{ state: "uploaded" | "duplicate"; partNumber: number; sha256: string; sizeBytes: number }> {
    ensurePartNumber(partNumber);
    if (expectedPartSha256 !== undefined && !SHA256_PATTERN.test(expectedPartSha256)) {
      fail("invalid_file_digest");
    }
    const scope = await profileScope(context.profileId);
    const session = await this.store.getSession(scope, uploadId);
    if (!session) fail("session_not_found", 404);
    ensureActive(session, Math.floor(this.nowProvider()));
    const file = session.files.find((item) => item.fileName === fileName);
    if (!file) fail("invalid_file_name");
    if (file.state !== "upload_required" || !file.encryption || !file.r2UploadId) {
      if (file.state === "deduplicated" || file.state === "completed") fail("part_conflict", 409);
      fail("session_not_active", 409);
    }
    const existing = file.parts.find((part) => part.partNumber === partNumber);
    if (existing) {
      if (expectedPartSha256 && existing.sha256 !== expectedPartSha256) fail("part_conflict", 409);
      return { state: "duplicate", partNumber, sha256: existing.sha256, sizeBytes: existing.sizeBytes };
    }

    const multipart = this.bucket.resumeMultipartUpload(file.objectKey, file.r2UploadId);
    const prepared = await prepareEncryptedPart(
      asReadableStream(body),
      scope,
      file.encryption,
      partNumber,
      this.cryptoBox,
    );
    let uploaded: R2UploadedPartLike;
    let stats: { sha256: string; sizeBytes: number; sqliteHeader: boolean };
    try {
      uploaded = await multipart.uploadPart(partNumber, prepared.stream);
      stats = await prepared.stats;
      if (stats.sizeBytes < 1 || stats.sizeBytes > MAX_R2_PART_SIZE_BYTES) fail("part_too_large", 413);
      if (expectedPartSha256 && stats.sha256 !== expectedPartSha256) fail("part_checksum_mismatch", 422);
    } catch (error) {
      if (error instanceof UploadProtocolError) {
        await multipart.abort().catch(() => undefined);
        await this.store.markAborted(scope, uploadId, "aborted").catch(() => undefined);
      }
      if (error instanceof UploadProtocolError) throw error;
      throw new UploadProtocolError("upload_failed", 502);
    }
    const part: UploadPartRecord = {
      partNumber,
      etag: uploaded.etag,
      sha256: stats.sha256,
      sizeBytes: stats.sizeBytes,
    };
    const result = await this.store.recordPart(scope, uploadId, fileName, part);
    return { state: result === "existing" ? "duplicate" : "uploaded", partNumber, sha256: part.sha256, sizeBytes: part.sizeBytes };
  }

  async complete(context: UploadContext, uploadId: string): Promise<{ state: "completed"; unchanged: boolean; uploadId: string; manifestSha256: string; submissionKind: SubmissionKind }> {
    const scope = await profileScope(context.profileId);
    const session = await this.store.getSession(scope, uploadId);
    if (!session) fail("session_not_found", 404);
    if (session.state === "completed") {
      return { state: "completed", unchanged: session.files.every((file) => file.state === "deduplicated"), uploadId, manifestSha256: session.manifestSha256, submissionKind: session.submissionKind };
    }
    ensureActive(session, Math.floor(this.nowProvider()));
    try {
      for (const file of session.files) {
        if (file.state === "deduplicated" || file.state === "completed") continue;
        if (!file.encryption || !file.r2UploadId) fail("multipart_incomplete", 409);
        validatePartLayout(file, this.minimumPartSizeBytes);
        const multipart = this.bucket.resumeMultipartUpload(file.objectKey, file.r2UploadId);
        await multipart.complete(file.parts.map((part) => ({ partNumber: part.partNumber, etag: part.etag })));
        const object = await this.bucket.get(file.objectKey);
        if (!object) fail("object_integrity_failed", 422);
        await verifyEncryptedObject(object, file, scope, this.cryptoBox);
        const ref = await this.store.putDedup({
          profileScope: scope,
          sha256: file.sha256,
          sizeBytes: file.sizeBytes,
          objectKey: file.objectKey,
          keyVersion: file.encryption.keyVersion,
        });
        if (ref.objectKey !== file.objectKey) await this.bucket.delete(file.objectKey);
        await this.store.markFileCompleted(scope, uploadId, file.fileName, ref);
      }
      const refreshed = await this.store.getSession(scope, uploadId);
      if (!refreshed || refreshed.files.some((file) => file.state !== "deduplicated" && file.state !== "completed")) {
        fail("multipart_incomplete", 409);
      }
      const completedAt = Math.floor(this.nowProvider());
      await this.store.markCompleted(scope, uploadId, completedAt);
      return { state: "completed", unchanged: refreshed.files.every((file) => file.state === "deduplicated"), uploadId, manifestSha256: session.manifestSha256, submissionKind: session.submissionKind };
    } catch (error) {
      await this.cleanupSessionObjects(scope, session);
      await this.store.markAborted(scope, uploadId, "aborted").catch(() => undefined);
      if (error instanceof UploadProtocolError) throw error;
      throw new UploadProtocolError("upload_failed", 502);
    }
  }

  async abort(context: UploadContext, uploadId: string): Promise<{ state: "aborted"; uploadId: string }> {
    const scope = await profileScope(context.profileId);
    const session = await this.store.getSession(scope, uploadId);
    if (!session) fail("session_not_found", 404);
    if (session.state === "completed") return { state: "aborted", uploadId };
    await this.cleanupSessionObjects(scope, session);
    await this.store.markAborted(scope, uploadId, "aborted");
    return { state: "aborted", uploadId };
  }

  async cleanupExpired(now = Math.floor(this.nowProvider())): Promise<{ sessions: number; objects: number }> {
    const sessions = await this.store.listExpired(now);
    let objects = 0;
    for (const session of sessions) {
      objects += await this.cleanupSessionObjects(session.profileScope, session);
      await this.store.markAborted(session.profileScope, session.uploadId, "expired");
    }
    return { sessions: sessions.length, objects };
  }

  private async cleanupSessionObjects(
    scope: string,
    session: UploadSessionRecord,
  ): Promise<number> {
    const keys = new Set<string>();
    for (const file of session.files) {
      if (file.createdObjectKey) {
        const canonical = await this.store.findDedup(scope, file.sha256, file.sizeBytes);
        if (canonical?.objectKey !== file.createdObjectKey) keys.add(file.createdObjectKey);
      }
      if (file.state === "upload_required" && file.r2UploadId) {
        await this.bucket.resumeMultipartUpload(file.objectKey, file.r2UploadId).abort().catch(() => undefined);
      }
    }
    await Promise.all([...keys].map((key) => this.bucket.delete(key).catch(() => undefined)));
    return keys.size;
  }
}

export type UploadAuthenticator = (request: Request) => Promise<UploadContext | null>;
export type UploadCompletedHook = (
  context: UploadContext,
  result: { state: "completed"; unchanged: boolean; uploadId: string; manifestSha256: string; submissionKind: SubmissionKind },
) => Promise<Record<string, unknown>>;

function responseJson(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function errorResponse(error: unknown): Response {
  if (error instanceof UploadProtocolError) return responseJson({ error: { code: error.code } }, error.status);
  return responseJson({ error: { code: "internal_error" } }, 500);
}

/**
 * Route adapter. The caller supplies authentication/profile resolution so this
 * module never accepts account_id/profile_id from JSON as an authorization
 * decision. It is intentionally usable with a mock R2 bucket in local tests.
 */
export async function handleUploadRequest(
  request: Request,
  service: UploadService,
  authenticate: UploadAuthenticator,
  onCompleted?: UploadCompletedHook,
): Promise<Response | null> {
  const url = new URL(request.url);
  const startRoute = url.pathname === "/v1/uploads";
  const statusMatch = url.pathname.match(/^\/v1\/uploads\/([A-Za-z0-9_-]+)$/);
  const completeMatch = url.pathname.match(/^\/v1\/uploads\/([A-Za-z0-9_-]+)\/complete$/);
  const abortMatch = statusMatch;
  const partMatch = url.pathname.match(/^\/v1\/uploads\/([A-Za-z0-9_-]+)\/files\/([^/]+)\/parts\/(\d+)$/);
  if (!startRoute && !statusMatch && !completeMatch && !partMatch) return null;

  try {
    const context = await authenticate(request);
    if (!context) fail("unauthorized", 401);
    if (startRoute && request.method === "POST") {
      const manifest = await request.json();
      return responseJson(await service.start(context, manifest), 201);
    }
    if (statusMatch && request.method === "GET") {
      return responseJson(await service.status(context, statusMatch[1]));
    }
    if (completeMatch && request.method === "POST") {
      const completed = await service.complete(context, completeMatch[1]);
      const extension = onCompleted ? await onCompleted(context, completed) : {};
      return responseJson({ ...completed, ...extension });
    }
    if (abortMatch && request.method === "DELETE") {
      return responseJson(await service.abort(context, abortMatch[1]));
    }
    if (partMatch && (request.method === "PUT" || request.method === "POST")) {
      const decodedFileName = decodeURIComponent(partMatch[2]);
      if (!(FIVE_DB_FILE_NAMES as readonly string[]).includes(decodedFileName)) fail("invalid_file_name");
      const expected = request.headers.get("X-Part-SHA256") ?? undefined;
      const body = request.body;
      if (!body) fail("part_size_mismatch", 422);
      return responseJson(await service.uploadPart(
        context,
        partMatch[1],
        decodedFileName as FiveDbFileName,
        Number(partMatch[3]),
        body,
        expected,
      ), 200);
    }
    return responseJson({ error: { code: "method_not_allowed" } }, 405);
  } catch (error) {
    return errorResponse(error);
  }
}
