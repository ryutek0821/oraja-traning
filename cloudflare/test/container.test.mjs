import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

test("Container image is pinned, non-root, and runs the shared-core entrypoint", async () => {
  const dockerfile = await readFile(new URL("container/Dockerfile", root), "utf8");
  assert.match(dockerfile, /FROM python:3\.11-slim@sha256:[0-9a-f]{64}/);
  assert.match(dockerfile, /USER processor/);
  assert.ok(dockerfile.includes('ENTRYPOINT ["python", "/app/container/entrypoint.py"]'));
  assert.match(dockerfile, /COPY src \/app\/src/);
});

test("Container entrypoint keeps the streamed manifest boundary and health routes", async () => {
  const entrypoint = await readFile(new URL("container/entrypoint.py", root), "utf8");
  const health = await readFile(new URL("container/health.py", root), "utf8");
  assert.match(entrypoint, /X-Container-Input-Manifest/);
  assert.match(entrypoint, /X-Container-Job-Kind/);
  assert.match(entrypoint, /X-Container-Play-Event/);
  assert.match(entrypoint, /self\.adapter\.run_job/);
  assert.match(entrypoint, /self\.path != "\/v1\/jobs"/);
  assert.match(entrypoint, /ORAJA_ALLOW_PLAINTEXT_FIXTURE/);
  assert.match(entrypoint, /class _BoundedBody/);
  assert.match(entrypoint, /content_length != expected_size/);
  assert.match(health, /"\/healthz"/);
  assert.match(health, /"\/readyz"/);
  assert.match(health, /"\/version"/);
  assert.match(entrypoint, /_readiness_probe/);
  assert.doesNotMatch(health, /ready = True/);
});
