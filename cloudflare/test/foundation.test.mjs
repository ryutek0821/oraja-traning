import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);

function run(script, ...args) {
  return spawnSync(process.execPath, [fileURLToPath(new URL(`scripts/${script}`, root)), ...args], {
    cwd: fileURLToPath(root),
    encoding: "utf8",
  });
}

test("configuration, migrations, and contract catalog pass their checks", () => {
  for (const script of ["check-config.mjs", "migrations.mjs", "schema-check.mjs"]) {
    const result = run(script, ...(script === "migrations.mjs" ? ["check"] : []));
    assert.equal(result.status, 0, `${script}: ${result.stderr}`);
  }
});

test("migration plans enumerate every isolated environment", () => {
  const result = run("migrations.mjs", "plan");
  assert.equal(result.status, 0, result.stderr);
  for (const environment of ["preview", "staging", "production"]) {
    assert.match(result.stdout, new RegExp(`--env ${environment}`));
  }
});

test("production deploy is denied without the explicit approval flag", () => {
  const result = run("guarded-deploy.mjs", "production");
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}\n${result.stderr}`, /ORAJA_PRODUCTION_DEPLOY_APPROVED/);
});

test("Worker and container expose health and build version metadata", async () => {
  const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");
  const container = await readFile(new URL("container/health.py", root), "utf8");
  assert.match(worker, /url\.pathname === "\/healthz"/);
  assert.match(worker, /url\.pathname === "\/version"/);
  assert.match(worker, /env\.BUILD_VERSION/);
  assert.match(container, /"\/healthz"/);
  assert.match(container, /"\/version"/);
  assert.match(container, /BUILD_VERSION/);
});

test("Worker routes every implemented service boundary", async () => {
  const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");
  for (const route of [
    "handleOAuthRoutes",
    "handleMcp",
    "handleAuth",
    "handleDeviceRoutes",
    "handlePlayRoute",
    "handleUploadRoute",
    "handleCapabilityTable",
    "handleAdvisorRoutes",
    "handlePrivacyRoutes",
  ]) {
    assert.match(worker, new RegExp(`(?:await |return )${route}\\(`));
  }
});
