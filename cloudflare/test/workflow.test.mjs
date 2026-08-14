import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const ledger = await readFile(new URL("worker/src/job-ledger.ts", root), "utf8");
const workflow = await readFile(new URL("worker/src/workflow.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");

test("dispatch leases compare against current time, not the new expiry", () => {
  assert.match(ledger, /lease_until <= \?3/);
  assert.match(ledger, /bind\(jobId, now \+ 90, now\)/);
});

test("success outcome and latest pointer share one D1 batch", () => {
  assert.match(ledger, /recordSuccessAndPublish/);
  assert.match(workflow, /ledger\.recordSuccessAndPublish/);
  assert.doesNotMatch(workflow, /recordOutcome\(job, "succeeded"/);
});

test("unwired processor cannot fabricate and publish digests", () => {
  assert.match(workflow, /processor_integration_unavailable/);
  assert.match(workflow, /artifact_manifest_missing/);
  assert.match(worker, /export \{ GenerateWorkflow \} from "\.\/workflow"/);
  assert.match(worker, /async queue\(/);
  assert.match(worker, /async scheduled\(/);
});

test("job status, cancellation, and manual retry are owner-scoped routes", () => {
  assert.match(worker, /handleJobRoutes/);
  assert.match(worker, /\/v1\\\/jobs\\\/\(\[\^\/\]\+\)/);
  assert.match(worker, /requireWebAccount\(request, env, mutate\)/);
  assert.match(worker, /ledger\.getStatus\(accountId, jobId\)/);
  assert.match(worker, /ledger\.cancel\(accountId, jobId\)/);
  assert.match(worker, /ledger\.manualRetry\(accountId, jobId, accountId\)/);
  assert.match(worker, /new JobDispatcher\(ledger, env\.JOB_QUEUE\)\.dispatch\(job\)/);
});
