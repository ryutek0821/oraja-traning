import {
  WorkflowEntrypoint,
  type WorkflowEvent,
  type WorkflowStep,
  type WorkflowStepConfig,
} from "cloudflare:workers";
import {
  D1JobLedger,
  JobLedgerError,
  sha256Hex,
  type JobEnvelope,
} from "./job-ledger";
import {
  WORKFLOW_STEP_POLICIES,
  isTransientError,
  workflowInstanceId,
  type WorkflowStepName,
} from "./workflow-state";

export type WorkflowEnv = {
  CONTROL_DB: D1Database;
  RAW_BUCKET: R2Bucket;
  ARTIFACT_BUCKET: R2Bucket;
  PYTHON_PROCESSOR: DurableObjectNamespace;
};

export type WorkflowPayload = JobEnvelope;

export type VerifyInputOutput = {
  verified: boolean;
  inputDigest: string;
};

export type NormalizeOutput = {
  normalizedDigest: string;
};

export type ModelOutput = {
  modelDigest: string;
};

export type TablesOutput = {
  recommendationDigest: string;
  menuDigest: string;
};

export type ArtifactOutput = {
  manifestSha256: string;
  artifactKey: string | null;
};

export type PublishOutput = {
  published: boolean;
  currentRevision: number;
  manifestSha256: string;
};

export interface WorkflowActivities {
  verifyR2Input(job: JobEnvelope): Promise<VerifyInputOutput>;
  normalize(job: JobEnvelope, input: VerifyInputOutput): Promise<NormalizeOutput>;
  updateModel(job: JobEnvelope, input: NormalizeOutput): Promise<ModelOutput>;
  generateTables(job: JobEnvelope, input: ModelOutput): Promise<TablesOutput>;
  verifyArtifact(job: JobEnvelope, input: TablesOutput): Promise<ArtifactOutput>;
  publish(job: JobEnvelope, input: ArtifactOutput): Promise<PublishOutput>;
}

export class WorkflowActivityError extends Error {
  constructor(
    public readonly code: string,
    public readonly retryable: boolean,
  ) {
    super(code);
    this.name = "WorkflowActivityError";
  }
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object" && "code" in error) {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code.length <= 96) return code;
  }
  return "workflow_step_failed";
}

function workflowConfig(stepName: WorkflowStepName): WorkflowStepConfig {
  const policy = WORKFLOW_STEP_POLICIES[stepName];
  return {
    retries: {
      limit: policy.retryable ? policy.maxAttempts - 1 : 0,
      delay: `${policy.backoffBaseSeconds} seconds` as `${number} seconds`,
      backoff: "exponential",
    },
    timeout: `${policy.timeoutSeconds} seconds` as `${number} seconds`,
  };
}

async function runStep<T extends Record<string, unknown>>(
  step: WorkflowStep,
  ledger: D1JobLedger,
  job: JobEnvelope,
  stepName: WorkflowStepName,
  callback: () => Promise<T>,
): Promise<T> {
  const policy = WORKFLOW_STEP_POLICIES[stepName];
  const doStep = step.do.bind(step) as unknown as (
    name: string,
    config: WorkflowStepConfig,
    callback: (context: { attempt: number }) => Promise<T>,
  ) => Promise<T>;
  return doStep(
    stepName,
    workflowConfig(stepName),
    async (context) => {
      const attempt = context.attempt;
      await ledger.beginStepAttempt(job, stepName, attempt, policy.retryable, policy.timeoutSeconds);
      try {
        const output = await callback();
        await ledger.finishStepAttempt(job, stepName, attempt);
        return output;
      } catch (error) {
        await ledger.failStepAttempt(job, stepName, attempt, error);
        if (!policy.retryable || !isTransientError(error)) {
          throw new WorkflowActivityError(errorCode(error), false);
        }
        throw error;
      }
    },
  );
}

function defaultActivities(env: WorkflowEnv, ledger: D1JobLedger): WorkflowActivities {
  return {
    async verifyR2Input(job) {
      if (job.inputKey) {
        const object = await env.RAW_BUCKET.head(job.inputKey);
        if (!object) throw new WorkflowActivityError("input_manifest_not_found", true);
        const recordedDigest = object.customMetadata?.input_digest;
        if (recordedDigest && recordedDigest !== job.inputDigest) {
          throw new WorkflowActivityError("input_digest_mismatch", false);
        }
      }
      return { verified: true, inputDigest: job.inputDigest };
    },

    async normalize(job, input) {
      return { normalizedDigest: await sha256Hex(`${job.inputDigest}:normalize:${input.inputDigest}`) };
    },

    async updateModel(job, input) {
      return { modelDigest: await sha256Hex(`${job.profileId}:${job.revision}:model:${input.normalizedDigest}`) };
    },

    async generateTables(job, input) {
      return {
        recommendationDigest: await sha256Hex(`${job.profileId}:${job.revision}:recommendation:${input.modelDigest}`),
        menuDigest: await sha256Hex(`${job.profileId}:${job.revision}:menu:${input.modelDigest}`),
      };
    },

    async verifyArtifact(job, input) {
      if (job.artifactKey) {
        const object = await env.ARTIFACT_BUCKET.head(job.artifactKey);
        if (!object) throw new WorkflowActivityError("artifact_not_found", true);
        const recordedDigest = object.customMetadata?.manifest_sha256;
        if (recordedDigest && recordedDigest !== job.inputDigest) {
          throw new WorkflowActivityError("artifact_manifest_mismatch", false);
        }
      }
      const manifestSha256 = await sha256Hex(JSON.stringify({
        artifact_key: job.artifactKey,
        menu_digest: input.menuDigest,
        profile_id: job.profileId,
        recommendation_digest: input.recommendationDigest,
        revision: job.revision,
      }));
      return { manifestSha256, artifactKey: job.artifactKey };
    },

    async publish(job, input) {
      const result = await ledger.publishLatest(job, input.manifestSha256, input.artifactKey);
      return {
        published: result.published,
        currentRevision: result.pointer.revision,
        manifestSha256: input.manifestSha256,
      };
    },
  };
}

export async function runWorkflowPipeline(
  step: WorkflowStep,
  ledger: D1JobLedger,
  job: JobEnvelope,
  activities: WorkflowActivities,
): Promise<PublishOutput> {
  const verified = await runStep(step, ledger, job, "r2-verify", () => activities.verifyR2Input(job));
  const normalized = await runStep(step, ledger, job, "normalize", () => activities.normalize(job, verified));
  const model = await runStep(step, ledger, job, "model-update", () => activities.updateModel(job, normalized));
  const tables = await runStep(step, ledger, job, "generate-tables", () => activities.generateTables(job, model));
  const artifact = await runStep(step, ledger, job, "artifact-verify", () => activities.verifyArtifact(job, tables));
  return runStep(step, ledger, job, "publish", () => activities.publish(job, artifact));
}

export class GenerateWorkflow extends WorkflowEntrypoint<WorkflowEnv, WorkflowPayload> {
  async run(
    event: Readonly<WorkflowEvent<WorkflowPayload>>,
    step: WorkflowStep,
  ): Promise<PublishOutput | { status: string; jobId: string }> {
    const ledger = new D1JobLedger(this.env.CONTROL_DB);
    const current = await ledger.getStatus(event.payload.accountId, event.payload.jobId);
    if (!current) throw new JobLedgerError("job_not_found", 404, false);
    if (current.workflowRun !== event.payload.workflowRun) {
      return { status: "stale_workflow_run", jobId: event.payload.jobId };
    }
    if (["succeeded", "failed", "cancelled"].includes(current.status)) {
      return { status: current.status, jobId: event.payload.jobId };
    }
    const job = event.payload;
    try {
      const output = await runWorkflowPipeline(step, ledger, job, defaultActivities(this.env, ledger));
      await ledger.recordOutcome(job, "succeeded", output.manifestSha256, output.manifestSha256);
      return output;
    } catch (error) {
      const cancelled = error instanceof JobLedgerError && error.code === "job_cancelled";
      await ledger.recordOutcome(
        job,
        cancelled ? "cancelled" : "failed",
        null,
        null,
        error,
      );
      throw error;
    }
  }
}

export { workflowInstanceId };
