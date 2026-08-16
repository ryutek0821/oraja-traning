import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const bridge = await readFile(new URL("worker/src/container-bridge.ts", root), "utf8");
const upload = await readFile(new URL("worker/src/upload-protocol.ts", root), "utf8");
const store = await readFile(new URL("worker/src/upload-store.ts", root), "utf8");
const entrypoint = await readFile(new URL("container/entrypoint.py", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");

test("bridge resolves an owner-scoped completed upload and verifies every encrypted frame", () => {
  assert.match(bridge, /upload-session:/);
  assert.match(bridge, /profileScope\(job\.profileId\)/);
  assert.match(bridge, /expectedInputDigest !== job\.inputDigest/);
  assert.match(bridge, /regenerationInputDigest\(job\.profileId, session\.manifestSha256, tableContext\.settingsRevision\)/);
  assert.match(bridge, /decryptCompletedUploadObject/);
  assert.match(upload, /partHasher\.digest\(\) !== part\.sha256/);
  assert.match(upload, /totalHasher\.digest\(\) !== file\.sha256/);
  assert.match(store, /getCompletedSession/);
  assert.match(store, /object_key = \?1 AND encryption_json IS NOT NULL/);
  assert.match(upload, /onCompleted \? await onCompleted\(context, completed\)/);
});

test("framed transport is bounded and the container rechecks the exact five files", () => {
  assert.match(bridge, /five-db-framed-v2/);
  assert.match(bridge, /content-length/);
  assert.match(bridge, /MAX_TABLE_CONTEXT_BYTES/);
  assert.match(entrypoint, /FRAMED_MAGIC_V2 = b"ORAJA5DB2/);
  assert.match(entrypoint, /table-context\.json/);
  assert.match(entrypoint, /seen != DATABASE_NAMES/);
  assert.match(entrypoint, /source\.remaining != 0/);
  assert.match(entrypoint, /hasher\.hexdigest\(\) != digest/);
});

test("Worker validates output ownership and digest before immutable R2 publication", () => {
  assert.match(bridge, /manifest\.profile_id !== job\.profileId/);
  assert.match(bridge, /calculatedManifest !== payload\.output_manifest_sha256/);
  assert.match(bridge, /artifact\.object_key\.startsWith\(prefix\)/);
  assert.match(bridge, /artifact_immutable_conflict/);
  assert.match(bridge, /await immutablePut\(env\.ARTIFACT_BUCKET, artifactKey/);
  assert.match(bridge, /MAX_CONTAINER_RESPONSE_BYTES/);
  assert.match(bridge, /boundedContainerResponse/);
  assert.match(bridge, /tableArtifacts\.length !== 4/);
});

test("completed uploads enqueue one immutable owner-scoped workflow input", () => {
  assert.match(upload, /submissionKind: SubmissionKind/);
  assert.match(worker, /eventId: `upload:\$\{completed\.uploadId\}:\$\{completed\.manifestSha256\}`/);
  assert.match(worker, /inputKey: `upload-session:\$\{completed\.uploadId\}`/);
  assert.match(worker, /inputDigest: completed\.manifestSha256/);
  assert.match(worker, /accountId: context\.accountId/);
});

test("scheduled and play jobs resolve durable inputs and preserve their job kind", () => {
  assert.match(bridge, /ORDER BY completed_at DESC, created_at DESC LIMIT 1/);
  assert.match(bridge, /internal\/play-events/);
  assert.match(bridge, /x-container-job-kind/);
  assert.match(bridge, /x-container-play-event/);
  assert.match(bridge, /MAX_PLAY_EVENT_BYTES/);
});
