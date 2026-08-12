import { readdir, readFile } from "node:fs/promises";

const contractsDirectory = new URL("../../docs/contracts/", import.meta.url);
const examplesDirectory = new URL("../../docs/contracts/examples/", import.meta.url);

async function readJson(url) {
  try {
    return JSON.parse(await readFile(url, "utf8"));
  } catch (error) {
    throw new Error(`invalid JSON: ${url.pathname}`, { cause: error });
  }
}

const contractFiles = (await readdir(contractsDirectory)).filter((file) => file.endsWith(".json")).sort();
const exampleFiles = (await readdir(examplesDirectory)).filter((file) => file.endsWith(".json")).sort();
if (contractFiles.length === 0 || exampleFiles.length === 0) throw new Error("contract catalog is empty");

for (const file of contractFiles) {
  const document = await readJson(new URL(file, contractsDirectory));
  if (file.endsWith(".schema.json")) {
    if (document.$schema !== "https://json-schema.org/draft/2020-12/schema") {
      throw new Error(`${file}: schema draft must be JSON Schema 2020-12`);
    }
    if (typeof document.$id !== "string" || !document.$id.startsWith("https://oraja-training.dev/contracts/")) {
      throw new Error(`${file}: schema must have the canonical contract ID`);
    }
    if (file !== "common.schema.json" && !document.$id.includes("/v1/")) {
      throw new Error(`${file}: versioned schema ID must contain /v1/`);
    }
  }
}

for (const file of exampleFiles) await readJson(new URL(file, examplesDirectory));

const fixturePairs = [
  ["ir-submission.valid.json", "ir-submission.invalid-course.json"],
  ["play-event.valid.json", "play-event.invalid-backfill-provenance.json"],
  ["aggregate-eligibility.valid.json", "aggregate-eligibility.invalid-boolean.json"],
  ["upload-manifest.valid.json", "upload-manifest.invalid-sidecar.json"],
  ["container-input.valid.json", "container-input.invalid-self-hosted-aggregate.json"],
  ["container-output.valid.json", "container-output.invalid-self-hosted-aggregate.json"],
  ["artifact-manifest.valid.json", "artifact-manifest.invalid-revision.json"],
];
for (const [valid, invalid] of fixturePairs) {
  if (!exampleFiles.includes(valid) || !exampleFiles.includes(invalid)) {
    throw new Error(`missing schema fixture pair: ${valid}, ${invalid}`);
  }
}

console.log(`Contract JSON OK: ${contractFiles.length} definitions, ${exampleFiles.length} fixtures`);
