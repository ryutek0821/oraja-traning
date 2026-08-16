import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);

function run(script, ...args) {
  return spawnSync(process.execPath, [fileURLToPath(new URL(`scripts/${script}`, root)), ...args], {
    cwd: fileURLToPath(root),
    encoding: "utf8",
  });
}

test("configuration and migrations pass their checks", () => {
  for (const script of ["check-config.mjs", "migrations.mjs"]) {
    const result = run(script, ...(script === "migrations.mjs" ? ["check"] : []));
    assert.equal(result.status, 0, `${script}: ${result.stderr}`);
  }
});

test("schema checker validates positive examples and rejects negative fixtures", () => {
  const result = run("schema-check.mjs");
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /8 valid fixtures, 8 invalid fixtures rejected/);
});

test("public scan rejects a tracked database", async () => {
  const directory = await mkdtemp(join(tmpdir(), "oraja-public-scan-"));
  try {
    const initialized = spawnSync("git", ["init", "--quiet"], { cwd: directory, encoding: "utf8" });
    assert.equal(initialized.status, 0, initialized.stderr);
    await writeFile(join(directory, "private.db"), "private score data");
    const added = spawnSync("git", ["add", "private.db"], { cwd: directory, encoding: "utf8" });
    assert.equal(added.status, 0, added.stderr);

    const result = spawnSync(process.execPath, [fileURLToPath(new URL("scripts/public-scan.mjs", root))], {
      cwd: directory,
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(`${result.stdout}\n${result.stderr}`, /private\.db: tracked database files are forbidden/);
  } finally {
    await rm(directory, { recursive: true, force: true });
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
    "handleJobRoutes",
    "handleCapabilityTable",
    "handleAdvisorRoutes",
    "handlePrivacyRoutes",
  ]) {
    assert.match(worker, new RegExp(`(?:await |return )${route}\\(`));
  }
});
