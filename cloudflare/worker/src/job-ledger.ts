import {
  canonicalJson,
  isJobKind,
  makeIdempotencyKey,
  makeScheduleEventId,
  queueRetryDelaySeconds,
  type JobKind,
  type ScheduleName,
  type TrustDomain,
} from "./workflow-state";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type OutcomeStatus = "succeeded" | "failed" | "cancelled";

export type JobEnvelope = {
  jobId: string;
  accountId: string;
  profileId: string;
  jobKind: JobKind;
  eventId: string;
  requestId: string | null;
  correlationId: string;
  contractVersion: string;
  trustDomain: TrustDomain;
  idempotencyKey: string;
  inputDigest: string;
  revision: number;
  workflowRun: number;
  maxAttempts: number;
  inputKey: string | null;
  artifactKey: string | null;
};

export type AcceptJobInput = {
  accountId: string;
  profileId: string;
  jobKind: JobKind;
  eventId: string;
  requestId?: string | null;
  correlationId?: string | null;
  contractVersion?: string;
  trustDomain?: TrustDomain;
  inputDigest: string;
  inputKey?: string | null;
  artifactKey?: string | null;
  maxAttempts?: number;
};

export type AcceptedJob = {
  status: "accepted" | "duplicate";
  job: JobEnvelope;
};

export type JobStepView = {
  workflowRun: number;
  stepName: string;
  attempt: number;
  status: string;
  retryable: boolean;
  startedAt: number;
  finishedAt: number | null;
  errorCode: string | null;
};

export type JobOutcomeView = {
  workflowRun: number;
  status: OutcomeStatus;
  outputDigest: string | null;
  manifestSha256: string | null;
  errorCode: string | null;
  recordedAt: number;
};

export type LatestPointerView = {
  revision: number;
  revisionId: string;
  manifestSha256: string;
  artifactKey: string | null;
  updatedAt: number;
};

export type JobStatusView = JobEnvelope & {
  status: JobStatus;
  createdAt: number;
  updatedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
  cancelRequestedAt: number | null;
  terminalReason: string | null;
  nextAttemptAt: number | null;
  steps: JobStepView[];
  outcomes: JobOutcomeView[];
  latest: LatestPointerView | null;
};

type JobRow = {
  id: string;
  account_id: string;
  profile_id: string;
  job_kind: string;
  event_id: string | null;
  request_id: string | null;
  contract_version: string;
  trust_domain: TrustDomain;
  correlation_id: string | null;
  idempotency_key: string;
  input_hash: string;
  status: JobStatus;
  revision: number;
  max_attempts: number;
  workflow_run: number;
  input_key: string | null;
  artifact_key: string | null;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  finished_at: number | null;
  cancel_requested_at: number | null;
  terminal_reason: string | null;
  next_attempt_at: number | null;
};

type CounterRow = { next_revision: number };
type ChangeResult = { meta?: { changes?: number } };
type StepRow = {
  workflow_run: number;
  step_name: string;
  attempt: number;
  status: string;
  retryable: number;
  started_at: number;
  finished_at: number | null;
  error_code: string | null;
};
type OutcomeRow = {
  workflow_run: number;
  status: OutcomeStatus;
  output_digest: string | null;
  manifest_sha256: string | null;
  error_code: string | null;
  recorded_at: number;
};
type PointerRow = {
  revision: number;
  revision_id: string;
  manifest_sha256: string;
  artifact_key: string | null;
  updated_at: number;
};
type ScheduleProfile = {
  account_id: string;
  profile_id: string;
  profile_status: string;
  account_status: string;
};

export class JobLedgerError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 500,
    public readonly retryable = false,
  ) {
    super(code);
    this.name = "JobLedgerError";
  }
}

export class IdempotencyConflictError extends JobLedgerError {
  constructor() {
    super("idempotency_conflict", 409, false);
    this.name = "IdempotencyConflictError";
  }
}

export function canonicalDigestPreimage(input: {
  contractVersion: string;
  inputDigest: string;
  jobKind: JobKind;
  profileId: string;
  trustDomain: TrustDomain;
}): string {
  return canonicalJson({
    contract_versions: [input.contractVersion],
    input_manifest_sha256: input.inputDigest,
    job_type: input.jobKind,
    profile_id: input.profileId,
    trust_domain: input.trustDomain,
  });
}

export async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function changes(result: ChangeResult): number {
  return result.meta?.changes ?? 0;
}

function numberValue(value: number | string | null | undefined, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function errorCode(error: unknown): string {
  if (error instanceof JobLedgerError) return error.code;
  if (error && typeof error === "object" && "code" in error) {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code.length <= 96) return code;
  }
  return "workflow_error";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isSha256(value: string): boolean {
  return /^[0-9a-f]{64}$/.test(value);
}

function validateInput(input: AcceptJobInput): void {
  if (!input.accountId || !input.profileId || !input.eventId) {
    throw new JobLedgerError("invalid_job_identity", 400, false);
  }
  if (!isJobKind(input.jobKind)) throw new JobLedgerError("invalid_job_kind", 400, false);
  if (!isSha256(input.inputDigest)) throw new JobLedgerError("invalid_input_digest", 400, false);
  if (input.trustDomain !== undefined && !["official", "self_hosted"].includes(input.trustDomain)) {
    throw new JobLedgerError("invalid_trust_domain", 400, false);
  }
  if (input.eventId.length > 256) throw new JobLedgerError("invalid_event_id", 400, false);
  if (input.maxAttempts !== undefined && (!Number.isInteger(input.maxAttempts) || input.maxAttempts < 1 || input.maxAttempts > 10)) {
    throw new JobLedgerError("invalid_max_attempts", 400, false);
  }
}

function toEnvelope(row: JobRow): JobEnvelope {
  if (!isJobKind(row.job_kind)) throw new JobLedgerError("invalid_job_kind_in_ledger", 500, false);
  return {
    jobId: row.id,
    accountId: row.account_id,
    profileId: row.profile_id,
    jobKind: row.job_kind,
    eventId: row.event_id ?? row.id,
    requestId: row.request_id,
    correlationId: row.correlation_id ?? row.id,
    contractVersion: row.contract_version,
    trustDomain: row.trust_domain,
    idempotencyKey: row.idempotency_key,
    inputDigest: row.input_hash,
    revision: numberValue(row.revision),
    workflowRun: numberValue(row.workflow_run),
    maxAttempts: numberValue(row.max_attempts, 4),
    inputKey: row.input_key,
    artifactKey: row.artifact_key,
  };
}

function auditStatement(
  db: D1Database,
  id: string,
  accountId: string,
  profileId: string,
  eventType: string,
  status: string,
  occurredAt: number,
  inputHash?: string | null,
  reasonCode?: string | null,
): D1PreparedStatement {
  return db.prepare(
    `INSERT INTO audit_events(
       id, account_id, profile_id, actor_kind, event_type, reason_code,
       request_id, resource_hash, input_hash, status, occurred_at
     ) VALUES (?1, ?2, ?3, 'service', ?4, ?5, NULL, NULL, ?6, ?7, ?8)`,
  ).bind(id, accountId, profileId, eventType, reasonCode ?? null, inputHash ?? null, status, occurredAt);
}

export class D1JobLedger {
  constructor(
    private readonly db: D1Database,
    private readonly clock: () => number = nowSeconds,
    private readonly idFactory: () => string = () => crypto.randomUUID(),
  ) {}

  private async findByEvent(input: AcceptJobInput): Promise<JobRow | null> {
    return this.db.prepare(
      `SELECT j.*
         FROM jobs j
         LEFT JOIN job_events e ON e.job_id = j.id
        WHERE j.profile_id = ?1
          AND (j.event_id = ?2 OR e.event_id = ?2)
        ORDER BY j.created_at ASC
        LIMIT 1`,
    ).bind(input.profileId, input.eventId).first<JobRow>();
  }

  private async findByKey(profileId: string, idempotencyKey: string): Promise<JobRow | null> {
    return this.db.prepare(
      `SELECT * FROM jobs WHERE profile_id = ?1 AND idempotency_key = ?2 LIMIT 1`,
    ).bind(profileId, idempotencyKey).first<JobRow>();
  }

  private sameRequest(row: JobRow, input: AcceptJobInput, idempotencyKey: string): boolean {
    return row.account_id === input.accountId
      && row.profile_id === input.profileId
      && row.job_kind === input.jobKind
      && row.idempotency_key === idempotencyKey
      && row.input_hash === input.inputDigest
      && row.event_id === input.eventId
      && row.trust_domain === (input.trustDomain ?? "official")
      && row.contract_version === (input.contractVersion ?? "v1");
  }

  private async existingFor(input: AcceptJobInput, idempotencyKey: string): Promise<JobRow | null> {
    const event = await this.findByEvent(input);
    return event ?? this.findByKey(input.profileId, idempotencyKey);
  }

  private async reserveRevision(
    accountId: string,
    profileId: string,
    trustDomain: TrustDomain,
    now: number,
  ): Promise<{ revision: number; previousRevision: number | null }> {
    await this.db.prepare(
      `INSERT INTO profile_revision_counters(account_id, profile_id, trust_domain, next_revision, updated_at)
       VALUES (?1, ?2, ?3, 0, ?4)
       ON CONFLICT(account_id, profile_id, trust_domain) DO NOTHING`,
    ).bind(accountId, profileId, trustDomain, now).run();
    const row = await this.db.prepare(
      `UPDATE profile_revision_counters
          SET next_revision = next_revision + 1, updated_at = ?4
        WHERE account_id = ?1 AND profile_id = ?2 AND trust_domain = ?3
      RETURNING next_revision`,
    ).bind(accountId, profileId, trustDomain, now).first<CounterRow>();
    if (!row) throw new JobLedgerError("revision_reservation_failed", 503, true);
    const revision = numberValue(row.next_revision);
    return { revision, previousRevision: revision > 1 ? revision - 1 : null };
  }

  async acceptJob(input: AcceptJobInput): Promise<AcceptedJob> {
    validateInput(input);
    const contractVersion = input.contractVersion ?? "v1";
    const trustDomain = input.trustDomain ?? "official";
    const idempotencyKey = makeIdempotencyKey(input.profileId, input.inputDigest);
    const existing = await this.existingFor(input, idempotencyKey);
    if (existing) {
      if (!this.sameRequest(existing, input, idempotencyKey)) throw new IdempotencyConflictError();
      return { status: "duplicate", job: toEnvelope(existing) };
    }

    const now = this.clock();
    const reservation = await this.reserveRevision(input.accountId, input.profileId, trustDomain, now);
    const jobId = this.idFactory();
    const revisionId = `revision:${jobId}`;
    const job: JobEnvelope = {
      jobId,
      accountId: input.accountId,
      profileId: input.profileId,
      jobKind: input.jobKind,
      eventId: input.eventId,
      requestId: input.requestId ?? null,
      correlationId: input.correlationId ?? input.requestId ?? jobId,
      contractVersion,
      trustDomain,
      idempotencyKey,
      inputDigest: input.inputDigest,
      revision: reservation.revision,
      workflowRun: 0,
      maxAttempts: input.maxAttempts ?? 4,
      inputKey: input.inputKey ?? null,
      artifactKey: input.artifactKey ?? null,
    };

    try {
      await this.db.batch([
        this.db.prepare(
          `INSERT INTO jobs(
             id, account_id, profile_id, job_kind, idempotency_key, input_hash,
             status, revision, created_at, updated_at, event_id, request_id,
             contract_version, trust_domain, correlation_id, max_attempts,
             workflow_run, input_key, artifact_key
           ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'queued', ?7, ?8, ?8, ?9, ?10, ?11, ?12, ?13, ?14, 0, ?15, ?16)`,
        ).bind(
          job.jobId, job.accountId, job.profileId, job.jobKind, job.idempotencyKey,
          job.inputDigest, job.revision, now, job.eventId, job.requestId,
          job.contractVersion, job.trustDomain, job.correlationId, job.maxAttempts,
          job.inputKey, job.artifactKey,
        ),
        this.db.prepare(
          `INSERT INTO job_events(
             id, job_id, account_id, profile_id, event_id, request_id,
             event_kind, payload_digest, accepted_at
           ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
        ).bind(
          this.idFactory(), job.jobId, job.accountId, job.profileId, job.eventId,
          job.requestId, job.jobKind, job.inputDigest, now,
        ),
        this.db.prepare(
          `INSERT INTO job_revisions(
             id, job_id, account_id, profile_id, trust_domain, revision,
             previous_revision, input_digest, reserved_at
           ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
        ).bind(
          revisionId, job.jobId, job.accountId, job.profileId, job.trustDomain,
          job.revision, reservation.previousRevision, job.inputDigest, now,
        ),
        this.db.prepare(
          `INSERT INTO job_dispatches(
             job_id, dispatch_key, status, attempts, lease_until,
             next_attempt_at, last_error_code, updated_at
           ) VALUES (?1, ?2, 'pending', 0, NULL, NULL, NULL, ?3)`,
        ).bind(job.jobId, `queue:${job.jobId}`, now),
        auditStatement(this.db, this.idFactory(), job.accountId, job.profileId, "job.queued", "accepted", now, job.inputDigest),
      ]);
    } catch (error) {
      const raced = await this.existingFor(input, idempotencyKey);
      if (raced) {
        if (!this.sameRequest(raced, input, idempotencyKey)) throw new IdempotencyConflictError();
        return { status: "duplicate", job: toEnvelope(raced) };
      }
      throw error;
    }
    return { status: "accepted", job };
  }

  async getOwnedJob(accountId: string, jobId: string): Promise<JobEnvelope | null> {
    const row = await this.db.prepare(
      `SELECT * FROM jobs WHERE account_id = ?1 AND id = ?2 LIMIT 1`,
    ).bind(accountId, jobId).first<JobRow>();
    return row ? toEnvelope(row) : null;
  }

  private async getJobRow(jobId: string): Promise<JobRow> {
    const row = await this.db.prepare("SELECT * FROM jobs WHERE id = ?1 LIMIT 1").bind(jobId).first<JobRow>();
    if (!row) throw new JobLedgerError("job_not_found", 404, false);
    return row;
  }

  async getStatus(accountId: string, jobId: string): Promise<JobStatusView | null> {
    const row = await this.db.prepare(
      `SELECT * FROM jobs WHERE account_id = ?1 AND id = ?2 LIMIT 1`,
    ).bind(accountId, jobId).first<JobRow>();
    if (!row) return null;
    const [stepsResult, outcomesResult, pointer] = await Promise.all([
      this.db.prepare(
        `SELECT workflow_run, step_name, attempt, status, retryable,
                started_at, finished_at, error_code
           FROM workflow_step_attempts WHERE job_id = ?1
          ORDER BY workflow_run, step_name, attempt`,
      ).bind(jobId).all<StepRow>(),
      this.db.prepare(
        `SELECT workflow_run, status, output_digest, manifest_sha256,
                error_code, recorded_at
           FROM revision_outcomes WHERE job_id = ?1
          ORDER BY workflow_run`,
      ).bind(jobId).all<OutcomeRow>(),
      this.db.prepare(
        `SELECT revision, revision_id, manifest_sha256, artifact_key, updated_at
           FROM latest_pointers
          WHERE account_id = ?1 AND profile_id = ?2 AND trust_domain = ?3`,
      ).bind(row.account_id, row.profile_id, row.trust_domain).first<PointerRow>(),
    ]);
    return {
      ...toEnvelope(row),
      status: row.status,
      createdAt: numberValue(row.created_at),
      updatedAt: numberValue(row.updated_at),
      startedAt: row.started_at === null ? null : numberValue(row.started_at),
      finishedAt: row.finished_at === null ? null : numberValue(row.finished_at),
      cancelRequestedAt: row.cancel_requested_at === null ? null : numberValue(row.cancel_requested_at),
      terminalReason: row.terminal_reason,
      nextAttemptAt: row.next_attempt_at === null ? null : numberValue(row.next_attempt_at),
      steps: (stepsResult.results ?? []).map((step) => ({
        workflowRun: numberValue(step.workflow_run),
        stepName: step.step_name,
        attempt: numberValue(step.attempt),
        status: step.status,
        retryable: Boolean(step.retryable),
        startedAt: numberValue(step.started_at),
        finishedAt: step.finished_at === null ? null : numberValue(step.finished_at),
        errorCode: step.error_code ?? null,
      })),
      outcomes: (outcomesResult.results ?? []).map((outcome) => ({
        workflowRun: numberValue(outcome.workflow_run),
        status: outcome.status,
        outputDigest: outcome.output_digest ?? null,
        manifestSha256: outcome.manifest_sha256 ?? null,
        errorCode: outcome.error_code ?? null,
        recordedAt: numberValue(outcome.recorded_at),
      })),
      latest: pointer ? {
        revision: numberValue(pointer.revision),
        revisionId: pointer.revision_id,
        manifestSha256: pointer.manifest_sha256,
        artifactKey: pointer.artifact_key ?? null,
        updatedAt: numberValue(pointer.updated_at),
      } : null,
    };
  }

  async claimDelivery(jobId: string, messageId: string): Promise<"claimed" | "duplicate" | "terminal"> {
    const now = this.clock();
    const prior = await this.db.prepare(
      `SELECT status FROM job_deliveries WHERE job_id = ?1 AND message_id = ?2 LIMIT 1`,
    ).bind(jobId, messageId).first<{ status: string }>();
    if (prior && prior.status !== "retry") return "duplicate";
    if (prior?.status === "retry") {
      await this.db.prepare(
        `UPDATE job_deliveries SET status = 'claimed', received_at = ?3, finished_at = NULL
          WHERE job_id = ?1 AND message_id = ?2`,
      ).bind(jobId, messageId, now).run();
    } else {
      const result = await this.db.prepare(
        `INSERT OR IGNORE INTO job_deliveries(
           id, job_id, message_id, status, received_at, finished_at
         ) VALUES (?1, ?2, ?3, 'claimed', ?4, NULL)`,
      ).bind(this.idFactory(), jobId, messageId, now).run();
      if (changes(result) !== 1) return "duplicate";
    }
    const job = await this.getJobRow(jobId);
    if (["succeeded", "failed", "cancelled"].includes(job.status)) {
      await this.markDelivery(jobId, messageId, "acked");
      return "terminal";
    }
    await this.db.prepare(
      `UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?2), updated_at = ?2
        WHERE id = ?1 AND status IN ('queued', 'running')`,
    ).bind(jobId, now).run();
    return "claimed";
  }

  async markDelivery(jobId: string, messageId: string, status: "acked" | "retry"): Promise<void> {
    await this.db.prepare(
      `UPDATE job_deliveries SET status = ?3, finished_at = ?4
        WHERE job_id = ?1 AND message_id = ?2`,
    ).bind(jobId, messageId, status, this.clock()).run();
  }

  async markWorkflowStarted(jobId: string, instanceId: string): Promise<void> {
    await this.db.prepare(
      `UPDATE jobs SET workflow_instance_id = COALESCE(workflow_instance_id, ?2), updated_at = ?3
        WHERE id = ?1`,
    ).bind(jobId, instanceId, this.clock()).run();
  }

  async recordQueueRetry(jobId: string, messageId: string, error: unknown, attempt: number): Promise<void> {
    const now = this.clock();
    const delay = queueRetryDelaySeconds(attempt);
    const code = errorCode(error);
    await this.db.batch([
      this.db.prepare(
        `UPDATE jobs SET next_attempt_at = ?2, updated_at = ?3
          WHERE id = ?1 AND status IN ('queued', 'running')`,
      ).bind(jobId, now + delay, now),
      this.db.prepare(
        `UPDATE job_deliveries SET status = 'retry', finished_at = ?3
          WHERE job_id = ?1 AND message_id = ?2`,
      ).bind(jobId, messageId, now),
      auditStatement(this.db, this.idFactory(), (await this.getJobRow(jobId)).account_id, (await this.getJobRow(jobId)).profile_id, "job.retry", "retryable", now, null, code),
    ]);
  }

  async beginStepAttempt(
    job: JobEnvelope,
    stepName: string,
    attempt: number,
    retryable: boolean,
    timeoutSeconds: number,
  ): Promise<void> {
    const row = await this.getJobRow(job.jobId);
    if (row.status === "cancelled" || row.cancel_requested_at !== null) {
      throw new JobLedgerError("job_cancelled", 409, false);
    }
    await this.db.prepare(
      `INSERT OR IGNORE INTO workflow_step_attempts(
         id, job_id, workflow_run, step_name, attempt, status, retryable,
         started_at, finished_at, timeout_seconds, error_code, error_hash
       ) VALUES (?1, ?2, ?3, ?4, ?5, 'running', ?6, ?7, NULL, ?8, NULL, NULL)`,
    ).bind(
      this.idFactory(), job.jobId, job.workflowRun, stepName, attempt,
      retryable ? 1 : 0, this.clock(), timeoutSeconds,
    ).run();
  }

  async finishStepAttempt(job: JobEnvelope, stepName: string, attempt: number): Promise<void> {
    await this.db.prepare(
      `UPDATE workflow_step_attempts
          SET status = 'succeeded', finished_at = ?5
        WHERE job_id = ?1 AND workflow_run = ?2 AND step_name = ?3
          AND attempt = ?4 AND status = 'running'`,
    ).bind(job.jobId, job.workflowRun, stepName, attempt, this.clock()).run();
  }

  async failStepAttempt(
    job: JobEnvelope,
    stepName: string,
    attempt: number,
    error: unknown,
  ): Promise<void> {
    const code = errorCode(error);
    const hash = await sha256Hex(errorMessage(error));
    await this.db.prepare(
      `UPDATE workflow_step_attempts
          SET status = 'failed', finished_at = ?5, error_code = ?6, error_hash = ?7
        WHERE job_id = ?1 AND workflow_run = ?2 AND step_name = ?3
          AND attempt = ?4 AND status = 'running'`,
    ).bind(job.jobId, job.workflowRun, stepName, attempt, this.clock(), code, hash).run();
  }

  async recordOutcome(
    job: JobEnvelope,
    status: OutcomeStatus,
    outputDigest?: string | null,
    manifestSha256?: string | null,
    error?: unknown,
  ): Promise<boolean> {
    const now = this.clock();
    const code = error ? errorCode(error) : null;
    const hash = error ? await sha256Hex(errorMessage(error)) : null;
    const result = await this.db.prepare(
      `INSERT OR IGNORE INTO revision_outcomes(
         id, revision_id, job_id, workflow_run, status, output_digest,
         manifest_sha256, error_code, error_hash, recorded_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)`,
    ).bind(
      this.idFactory(), `revision:${job.jobId}`, job.jobId, job.workflowRun,
      status, outputDigest ?? null, manifestSha256 ?? null, code, hash, now,
    ).run();
    if (changes(result) !== 1) return false;
    const terminalReason = status === "succeeded" ? null : (code ?? status);
    await this.db.batch([
      this.db.prepare(
        `UPDATE jobs SET status = ?2, finished_at = ?3, terminal_reason = ?4,
                         next_attempt_at = NULL, updated_at = ?3
          WHERE id = ?1`,
      ).bind(job.jobId, status, now, terminalReason),
      auditStatement(
        this.db,
        this.idFactory(),
        job.accountId,
        job.profileId,
        status === "succeeded" ? "job.succeeded" : "job.failed",
        status,
        now,
        outputDigest ?? job.inputDigest,
        code,
      ),
    ]);
    return true;
  }

  async publishLatest(
    job: JobEnvelope,
    manifestSha256: string,
    artifactKey?: string | null,
  ): Promise<{ published: boolean; pointer: LatestPointerView }> {
    if (!isSha256(manifestSha256)) throw new JobLedgerError("invalid_manifest_digest", 400, false);
    const revision = await this.db.prepare(
      `SELECT id FROM job_revisions
        WHERE id = ?1 AND job_id = ?2 AND account_id = ?3 AND profile_id = ?4
          AND trust_domain = ?5 AND revision = ?6`,
    ).bind(`revision:${job.jobId}`, job.jobId, job.accountId, job.profileId, job.trustDomain, job.revision).first<{ id: string }>();
    if (!revision) throw new JobLedgerError("revision_not_found", 409, false);
    const now = this.clock();
    const result = await this.db.prepare(
      `INSERT INTO latest_pointers(
         account_id, profile_id, trust_domain, revision, revision_id,
         manifest_sha256, artifact_key, updated_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
       ON CONFLICT(account_id, profile_id, trust_domain) DO UPDATE SET
         revision = excluded.revision,
         revision_id = excluded.revision_id,
         manifest_sha256 = excluded.manifest_sha256,
         artifact_key = excluded.artifact_key,
         updated_at = excluded.updated_at
       WHERE excluded.revision > latest_pointers.revision`,
    ).bind(
      job.accountId, job.profileId, job.trustDomain, job.revision, revision.id,
      manifestSha256, artifactKey ?? job.artifactKey, now,
    ).run();
    const pointer = await this.db.prepare(
      `SELECT revision, revision_id, manifest_sha256, artifact_key, updated_at
         FROM latest_pointers
        WHERE account_id = ?1 AND profile_id = ?2 AND trust_domain = ?3`,
    ).bind(job.accountId, job.profileId, job.trustDomain).first<PointerRow>();
    if (!pointer) throw new JobLedgerError("latest_pointer_missing", 503, true);
    const published = changes(result) === 1;
    await this.db.prepare(
      `INSERT INTO audit_events(
         id, account_id, profile_id, actor_kind, event_type, reason_code,
         request_id, resource_hash, input_hash, status, occurred_at
       ) VALUES (?1, ?2, ?3, 'service', ?4, ?5, NULL, ?6, ?7, 'success', ?8)`,
    ).bind(
      this.idFactory(), job.accountId, job.profileId,
      published ? "revision.published" : "revision.superseded",
      published ? null : "older_revision", manifestSha256, job.inputDigest, now,
    ).run();
    return {
      published,
      pointer: {
        revision: numberValue(pointer.revision),
        revisionId: pointer.revision_id,
        manifestSha256: pointer.manifest_sha256,
        artifactKey: pointer.artifact_key ?? null,
        updatedAt: numberValue(pointer.updated_at),
      },
    };
  }

  async cancel(accountId: string, jobId: string, reasonCode = "operator_cancelled"): Promise<JobStatus> {
    const job = await this.getJobRow(jobId);
    if (job.account_id !== accountId) throw new JobLedgerError("job_not_found", 404, false);
    if (job.status === "queued") {
      const now = this.clock();
      await this.db.prepare(
        `UPDATE jobs SET status = 'cancelled', finished_at = ?3,
                         terminal_reason = ?4, updated_at = ?3
          WHERE id = ?1 AND status = 'queued'`,
      ).bind(jobId, accountId, now, reasonCode).run();
      await this.recordOutcome(toEnvelope({ ...job, status: "cancelled" }), "cancelled", null, null, new JobLedgerError(reasonCode, 409, false));
      return "cancelled";
    }
    if (job.status === "running") {
      await this.db.prepare(
        `UPDATE jobs SET cancel_requested_at = ?2, updated_at = ?2
          WHERE id = ?1 AND status = 'running' AND cancel_requested_at IS NULL`,
      ).bind(jobId, this.clock()).run();
    }
    return job.status;
  }

  async manualRetry(accountId: string, jobId: string, requestedBy: string, reasonCode = "operator_retry"): Promise<JobEnvelope> {
    const job = await this.getJobRow(jobId);
    if (job.account_id !== accountId) throw new JobLedgerError("job_not_found", 404, false);
    if (job.status !== "failed") throw new JobLedgerError("job_not_retryable", 409, false);
    const nextRun = numberValue(job.workflow_run) + 1;
    const now = this.clock();
    const result = await this.db.batch([
      this.db.prepare(
        `UPDATE jobs SET status = 'queued', workflow_run = ?2, finished_at = NULL,
                         terminal_reason = NULL, cancel_requested_at = NULL,
                         next_attempt_at = NULL, updated_at = ?3
          WHERE id = ?1 AND status = 'failed'`,
      ).bind(jobId, nextRun, now),
      this.db.prepare(
        `INSERT INTO job_requeues(
           id, job_id, workflow_run, requested_by, reason_code, requested_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)`,
      ).bind(this.idFactory(), jobId, nextRun, requestedBy, reasonCode, now),
      this.db.prepare(
        `UPDATE job_dispatches SET status = 'pending', lease_until = NULL,
                         next_attempt_at = NULL, last_error_code = NULL, updated_at = ?2
          WHERE job_id = ?1`,
      ).bind(jobId, now),
      auditStatement(this.db, this.idFactory(), job.account_id, job.profile_id, "job.retry", "manual", now, job.input_hash, reasonCode),
    ]);
    if (changes(result[0] as ChangeResult) !== 1) throw new JobLedgerError("job_not_retryable", 409, false);
    return toEnvelope({ ...job, status: "queued", workflow_run: nextRun, terminal_reason: null, finished_at: null, cancel_requested_at: null, next_attempt_at: null });
  }

  async claimDispatch(jobId: string): Promise<boolean> {
    const now = this.clock();
    const result = await this.db.prepare(
      `UPDATE job_dispatches
          SET status = 'sending', attempts = attempts + 1,
              lease_until = ?2, updated_at = ?2
        WHERE job_id = ?1 AND status <> 'sent'
          AND (lease_until IS NULL OR lease_until <= ?2)
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?2)`,
    ).bind(jobId, now + 90).run();
    return changes(result) === 1;
  }

  async markDispatchSent(jobId: string): Promise<void> {
    await this.db.prepare(
      `UPDATE job_dispatches SET status = 'sent', lease_until = NULL,
              next_attempt_at = NULL, last_error_code = NULL, updated_at = ?2
        WHERE job_id = ?1`,
    ).bind(jobId, this.clock()).run();
  }

  async markDispatchPending(jobId: string, error: unknown): Promise<void> {
    const now = this.clock();
    await this.db.prepare(
      `UPDATE job_dispatches SET status = 'pending', lease_until = NULL,
              next_attempt_at = ?2, last_error_code = ?3, updated_at = ?2
        WHERE job_id = ?1`,
    ).bind(jobId, now + queueRetryDelaySeconds(1), errorCode(error)).run();
  }

  async recordScheduleRun(
    schedule: ScheduleName,
    profileId: string,
    scheduledAt: number,
    jobId: string,
    status: "accepted" | "duplicate",
  ): Promise<void> {
    await this.db.prepare(
      `INSERT OR IGNORE INTO schedule_runs(
         schedule_name, profile_id, scheduled_at, job_id, status, recorded_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)`,
    ).bind(schedule, profileId, scheduledAt, jobId, status, this.clock()).run();
  }

  async scheduleProfiles(schedule: ScheduleName): Promise<ScheduleProfile[]> {
    const result = await this.db.prepare(
      `SELECT p.account_id, p.id AS profile_id, p.status AS profile_status,
              a.status AS account_status
         FROM profiles p
         JOIN accounts a ON a.id = p.account_id
        WHERE (
          (?1 IN ('monthly', 'daily-backup') AND p.status = 'active' AND a.status = 'active')
          OR (?1 = 'deletion-sweep' AND p.status = 'deletion_pending')
        )
        ORDER BY p.account_id, p.id`,
    ).bind(schedule).all<ScheduleProfile>();
    return result.results ?? [];
  }
}

export class JobDispatcher {
  constructor(
    private readonly ledger: D1JobLedger,
    private readonly queue: Queue<JobEnvelope>,
  ) {}

  async acceptAndEnqueue(input: AcceptJobInput): Promise<AcceptedJob> {
    const accepted = await this.ledger.acceptJob(input);
    await this.dispatch(accepted.job);
    return accepted;
  }

  async dispatch(job: JobEnvelope): Promise<boolean> {
    if (!(await this.ledger.claimDispatch(job.jobId))) return false;
    try {
      await this.queue.send(job, { contentType: "json" });
      await this.ledger.markDispatchSent(job.jobId);
      return true;
    } catch (error) {
      await this.ledger.markDispatchPending(job.jobId, error);
      throw new JobLedgerError("queue_unavailable", 503, true);
    }
  }
}

export type QueueMessageLike = {
  readonly id: string;
  readonly body: unknown;
  readonly attempts: number;
  ack(): void;
  retry(options?: { delaySeconds?: number }): void;
};

export type QueueConsumerDependencies = {
  ledger: D1JobLedger;
  startWorkflow: (job: JobEnvelope) => Promise<void>;
};

function queueEnvelope(body: unknown): JobEnvelope {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new JobLedgerError("invalid_queue_message", 400, false);
  }
  const value = body as Record<string, unknown>;
  if (
    typeof value.jobId !== "string" || typeof value.accountId !== "string"
    || typeof value.profileId !== "string" || typeof value.eventId !== "string"
    || typeof value.inputDigest !== "string" || typeof value.idempotencyKey !== "string"
    || typeof value.revision !== "number" || typeof value.workflowRun !== "number"
    || typeof value.maxAttempts !== "number" || !isJobKind(value.jobKind)
    || !["official", "self_hosted"].includes(String(value.trustDomain))
  ) throw new JobLedgerError("invalid_queue_message", 400, false);
  if (!isSha256(value.inputDigest)) throw new JobLedgerError("invalid_queue_message", 400, false);
  return value as unknown as JobEnvelope;
}

function duplicateWorkflowError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /already exists|already started|duplicate workflow|unique constraint/i.test(message);
}

export async function consumeQueueMessage(
  message: QueueMessageLike,
  dependencies: QueueConsumerDependencies,
): Promise<void> {
  let job: JobEnvelope;
  try {
    job = queueEnvelope(message.body);
  } catch {
    // A malformed message has no trusted owner/job row to update. Acking it
    // prevents an unbounded poison-message loop; the Queue provider retains
    // provider-level delivery metrics for the operator alert.
    message.ack();
    return;
  }

  const claim = await dependencies.ledger.claimDelivery(job.jobId, message.id);
  if (claim !== "claimed") {
    message.ack();
    return;
  }
  try {
    await dependencies.startWorkflow(job);
    await dependencies.ledger.markWorkflowStarted(job.jobId, `job:${job.jobId}:run:${job.workflowRun}`);
    await dependencies.ledger.markDelivery(job.jobId, message.id, "acked");
    message.ack();
  } catch (error) {
    if (duplicateWorkflowError(error)) {
      await dependencies.ledger.markWorkflowStarted(job.jobId, `job:${job.jobId}:run:${job.workflowRun}`);
      await dependencies.ledger.markDelivery(job.jobId, message.id, "acked");
      message.ack();
      return;
    }
    await dependencies.ledger.recordQueueRetry(job.jobId, message.id, error, message.attempts);
    message.retry({ delaySeconds: queueRetryDelaySeconds(message.attempts) });
  }
}

export async function dispatchSchedule(
  ledger: D1JobLedger,
  dispatcher: JobDispatcher,
  schedule: ScheduleName,
  scheduledAt: number,
): Promise<{ accepted: number; duplicates: number }> {
  const profiles = await ledger.scheduleProfiles(schedule);
  let accepted = 0;
  let duplicates = 0;
  for (const profile of profiles) {
    const eventId = makeScheduleEventId(schedule, profile.profile_id, scheduledAt);
    const jobKind: JobKind = schedule === "monthly"
      ? "monthly"
      : schedule === "daily-backup" ? "export" : "delete";
    const inputDigest = await sha256Hex(canonicalJson({
      contract_version: "v1",
      job_kind: jobKind,
      profile_id: profile.profile_id,
      schedule,
      scheduled_at: scheduledAt,
      trust_domain: "official",
    }));
    const result = await dispatcher.acceptAndEnqueue({
      accountId: profile.account_id,
      profileId: profile.profile_id,
      jobKind,
      eventId,
      requestId: `schedule:${scheduledAt}`,
      correlationId: `schedule:${schedule}:${scheduledAt}`,
      inputDigest,
      trustDomain: "official",
    });
    await ledger.recordScheduleRun(schedule, profile.profile_id, scheduledAt, result.job.jobId, result.status);
    if (result.status === "accepted") accepted += 1;
    else duplicates += 1;
  }
  return { accepted, duplicates };
}
