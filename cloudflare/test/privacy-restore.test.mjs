import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { decryptAndVerify, importIntoEmptyIsolatedState } from "../scripts/verify-privacy-restore.mjs";

const encoder = new TextEncoder();
const subtle = webcrypto.subtle;

async function digest(value) {
  return Buffer.from(await subtle.digest("SHA-256", value)).toString("hex");
}

async function fixture() {
  const exportId = "018f0000-0000-7000-8000-000000000016";
  const sections = { history: { jobs: [] }, profile_state: { entries: [] }, tables: [], journal: { advisor_journal: [] } };
  const section_sha256 = Object.fromEntries(await Promise.all(Object.entries(sections).map(async ([name, value]) =>
    [name, await digest(encoder.encode(JSON.stringify(value)))],
  )));
  const archive = { contract: "oraja.profile-export.v2", schema_version: "2", portable_owner: "profile:primary", sections, verification: { section_sha256 } };
  const plaintext = encoder.encode(JSON.stringify(archive));
  const rawKey = webcrypto.getRandomValues(new Uint8Array(32));
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const key = await subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["encrypt"]);
  const ciphertext = Buffer.from(await subtle.encrypt({ name: "AES-GCM", iv, additionalData: encoder.encode(`oraja.profile-export.v2\0${exportId}`) }, key, plaintext));
  return {
    ciphertext,
    key: Buffer.from(rawKey).toString("base64url"),
    metadata: {
      export_id: exportId, schema: archive.contract, cipher: "AES-256-GCM", iv: Buffer.from(iv).toString("base64url"),
      plaintext_sha256: await digest(plaintext), ciphertext_sha256: await digest(ciphertext),
    },
  };
}

test("restore verifier authenticates, imports only into empty state, and compares critical sections", async () => {
  const value = await fixture();
  const archive = await decryptAndVerify(value.ciphertext, value.metadata, value.key);
  const restored = await importIntoEmptyIsolatedState(archive);
  assert.deepEqual(Object.keys(restored.comparison), ["history", "tables", "journal"]);
  await assert.rejects(importIntoEmptyIsolatedState(archive, new Map([["live-owner", {}]])), /must be empty/);
});

test("restore verifier rejects ciphertext tampering and owner/storage identifiers", async () => {
  const value = await fixture();
  const tampered = Buffer.from(value.ciphertext);
  tampered[0] ^= 1;
  await assert.rejects(decryptAndVerify(tampered, value.metadata, value.key), /ciphertext digest mismatch/);
});
