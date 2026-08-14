import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const protocol = await readFile(new URL("worker/src/upload-protocol.ts", root), "utf8");
const store = await readFile(new URL("worker/src/upload-store.ts", root), "utf8");
const worker = await readFile(new URL("worker/src/index.ts", root), "utf8");

test("failed completion removes owned dedup references before deleting R2 objects", () => {
  assert.match(protocol, /ownedRefs\.map\(\(ref\) => this\.store\.removeDedup/);
  assert.match(store, /DELETE FROM upload_dedup/);
  assert.match(store, /AND object_key = \?4/);
});

test("the Worker exposes the authenticated upload adapter", () => {
  assert.match(worker, /handleUploadRequest/);
  assert.match(worker, /new D1UploadSessionStore/);
  assert.match(worker, /EnvelopeCrypto\.fromSecret/);
  assert.match(worker, /requireWebAccount/);
});
