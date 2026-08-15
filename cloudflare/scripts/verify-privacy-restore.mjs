import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { webcrypto } from "node:crypto";

const subtle = webcrypto.subtle;
const encoder = new TextEncoder();

function decode(value, length) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url metadata");
  const bytes = Buffer.from(value, "base64url");
  if (bytes.byteLength !== length) throw new Error("invalid metadata length");
  return bytes;
}

async function sha256(value) {
  return Buffer.from(await subtle.digest("SHA-256", value)).toString("hex");
}

function assertPortable(value, path = "archive") {
  if (Array.isArray(value)) return value.forEach((item, index) => assertPortable(item, `${path}[${index}]`));
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    if (["account_id", "profile_id", "object_key", "artifact_key", "input_key", "target_key"].includes(key)) {
      throw new Error(`non-portable owner/storage identifier at ${path}.${key}`);
    }
    assertPortable(item, `${path}.${key}`);
  }
}

export async function decryptAndVerify(ciphertext, metadata, userKey) {
  if (metadata.schema !== "oraja.profile-export.v2" || metadata.cipher !== "AES-256-GCM") {
    throw new Error("unsupported export format");
  }
  if (await sha256(ciphertext) !== metadata.ciphertext_sha256) throw new Error("ciphertext digest mismatch");
  const key = await subtle.importKey("raw", decode(userKey, 32), { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const plaintext = Buffer.from(await subtle.decrypt({
    name: "AES-GCM",
    iv: decode(metadata.iv, 12),
    additionalData: encoder.encode(`oraja.profile-export.v2\0${metadata.export_id}`),
    tagLength: 128,
  }, key, ciphertext));
  if (await sha256(plaintext) !== metadata.plaintext_sha256) throw new Error("plaintext digest mismatch");
  const archive = JSON.parse(plaintext.toString("utf8"));
  if (archive.contract !== metadata.schema || archive.schema_version !== "2" || archive.portable_owner !== "profile:primary") {
    throw new Error("archive schema mismatch");
  }
  assertPortable(archive);
  for (const name of ["history", "tables", "journal"]) {
    const section = archive.sections?.[name];
    const expected = archive.verification?.section_sha256?.[name];
    if (!section || typeof expected !== "string" || await sha256(encoder.encode(JSON.stringify(section))) !== expected) {
      throw new Error(`${name} section digest mismatch`);
    }
  }
  return archive;
}

export async function importIntoEmptyIsolatedState(archive, destination = new Map()) {
  if (destination.size !== 0) throw new Error("restore destination must be empty");
  for (const [name, section] of Object.entries(archive.sections)) {
    destination.set(name, structuredClone(section));
  }
  const comparison = {};
  for (const name of ["history", "tables", "journal"]) {
    comparison[name] = await sha256(encoder.encode(JSON.stringify(destination.get(name))));
    if (comparison[name] !== archive.verification.section_sha256[name]) throw new Error(`${name} restore comparison failed`);
  }
  return { destination, comparison };
}

async function main() {
  const [archivePath, metadataPath] = process.argv.slice(2);
  if (!archivePath || !metadataPath || !process.env.ORAJA_EXPORT_KEY) {
    throw new Error("usage: ORAJA_EXPORT_KEY=<one-time-key> node scripts/verify-privacy-restore.mjs <archive.oraenc> <metadata.json>");
  }
  const [ciphertext, metadataText] = await Promise.all([readFile(archivePath), readFile(metadataPath, "utf8")]);
  const archive = await decryptAndVerify(ciphertext, JSON.parse(metadataText), process.env.ORAJA_EXPORT_KEY);
  const restored = await importIntoEmptyIsolatedState(archive);
  process.stdout.write(JSON.stringify({ verified: true, schema: archive.contract, section_sha256: restored.comparison }) + "\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
}
