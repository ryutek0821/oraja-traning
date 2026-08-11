import {
  type FiveDbFileName,
  type ObjectEncryptionMetadata,
  type ProfileEnvelopeRecord,
  type StoredFileRef,
  type UploadFileSession,
  type UploadPartRecord,
  type UploadSessionRecord,
  type UploadSessionState,
  type UploadSessionStore,
  type SubmissionKind,
  UploadProtocolError,
} from "./upload-protocol";

type SessionRow = {
  upload_id: string;
  profile_scope: string;
  manifest_id: string;
  manifest_sha256: string;
  month: string;
  submission_kind: SubmissionKind;
  source_generation: string;
  envelope_key_version: string;
  envelope_wrap_nonce: string;
  envelope_wrapped_dek: string;
  state: UploadSessionState;
  created_at: number;
  expires_at: number;
  completed_at: number | null;
};

type FileRow = {
  file_name: FiveDbFileName;
  sha256: string;
  size_bytes: number;
  state: UploadFileSession["state"];
  object_key: string;
  created_object_key: string | null;
  r2_upload_id: string | null;
  encryption_json: string | null;
  dedup_json: string | null;
};

type PartRow = {
  file_name: FiveDbFileName;
  part_number: number;
  etag: string;
  sha256: string;
  size_bytes: number;
};

type EnvelopeRow = {
  key_version: string;
  wrap_nonce: string;
  wrapped_dek: string;
};

type DedupRow = {
  profile_scope: string;
  sha256: string;
  size_bytes: number;
  object_key: string;
  key_version: string;
};

function protocolError(code: ConstructorParameters<typeof UploadProtocolError>[0], status = 500): never {
  throw new UploadProtocolError(code, status);
}

function parseJson<T>(value: string | null, code: ConstructorParameters<typeof UploadProtocolError>[0]): T | undefined {
  if (value === null) return undefined;
  try {
    return JSON.parse(value) as T;
  } catch {
    protocolError(code);
  }
}

function changes(result: { meta?: { changes?: number } }): number {
  return result.meta?.changes ?? 0;
}

function manifestJson(session: UploadSessionRecord): string {
  return JSON.stringify({
    contract: "five-db-upload-manifest",
    schema_version: "1",
    manifest_id: session.manifestId,
    month: session.month,
    submission_kind: session.submissionKind,
    source_generation: session.sourceGeneration,
    game_mode: "SP7",
    trust_domain: "official",
    files: session.files.map((file) => ({
      file_name: file.fileName,
      sha256: file.sha256,
      size_bytes: file.sizeBytes,
    })),
    snapshot: { static_snapshot: true, wal_present: false, shm_present: false },
    privacy: { contains_bms: false, contains_replay: false, aggregate_eligible: false },
  });
}

export class D1UploadSessionStore implements UploadSessionStore {
  constructor(private readonly db: D1Database) {}

  async findSessionByManifest(profileScope: string, manifestSha256: string): Promise<UploadSessionRecord | null> {
    const row = await this.db
      .prepare(
        `SELECT upload_id, profile_scope, manifest_id, manifest_sha256, month,
                submission_kind, source_generation, envelope_key_version,
                envelope_wrap_nonce, envelope_wrapped_dek, state,
                created_at, expires_at, completed_at
           FROM upload_sessions
          WHERE profile_scope = ?1 AND manifest_sha256 = ?2
          ORDER BY created_at DESC
          LIMIT 1`,
      )
      .bind(profileScope, manifestSha256)
      .first<SessionRow>();
    return row ? this.hydrate(row) : null;
  }

  async getSession(profileScope: string, uploadId: string): Promise<UploadSessionRecord | null> {
    const row = await this.db
      .prepare(
        `SELECT upload_id, profile_scope, manifest_id, manifest_sha256, month,
                submission_kind, source_generation, envelope_key_version,
                envelope_wrap_nonce, envelope_wrapped_dek, state,
                created_at, expires_at, completed_at
           FROM upload_sessions
          WHERE profile_scope = ?1 AND upload_id = ?2`,
      )
      .bind(profileScope, uploadId)
      .first<SessionRow>();
    return row ? this.hydrate(row) : null;
  }

  async createSession(session: UploadSessionRecord): Promise<void> {
    const statements: D1PreparedStatement[] = [
      this.db
        .prepare(
          `INSERT INTO upload_sessions(
             upload_id, profile_scope, manifest_id, manifest_sha256, month,
             submission_kind, source_generation, envelope_key_version,
             envelope_wrap_nonce, envelope_wrapped_dek, manifest_json,
             state, created_at, expires_at, completed_at
           ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)`,
        )
        .bind(
          session.uploadId,
          session.profileScope,
          session.manifestId,
          session.manifestSha256,
          session.month,
          session.submissionKind,
          session.sourceGeneration,
          session.envelope.keyVersion,
          session.envelope.wrapNonce,
          session.envelope.wrappedDek,
          manifestJson(session),
          session.state,
          session.createdAt,
          session.expiresAt,
          session.completedAt ?? null,
        ),
    ];
    for (const file of session.files) {
      statements.push(
        this.db
          .prepare(
            `INSERT INTO upload_files(
               upload_id, file_name, sha256, size_bytes, state, object_key,
               created_object_key, r2_upload_id, encryption_json, dedup_json
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)`,
          )
          .bind(
            session.uploadId,
            file.fileName,
            file.sha256,
            file.sizeBytes,
            file.state,
            file.objectKey,
            file.createdObjectKey ?? null,
            file.r2UploadId ?? null,
            file.encryption ? JSON.stringify(file.encryption) : null,
            file.dedupRef ? JSON.stringify(file.dedupRef) : null,
          ),
      );
    }
    await this.db.batch(statements);
  }

  async markFileCompleted(
    profileScope: string,
    uploadId: string,
    fileName: FiveDbFileName,
    ref: StoredFileRef,
  ): Promise<void> {
    const result = await this.db
      .prepare(
        `UPDATE upload_files
            SET state = 'completed', object_key = ?1, dedup_json = ?2
          WHERE upload_id = ?3 AND file_name = ?4
            AND EXISTS (
              SELECT 1 FROM upload_sessions
               WHERE upload_id = ?3 AND profile_scope = ?5
            )`,
      )
      .bind(ref.objectKey, JSON.stringify(ref), uploadId, fileName, profileScope)
      .run();
    if (changes(result) !== 1) protocolError("session_not_found", 404);
  }

  async recordPart(
    profileScope: string,
    uploadId: string,
    fileName: FiveDbFileName,
    part: UploadPartRecord,
  ): Promise<"inserted" | "existing"> {
    const insert = await this.db
      .prepare(
        `INSERT OR IGNORE INTO upload_parts(
           upload_id, file_name, part_number, etag, sha256, size_bytes
         )
         SELECT ?1, ?2, ?3, ?4, ?5, ?6
          WHERE EXISTS (
            SELECT 1 FROM upload_sessions
             WHERE upload_id = ?1 AND profile_scope = ?7
          )`,
      )
      .bind(uploadId, fileName, part.partNumber, part.etag, part.sha256, part.sizeBytes, profileScope)
      .run();
    if (changes(insert) === 1) return "inserted";
    const existing = await this.db
      .prepare(
        `SELECT part_number, etag, sha256, size_bytes
           FROM upload_parts
          WHERE upload_id = ?1 AND file_name = ?2 AND part_number = ?3`,
      )
      .bind(uploadId, fileName, part.partNumber)
      .first<PartRow>();
    if (!existing) protocolError("session_not_found", 404);
    if (existing.sha256 !== part.sha256 || existing.size_bytes !== part.sizeBytes) {
      protocolError("part_conflict", 409);
    }
    return "existing";
  }

  async markCompleted(profileScope: string, uploadId: string, completedAt: number): Promise<void> {
    const result = await this.db
      .prepare(
        `UPDATE upload_sessions
            SET state = 'completed', completed_at = ?1
          WHERE profile_scope = ?2 AND upload_id = ?3 AND state = 'active'`,
      )
      .bind(completedAt, profileScope, uploadId)
      .run();
    if (changes(result) !== 1) protocolError("session_not_found", 404);
  }

  async markAborted(profileScope: string, uploadId: string, state: "aborted" | "expired" = "aborted"): Promise<void> {
    const result = await this.db
      .prepare(
        `UPDATE upload_sessions
            SET state = ?1
          WHERE profile_scope = ?2 AND upload_id = ?3 AND state = 'active'`,
      )
      .bind(state, profileScope, uploadId)
      .run();
    if (changes(result) !== 1) protocolError("session_not_found", 404);
  }

  async getProfileEnvelope(profileScope: string): Promise<ProfileEnvelopeRecord | null> {
    const row = await this.db
      .prepare(
        `SELECT key_version, wrap_nonce, wrapped_dek
           FROM upload_profile_envelopes
          WHERE profile_scope = ?1`,
      )
      .bind(profileScope)
      .first<EnvelopeRow>();
    return row ? { keyVersion: row.key_version, wrapNonce: row.wrap_nonce, wrappedDek: row.wrapped_dek } : null;
  }

  async putProfileEnvelope(profileScope: string, envelope: ProfileEnvelopeRecord): Promise<ProfileEnvelopeRecord> {
    await this.db
      .prepare(
        `INSERT OR IGNORE INTO upload_profile_envelopes(
           profile_scope, key_version, wrap_nonce, wrapped_dek, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5)`,
      )
      .bind(profileScope, envelope.keyVersion, envelope.wrapNonce, envelope.wrappedDek, Math.floor(Date.now() / 1000))
      .run();
    return (await this.getProfileEnvelope(profileScope)) ?? protocolError("internal_error");
  }

  async findDedup(profileScope: string, sha256: string, sizeBytes: number): Promise<StoredFileRef | null> {
    const row = await this.db
      .prepare(
        `SELECT profile_scope, sha256, size_bytes, object_key, key_version
           FROM upload_dedup
          WHERE profile_scope = ?1 AND sha256 = ?2 AND size_bytes = ?3`,
      )
      .bind(profileScope, sha256, sizeBytes)
      .first<DedupRow>();
    return row ? this.dedupFromRow(row) : null;
  }

  async putDedup(ref: StoredFileRef): Promise<StoredFileRef> {
    await this.db
      .prepare(
        `INSERT OR IGNORE INTO upload_dedup(
           profile_scope, sha256, size_bytes, object_key, key_version, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)`,
      )
      .bind(ref.profileScope, ref.sha256, ref.sizeBytes, ref.objectKey, ref.keyVersion, Math.floor(Date.now() / 1000))
      .run();
    return (await this.findDedup(ref.profileScope, ref.sha256, ref.sizeBytes)) ?? protocolError("internal_error");
  }

  async listExpired(now: number): Promise<UploadSessionRecord[]> {
    const result = await this.db
      .prepare(
        `SELECT upload_id, profile_scope, manifest_id, manifest_sha256, month,
                submission_kind, source_generation, envelope_key_version,
                envelope_wrap_nonce, envelope_wrapped_dek, state,
                created_at, expires_at, completed_at
           FROM upload_sessions
          WHERE state = 'active' AND expires_at <= ?1`,
      )
      .bind(now)
      .all<SessionRow>();
    const sessions: UploadSessionRecord[] = [];
    for (const row of result.results) sessions.push(await this.hydrate(row));
    return sessions;
  }

  private async hydrate(row: SessionRow): Promise<UploadSessionRecord> {
    const filesResult = await this.db
      .prepare(
        `SELECT file_name, sha256, size_bytes, state, object_key,
                created_object_key, r2_upload_id, encryption_json, dedup_json
           FROM upload_files
          WHERE upload_id = ?1
          ORDER BY file_name`,
      )
      .bind(row.upload_id)
      .all<FileRow>();
    const partsResult = await this.db
      .prepare(
        `SELECT file_name, part_number, etag, sha256, size_bytes
           FROM upload_parts
          WHERE upload_id = ?1
          ORDER BY file_name, part_number`,
      )
      .bind(row.upload_id)
      .all<PartRow>();
    const partsByFile = new Map<FiveDbFileName, UploadPartRecord[]>();
    for (const part of partsResult.results) {
      const list = partsByFile.get(part.file_name) ?? [];
      list.push({
        partNumber: part.part_number,
        etag: part.etag,
        sha256: part.sha256,
        sizeBytes: part.size_bytes,
      });
      partsByFile.set(part.file_name, list);
    }
    const files: UploadFileSession[] = filesResult.results.map((file) => ({
      fileName: file.file_name,
      sha256: file.sha256,
      sizeBytes: file.size_bytes,
      state: file.state,
      objectKey: file.object_key,
      createdObjectKey: file.created_object_key ?? undefined,
      r2UploadId: file.r2_upload_id ?? undefined,
      encryption: parseJson<ObjectEncryptionMetadata>(file.encryption_json, "encryption_metadata_invalid"),
      dedupRef: parseJson<StoredFileRef>(file.dedup_json, "internal_error"),
      parts: partsByFile.get(file.file_name) ?? [],
    }));
    return {
      uploadId: row.upload_id,
      profileScope: row.profile_scope,
      manifestId: row.manifest_id,
      manifestSha256: row.manifest_sha256,
      month: row.month,
      submissionKind: row.submission_kind,
      sourceGeneration: row.source_generation,
      envelope: {
        keyVersion: row.envelope_key_version,
        wrapNonce: row.envelope_wrap_nonce,
        wrappedDek: row.envelope_wrapped_dek,
      },
      files,
      state: row.state,
      createdAt: row.created_at,
      expiresAt: row.expires_at,
      completedAt: row.completed_at ?? undefined,
    };
  }

  private dedupFromRow(row: DedupRow): StoredFileRef {
    return {
      profileScope: row.profile_scope,
      sha256: row.sha256,
      sizeBytes: row.size_bytes,
      objectKey: row.object_key,
      keyVersion: row.key_version,
    };
  }
}
