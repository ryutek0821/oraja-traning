import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { Sha256, sha256Blob } from "../web/public/sha256.js";

const root = new URL("../", import.meta.url);

test("incremental browser SHA-256 matches standard vectors across chunk boundaries", async () => {
  for (const bytes of [new Uint8Array(), new TextEncoder().encode("abc"), randomBytes(131_173)]) {
    const expected = createHash("sha256").update(bytes).digest("hex");
    const incremental = new Sha256();
    for (let offset = 0; offset < bytes.length; offset += 997) incremental.update(bytes.subarray(offset, offset + 997));
    assert.equal(incremental.digestHex(), expected);
    assert.equal(await sha256Blob(new Blob([bytes]), 4093), expected);
  }
});

test("dashboard performs resumable owner-session multipart upload without persisting secrets", async () => {
  const source = await readFile(new URL("web/public/app.js", root), "utf8");
  for (const boundary of ["sha256Blob", 'method:"PUT"', "x-part-sha256", "uploadedParts", "/complete", "pollJob"]) {
    assert.match(source, new RegExp(boundary.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
  assert.match(source, /localStorage\.removeItem\(key\)/);
  assert.doesNotMatch(source, /localStorage\.setItem\([^\n]*(?:token|csrf|capability)/i);
  const uploadFlow = source.slice(source.indexOf("async function submitFiveDb"), source.indexOf("function render"));
  assert.doesNotMatch(uploadFlow, /profile_id\s*:/);
});
