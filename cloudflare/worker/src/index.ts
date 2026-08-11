import { Container } from "@cloudflare/containers";
import { WorkflowEntrypoint } from "cloudflare:workers";

export interface Env {
  ENVIRONMENT: string;
  BUILD_VERSION: string;
  PUBLIC_ORIGIN: string;
  CORS_ORIGINS: string;
  ORIGIN_ALLOWLIST: string;
  CONTROL_DB: D1Database;
  PROFILE_DO: DurableObjectNamespace;
  PYTHON_PROCESSOR: DurableObjectNamespace;
  RAW_BUCKET: R2Bucket;
  ARTIFACT_BUCKET: R2Bucket;
  BACKUP_BUCKET: R2Bucket;
  JOB_QUEUE: Queue;
  GENERATE_WORKFLOW: Workflow;
  ASSETS?: Fetcher;
  AUTH_EMAIL?: SendEmail;
  AUTH_EMAIL_FROM?: string;
  EMAIL_ENCRYPTION_KEY?: string;
  DEVICE_TOKEN_PEPPER?: string;
}

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value) + "\n", { status, headers: JSON_HEADERS });
}

export class PythonProcessor extends Container {
  defaultPort = 8080;
  sleepAfter = "10m";
  enableInternet = false;
}

export class GenerateWorkflow extends WorkflowEntrypoint<Env, { job_id: string }> {
  async run(
    event: { payload: { job_id: string } },
    step: { do<T>(name: string, callback: () => Promise<T>): Promise<T> },
  ): Promise<{ status: string; job_id: string }> {
    return step.do("foundation-health-check", async () => ({
      status: "foundation-ready",
      job_id: event.payload.job_id,
    }));
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") {
      return json({ status: "ok", environment: env.ENVIRONMENT });
    }
    if (url.pathname === "/version" && request.method === "GET") {
      return json({ service: "oraja-training", environment: env.ENVIRONMENT, version: env.BUILD_VERSION });
    }
    if (env.ASSETS) return env.ASSETS.fetch(request);
    return json({ error: { code: "not_found" } }, 404);
  },
};
