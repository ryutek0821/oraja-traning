export type JobKind =
  | "initial"
  | "monthly"
  | "play"
  | "regenerate"
  | "export"
  | "delete";

export type TrustDomain = "official" | "self_hosted";

export type WorkflowStepName =
  | "r2-verify"
  | "normalize"
  | "model-update"
  | "generate-tables"
  | "artifact-verify"
  | "publish";

export type StepPolicy = {
  maxAttempts: number;
  timeoutSeconds: number;
  retryable: boolean;
  backoffBaseSeconds: number;
  maxBackoffSeconds: number;
};

export const WORKFLOW_STEP_POLICIES: Readonly<Record<WorkflowStepName, StepPolicy>> = {
  "r2-verify": {
    maxAttempts: 4,
    timeoutSeconds: 60,
    retryable: true,
    backoffBaseSeconds: 5,
    maxBackoffSeconds: 120,
  },
  normalize: {
    maxAttempts: 3,
    timeoutSeconds: 300,
    retryable: true,
    backoffBaseSeconds: 5,
    maxBackoffSeconds: 180,
  },
  "model-update": {
    maxAttempts: 3,
    timeoutSeconds: 600,
    retryable: true,
    backoffBaseSeconds: 10,
    maxBackoffSeconds: 300,
  },
  "generate-tables": {
    maxAttempts: 3,
    timeoutSeconds: 300,
    retryable: true,
    backoffBaseSeconds: 5,
    maxBackoffSeconds: 180,
  },
  "artifact-verify": {
    maxAttempts: 2,
    timeoutSeconds: 120,
    retryable: false,
    backoffBaseSeconds: 5,
    maxBackoffSeconds: 30,
  },
  publish: {
    maxAttempts: 4,
    timeoutSeconds: 120,
    retryable: true,
    backoffBaseSeconds: 5,
    maxBackoffSeconds: 120,
  },
};

export const SCHEDULE_CRONS = {
  monthly: "0 2 1 * *",
  "daily-backup": "0 3 * * *",
  "deletion-sweep": "0 4 * * *",
} as const;

export type ScheduleName = keyof typeof SCHEDULE_CRONS;

export const JOB_KINDS: readonly JobKind[] = [
  "initial",
  "monthly",
  "play",
  "regenerate",
  "export",
  "delete",
];

export function isJobKind(value: unknown): value is JobKind {
  return typeof value === "string" && (JOB_KINDS as readonly string[]).includes(value);
}

export function isWorkflowStepName(value: unknown): value is WorkflowStepName {
  return typeof value === "string" && value in WORKFLOW_STEP_POLICIES;
}

export function retryDelaySeconds(step: WorkflowStepName, attempt: number): number {
  const policy = WORKFLOW_STEP_POLICIES[step];
  const normalizedAttempt = Math.max(1, Math.floor(attempt));
  return Math.min(
    policy.maxBackoffSeconds,
    policy.backoffBaseSeconds * 2 ** Math.max(0, normalizedAttempt - 1),
  );
}

export function queueRetryDelaySeconds(attempt: number): number {
  const normalizedAttempt = Math.max(1, Math.floor(attempt));
  return Math.min(300, 5 * 2 ** Math.max(0, normalizedAttempt - 1));
}

export function scheduleForCron(cron: string): ScheduleName | null {
  for (const [name, expression] of Object.entries(SCHEDULE_CRONS)) {
    if (expression === cron) return name as ScheduleName;
  }
  return null;
}

export function canonicalJson(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("canonical_json_non_finite_number");
    if (Object.is(value, -0)) return "0";
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
  }
  throw new TypeError("canonical_json_unsupported_value");
}

export async function regenerationInputDigest(
  profileId: string,
  sourceManifestSha256: string,
  settingsRevision: number,
): Promise<string> {
  if (!/^[0-9a-f]{64}$/.test(sourceManifestSha256)
    || !Number.isSafeInteger(settingsRevision)
    || settingsRevision < 1) {
    throw new TypeError("invalid_regeneration_input");
  }
  const bytes = new TextEncoder().encode(canonicalJson({
    contract: "settings-regeneration-input",
    profile_id: profileId,
    settings_revision: settingsRevision,
    source_manifest_sha256: sourceManifestSha256,
  }));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function makeIdempotencyKey(profileId: string, inputDigest: string): string {
  return `job:${profileId}:${inputDigest}`;
}

export function makeScheduleEventId(
  schedule: ScheduleName,
  profileId: string,
  scheduledAt: number,
): string {
  return `schedule:${schedule}:${profileId}:${Math.floor(scheduledAt)}`;
}

export function workflowInstanceId(jobId: string, workflowRun: number): string {
  return `job:${jobId}:run:${Math.max(0, Math.floor(workflowRun))}`;
}

export function isTransientError(error: unknown): boolean {
  if (!error || typeof error !== "object") return true;
  const candidate = error as { retryable?: unknown; status?: unknown; code?: unknown };
  if (candidate.retryable === false) return false;
  if (candidate.retryable === true) return true;
  if (typeof candidate.status === "number") return candidate.status >= 500 || candidate.status === 429;
  return true;
}
