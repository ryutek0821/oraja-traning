import { canonicalJson, regenerationInputDigest } from "./workflow-state";
import { sha256Hex, type JobEnvelope } from "./job-ledger";
import { D1UploadSessionStore } from "./upload-store";
import {
  EnvelopeCrypto,
  FIVE_DB_FILE_NAMES,
  decryptCompletedUploadObject,
  profileScope,
  type UploadFileSession,
} from "./upload-protocol";

export type ContainerBridgeEnv = {
  CONTROL_DB: D1Database;
  PROFILE_DO: DurableObjectNamespace;
  RAW_BUCKET: R2Bucket;
  ARTIFACT_BUCKET: R2Bucket;
  PYTHON_PROCESSOR: DurableObjectNamespace;
  ENVELOPE_MASTER_KEY?: string;
};

export type ContainerBridgeResult = {
  outputManifestSha256: string;
  artifactKey: string;
  tableArtifacts: TableArtifact[];
};

export type TableArtifact = {
  kind: "recommend_header" | "recommend_score" | "daily_menu_header" | "daily_menu_score";
  objectKey: string;
  sha256: string;
  sizeBytes: number;
};

type ContainerArtifact = {
  kind: string;
  object_key: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
  data_base64: string;
};

type ContainerResponse = {
  status: string;
  output_manifest: Record<string, unknown>;
  output_manifest_sha256: string;
  artifacts: ContainerArtifact[];
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_BRIDGE_BYTES = 5 * 1024 * 1024 * 1024;
const MAX_INLINE_ARTIFACT_BYTES = 64 * 1024 * 1024;
const MAX_CONTAINER_RESPONSE_BYTES = 96 * 1024 * 1024;
const MAGIC = new TextEncoder().encode("ORAJA5DB2\n");
const encoder = new TextEncoder();
const MAX_TABLE_CONTEXT_BYTES = 16 * 1024 * 1024;
const MAX_PLAY_EVENT_BYTES = 16 * 1024;

export class ContainerBridgeError extends Error {
  constructor(
    public readonly code: string,
    public readonly retryable: boolean,
  ) {
    super(code);
    this.name = "ContainerBridgeError";
  }
}

function requestedAt(jobId: string): string {
  if (!UUID.test(jobId)) throw new ContainerBridgeError("invalid_job_identity", false);
  const millis = Number.parseInt(jobId.replace(/-/g, "").slice(0, 12), 16);
  return new Date(millis).toISOString().replace(".000Z", "Z");
}

function encodeBase64Url(value: string): string {
  const bytes = encoder.encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw new ContainerBridgeError("artifact_payload_invalid", false);
  try {
    return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
  } catch {
    throw new ContainerBridgeError("artifact_payload_invalid", false);
  }
}

async function sha256Bytes(value: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", value.slice().buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function boundedContainerResponse(response: Response): Promise<ContainerResponse> {
  if (!response.body) throw new ContainerBridgeError("container_response_invalid", true);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      total += item.value.byteLength;
      if (total > MAX_CONTAINER_RESPONSE_BYTES) {
        await reader.cancel("bounded response exceeded");
        throw new ContainerBridgeError("container_response_too_large", false);
      }
      chunks.push(item.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder().decode(bytes)) as ContainerResponse;
  } catch {
    throw new ContainerBridgeError("container_response_invalid", false);
  }
}

function fileHeader(file: UploadFileSession): Uint8Array {
  return encoder.encode(canonicalJson({
    file_name: file.fileName,
    sha256: file.sha256,
    size_bytes: file.sizeBytes,
  }));
}

function contextHeader(content: Uint8Array, digest: string): Uint8Array {
  return encoder.encode(canonicalJson({
    file_name: "table-context.json",
    sha256: digest,
    size_bytes: content.byteLength,
  }));
}

function uint32(value: number): Uint8Array {
  const bytes = new Uint8Array(4);
  new DataView(bytes.buffer).setUint32(0, value);
  return bytes;
}

function transportLength(files: readonly UploadFileSession[], context: Uint8Array, contextDigest: string): number {
  const header = contextHeader(context, contextDigest);
  return files.reduce((total, file) => total + 4 + fileHeader(file).byteLength + file.sizeBytes, MAGIC.byteLength)
    + 4 + header.byteLength + context.byteLength;
}

function framedStream(
  files: readonly UploadFileSession[],
  objects: readonly R2Object[],
  scope: string,
  cryptoBox: EnvelopeCrypto,
  context: Uint8Array,
  contextDigest: string,
): ReadableStream<Uint8Array> {
  async function* frames(): AsyncGenerator<Uint8Array> {
    yield MAGIC;
    for (let index = 0; index < files.length; index += 1) {
      const header = fileHeader(files[index]);
      yield uint32(header.byteLength);
      yield header;
      const stream = decryptCompletedUploadObject(objects[index], files[index], scope, cryptoBox);
      const reader = stream.getReader();
      try {
        while (true) {
          const item = await reader.read();
          if (item.done) break;
          yield item.value;
        }
      } finally {
        reader.releaseLock();
      }
    }
    const header = contextHeader(context, contextDigest);
    yield uint32(header.byteLength);
    yield header;
    yield context;
  }
  const iterator = frames();
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

async function loadTableContext(job: JobEnvelope, db: D1Database): Promise<{ bytes: Uint8Array; settingsRevision: number }> {
  const profile = await db.prepare(
    `SELECT p.display_name, p.timezone,
            COALESCE(s.target_judged, 100000) AS target_judged,
            COALESCE(s.reserve_judged, 10000) AS reserve_judged,
            COALESCE(s.readiness, 'normal') AS readiness,
            COALESCE(s.settings_revision, 1) AS settings_revision
       FROM profiles p
       LEFT JOIN profile_settings s
         ON s.account_id = p.account_id AND s.profile_id = p.id
      WHERE p.account_id = ?1 AND p.id = ?2 AND p.status = 'active'`,
  ).bind(job.accountId, job.profileId).first<Record<string, unknown>>();
  if (!profile) throw new ContainerBridgeError("table_input_unavailable", false);
  const result = await db.prepare(
    `SELECT s.public_id AS table_id, e.level, e.sha256, e.md5, e.title
       FROM chart_catalog_entries e
       JOIN chart_catalog_versions v ON v.id = e.catalog_version_id
       JOIN chart_catalog_sources s ON s.id = v.source_id
      WHERE v.status = 'active' AND s.retired_at IS NULL
        AND e.level IS NOT NULL AND e.song_mode = 7
      ORDER BY s.public_id, e.level, e.sha256
      LIMIT 20001`,
  ).all<Record<string, unknown>>();
  if (result.results.length < 1 || result.results.length > 20_000) {
    throw new ContainerBridgeError("table_input_unavailable", false);
  }
  const entries = result.results.map((row) => ({
    table_id: row.table_id,
    level: row.level,
    sha256: row.sha256,
    md5: row.md5,
    title: row.title,
  }));
  const catalogManifestSha256 = await sha256Hex(canonicalJson(entries));
  const context = encoder.encode(canonicalJson({
    profile_id: job.profileId,
    display_name: profile.display_name,
    settings_revision: profile.settings_revision,
    settings: {
      timezone: profile.timezone,
      target_judged: profile.target_judged,
      reserve_judged: profile.reserve_judged,
      readiness: profile.readiness,
    },
    catalog_manifest_sha256: catalogManifestSha256,
    catalog_entries: entries,
  }));
  if (context.byteLength > MAX_TABLE_CONTEXT_BYTES) {
    throw new ContainerBridgeError("table_input_too_large", false);
  }
  const settingsRevision = Number(profile.settings_revision);
  if (!Number.isSafeInteger(settingsRevision) || settingsRevision < 1) {
    throw new ContainerBridgeError("table_input_unavailable", false);
  }
  return { bytes: context, settingsRevision };
}

function parseInputPointer(value: string | null): string {
  const match = /^upload-session:([0-9a-f-]{36})$/i.exec(value ?? "");
  if (!match || !UUID.test(match[1])) throw new ContainerBridgeError("input_manifest_pointer_missing", false);
  return match[1].toLowerCase();
}

async function resolveUploadId(job: JobEnvelope, db: D1Database, scope: string): Promise<string> {
  const direct = /^upload-session:([0-9a-f-]{36})$/i.exec(job.inputKey ?? "");
  if (direct && UUID.test(direct[1])) return direct[1].toLowerCase();
  const deferred = job.jobKind === "monthly" || job.jobKind === "regenerate"
    || (job.jobKind === "play" && job.inputKey === `play-event:${job.eventId}`);
  if (!deferred) throw new ContainerBridgeError("input_manifest_pointer_missing", false);
  const latest = await db.prepare(
    `SELECT upload_id FROM upload_sessions
      WHERE profile_scope = ?1 AND state = 'completed'
      ORDER BY completed_at DESC, created_at DESC LIMIT 1`,
  ).bind(scope).first<{ upload_id: string }>();
  if (!latest || !UUID.test(latest.upload_id)) {
    throw new ContainerBridgeError("input_manifest_not_found", true);
  }
  return latest.upload_id.toLowerCase();
}

async function loadPlayEvent(job: JobEnvelope, env: ContainerBridgeEnv): Promise<string | null> {
  if (job.jobKind !== "play") return null;
  if (job.inputKey !== `play-event:${job.eventId}`) {
    throw new ContainerBridgeError("play_event_pointer_invalid", false);
  }
  const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(job.profileId));
  const response = await stub.fetch(
    `https://profile.internal/internal/play-events/${encodeURIComponent(job.eventId)}`,
  );
  if (!response.ok) throw new ContainerBridgeError("play_event_unavailable", response.status >= 500);
  const value = await response.json() as Record<string, unknown>;
  if (value.contract !== "container-play-event" || value.schema_version !== 1
    || value.event_id !== job.eventId || value.profile_id !== job.profileId
    || value.payload_digest !== job.inputDigest || typeof value.row !== "object" || value.row === null) {
    throw new ContainerBridgeError("play_event_contract_invalid", false);
  }
  const encoded = canonicalJson(value);
  if (encoder.encode(encoded).byteLength > MAX_PLAY_EVENT_BYTES) {
    throw new ContainerBridgeError("play_event_too_large", false);
  }
  return encodeBase64Url(encoded);
}

async function immutablePut(bucket: R2Bucket, key: string, content: Uint8Array, digest: string, contentType: string): Promise<void> {
  const existing = await bucket.head(key);
  if (existing) {
    if (existing.customMetadata?.sha256 !== digest || existing.size !== content.byteLength) {
      throw new ContainerBridgeError("artifact_immutable_conflict", false);
    }
    return;
  }
  await bucket.put(key, content, { httpMetadata: { contentType }, customMetadata: { sha256: digest } });
  const stored = await bucket.head(key);
  if (!stored || stored.customMetadata?.sha256 !== digest || stored.size !== content.byteLength) {
    throw new ContainerBridgeError("artifact_store_failed", true);
  }
}

function validateOutputIdentity(job: JobEnvelope, response: ContainerResponse): void {
  const manifest = response.output_manifest;
  if (response.status !== "succeeded"
    || manifest.contract !== "container-output-manifest"
    || manifest.schema_version !== "1"
    || manifest.job_id !== job.jobId
    || manifest.account_id !== job.accountId
    || manifest.profile_id !== job.profileId
    || manifest.idempotency_key !== job.idempotencyKey
    || manifest.trust_domain !== job.trustDomain
    || manifest.output_revision !== job.revision
    || !SHA256.test(response.output_manifest_sha256)) {
    throw new ContainerBridgeError("container_output_contract_invalid", false);
  }
}

export async function processContainerJob(job: JobEnvelope, env: ContainerBridgeEnv): Promise<ContainerBridgeResult> {
  if (!env.ENVELOPE_MASTER_KEY) throw new ContainerBridgeError("envelope_key_unavailable", true);
  if (job.trustDomain !== "official") throw new ContainerBridgeError("unsupported_trust_domain", false);
  const scope = await profileScope(job.profileId);
  const uploadId = await resolveUploadId(job, env.CONTROL_DB, scope);
  const session = await new D1UploadSessionStore(env.CONTROL_DB).getCompletedSession(scope, uploadId);
  if (!session) throw new ContainerBridgeError("input_manifest_not_found", true);
  const tableContext = await loadTableContext(job, env.CONTROL_DB);
  const expectedInputDigest = job.jobKind === "regenerate" && job.eventId.startsWith("settings:")
    ? await regenerationInputDigest(job.profileId, session.manifestSha256, tableContext.settingsRevision)
    : job.jobKind === "initial" ? session.manifestSha256 : job.inputDigest;
  if (expectedInputDigest !== job.inputDigest) throw new ContainerBridgeError("input_digest_mismatch", false);
  const playEvent = await loadPlayEvent(job, env);
  const byName = new Map(session.files.map((file) => [file.fileName, file]));
  const files = FIVE_DB_FILE_NAMES.map((name) => byName.get(name));
  if (files.some((file) => !file)) throw new ContainerBridgeError("input_file_set_invalid", false);
  const ordered = files as UploadFileSession[];
  const totalPlaintext = ordered.reduce((total, file) => total + file.sizeBytes, 0);
  if (!Number.isSafeInteger(totalPlaintext) || totalPlaintext > MAX_BRIDGE_BYTES) {
    throw new ContainerBridgeError("input_bundle_too_large", false);
  }
  const objects: R2Object[] = [];
  for (const file of ordered) {
    const object = await env.RAW_BUCKET.get(file.objectKey);
    if (!object) throw new ContainerBridgeError("input_object_not_found", true);
    objects.push(object);
  }
  const inputManifest = {
    contract: "container-input-manifest",
    schema_version: "1",
    job_id: job.jobId,
    idempotency_key: job.idempotencyKey,
    account_id: job.accountId,
    profile_id: job.profileId,
    trust_domain: job.trustDomain,
    source_manifest_sha256: session.manifestSha256,
    input_bundle: {
      object_key: `profiles/${job.profileId}/uploads/${uploadId}/five-db.enc`,
      sha256: session.manifestSha256,
      size_bytes: totalPlaintext,
      encryption: "envelope-v1",
      key_ref: `profile-key:${job.profileId}`,
    },
    requested_at: requestedAt(job.jobId),
    game_mode: "SP7",
    policy: {
      eligibility_status: "eligible",
      eligibility_reason_code: null,
      eligibility_policy_version: "2026-07-01",
      include_raw_db_in_model: false,
    },
  };
  const cryptoBox = EnvelopeCrypto.fromSecret(env.ENVELOPE_MASTER_KEY);
  const tableContextDigest = await sha256Bytes(tableContext.bytes);
  const body = framedStream(ordered, objects, scope, cryptoBox, tableContext.bytes, tableContextDigest);
  const length = transportLength(ordered, tableContext.bytes, tableContextDigest);
  const stub = env.PYTHON_PROCESSOR.get(env.PYTHON_PROCESSOR.idFromName(job.jobId));
  const headers: Record<string, string> = {
    "content-type": "application/octet-stream",
    "content-length": String(length),
    "x-container-input-manifest": encodeBase64Url(canonicalJson(inputManifest)),
    "x-container-output-revision": String(job.revision),
    "x-container-transport": "five-db-framed-v2",
    "x-container-job-kind": job.jobKind,
  };
  if (playEvent) headers["x-container-play-event"] = playEvent;
  const response = await stub.fetch("http://python-processor/v1/jobs", {
    method: "POST",
    headers,
    body,
  });
  if (!response.ok) {
    const retryable = response.status >= 500 || response.status === 429;
    throw new ContainerBridgeError("container_processing_failed", retryable);
  }
  const declared = Number(response.headers.get("content-length") ?? "0");
  if (declared > MAX_CONTAINER_RESPONSE_BYTES) throw new ContainerBridgeError("container_response_too_large", false);
  const payload = await boundedContainerResponse(response);
  validateOutputIdentity(job, payload);
  const calculatedManifest = await sha256Hex(canonicalJson(payload.output_manifest));
  if (calculatedManifest !== payload.output_manifest_sha256) {
    throw new ContainerBridgeError("container_manifest_digest_mismatch", false);
  }
  if (!Array.isArray(payload.artifacts) || payload.artifacts.length < 1) {
    throw new ContainerBridgeError("container_artifact_missing", false);
  }
  const manifestArtifacts = payload.output_manifest.artifacts;
  if (!Array.isArray(manifestArtifacts) || manifestArtifacts.length !== payload.artifacts.length) {
    throw new ContainerBridgeError("container_artifact_contract_invalid", false);
  }
  let inlineBytes = 0;
  const tableArtifacts: TableArtifact[] = [];
  const prefix = `profiles/${job.profileId}/jobs/${job.jobId}/revisions/${job.revision}/`;
  for (const artifact of payload.artifacts) {
    if (!artifact.object_key.startsWith(prefix) || !SHA256.test(artifact.sha256)) {
      throw new ContainerBridgeError("container_artifact_partition_invalid", false);
    }
    const declaredArtifact = manifestArtifacts.find((candidate) => (
      candidate && typeof candidate === "object"
      && (candidate as Record<string, unknown>).object_key === artifact.object_key
      && (candidate as Record<string, unknown>).sha256 === artifact.sha256
    ));
    if (!declaredArtifact) throw new ContainerBridgeError("container_artifact_contract_invalid", false);
    const content = decodeBase64(artifact.data_base64);
    inlineBytes += content.byteLength;
    if (inlineBytes > MAX_INLINE_ARTIFACT_BYTES
      || content.byteLength !== artifact.size_bytes
      || await sha256Bytes(content) !== artifact.sha256) {
      throw new ContainerBridgeError("container_artifact_digest_mismatch", false);
    }
    await immutablePut(env.ARTIFACT_BUCKET, artifact.object_key, content, artifact.sha256, artifact.content_type);
    if (["recommend_header", "recommend_score", "daily_menu_header", "daily_menu_score"].includes(artifact.kind)) {
      tableArtifacts.push({
        kind: artifact.kind as TableArtifact["kind"],
        objectKey: artifact.object_key,
        sha256: artifact.sha256,
        sizeBytes: artifact.size_bytes,
      });
    }
  }
  if (tableArtifacts.length !== 4 || new Set(tableArtifacts.map((artifact) => artifact.kind)).size !== 4) {
    throw new ContainerBridgeError("table_artifact_missing", false);
  }
  const artifactKey = `${prefix}manifest.json`;
  const manifestBytes = encoder.encode(canonicalJson(payload.output_manifest));
  await immutablePut(env.ARTIFACT_BUCKET, artifactKey, manifestBytes, payload.output_manifest_sha256, "application/json");
  return { outputManifestSha256: payload.output_manifest_sha256, artifactKey, tableArtifacts };
}
