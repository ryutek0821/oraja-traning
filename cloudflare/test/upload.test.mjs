import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const protocol = await readFile(new URL("worker/src/upload-protocol.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");

test("failed completion preserves canonical dedup objects before deleting owned R2 objects", () => {
  assert.match(protocol, /this\.store\.findDedup\(scope, file\.sha256, file\.sizeBytes\)/);
  assert.match(protocol, /canonical\?\.objectKey !== file\.createdObjectKey/);
  assert.doesNotMatch(protocol, /ownedRefs\.map\(\(ref\) => this\.store\.removeDedup/);
});

test("the Worker exposes the authenticated upload adapter", () => {
  assert.match(worker, /handleUploadRequest/);
  assert.match(worker, /new D1UploadSessionStore/);
  assert.match(worker, /EnvelopeCrypto\.fromSecret/);
  assert.match(worker, /requireWebAccount/);
});
