import { readdir, readFile } from "node:fs/promises";
import { isDeepStrictEqual } from "node:util";

const contractsDirectory = new URL("../../docs/contracts/", import.meta.url);
const examplesDirectory = new URL("../../docs/contracts/examples/", import.meta.url);

async function readJson(url) {
  try {
    return JSON.parse(await readFile(url, "utf8"));
  } catch (error) {
    throw new Error(`invalid JSON: ${url.pathname}`, { cause: error });
  }
}

function pointer(document, fragment, reference) {
  if (fragment === "") return document;
  if (!fragment.startsWith("/")) throw new Error(`unsupported schema reference: ${reference}`);
  let value = document;
  for (const encodedPart of fragment.slice(1).split("/")) {
    const part = decodeURIComponent(encodedPart).replaceAll("~1", "/").replaceAll("~0", "~");
    if (value === null || typeof value !== "object" || !(part in value)) {
      throw new Error(`unresolved schema reference: ${reference}`);
    }
    value = value[part];
  }
  return value;
}

function typeMatches(value, expected) {
  if (expected === "null") return value === null;
  if (expected === "array") return Array.isArray(value);
  if (expected === "object") return value !== null && typeof value === "object" && !Array.isArray(value);
  if (expected === "integer") return Number.isInteger(value);
  if (expected === "number") return typeof value === "number" && Number.isFinite(value);
  return typeof value === expected;
}

const contractFiles = (await readdir(contractsDirectory)).filter((file) => file.endsWith(".json")).sort();
const exampleFiles = (await readdir(examplesDirectory)).filter((file) => file.endsWith(".json")).sort();
if (contractFiles.length === 0 || exampleFiles.length === 0) throw new Error("contract catalog is empty");

const schemasByFile = new Map();
const schemasById = new Map();
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
    schemasByFile.set(file, document);
    schemasById.set(document.$id, document);
  }
}

function resolveReference(reference, currentDocument) {
  const separator = reference.indexOf("#");
  const uri = separator === -1 ? reference : reference.slice(0, separator);
  const fragment = separator === -1 ? "" : reference.slice(separator + 1);
  let document = currentDocument;
  if (uri !== "") {
    const file = uri.split("/").at(-1);
    document = schemasById.get(uri) ?? schemasByFile.get(file);
    if (document === undefined) throw new Error(`unresolved schema reference: ${reference}`);
  }
  return { document, schema: pointer(document, fragment, reference) };
}

function validate(schema, value, document, path = "$") {
  if (schema === true) return [];
  if (schema === false) return [`${path}: rejected by false schema`];
  const errors = [];

  if (schema.$ref !== undefined) {
    const resolved = resolveReference(schema.$ref, document);
    errors.push(...validate(resolved.schema, value, resolved.document, path));
  }
  if (schema.allOf !== undefined) {
    for (const branch of schema.allOf) errors.push(...validate(branch, value, document, path));
  }
  if (schema.anyOf !== undefined && !schema.anyOf.some((branch) => validate(branch, value, document, path).length === 0)) {
    errors.push(`${path}: no anyOf branch matched`);
  }
  if (schema.if !== undefined) {
    const conditionMatches = validate(schema.if, value, document, path).length === 0;
    if (conditionMatches && schema.then !== undefined) errors.push(...validate(schema.then, value, document, path));
    if (!conditionMatches && schema.else !== undefined) errors.push(...validate(schema.else, value, document, path));
  }
  if (schema.not !== undefined && validate(schema.not, value, document, path).length === 0) {
    errors.push(`${path}: not-schema matched`);
  }

  const expectedTypes = schema.type === undefined
    ? []
    : (Array.isArray(schema.type) ? schema.type : [schema.type]);
  if (expectedTypes.length > 0 && !expectedTypes.some((expected) => typeMatches(value, expected))) {
    errors.push(`${path}: expected ${expectedTypes.join(" or ")}`);
    return errors;
  }
  if (schema.const !== undefined && !isDeepStrictEqual(value, schema.const)) {
    errors.push(`${path}: const mismatch`);
  }
  if (schema.enum !== undefined && !schema.enum.some((candidate) => isDeepStrictEqual(value, candidate))) {
    errors.push(`${path}: value is not in enum`);
  }

  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const key of schema.required ?? []) {
      if (!(key in value)) errors.push(`${path}: missing required property ${key}`);
    }
    const properties = schema.properties ?? {};
    for (const [key, childSchema] of Object.entries(properties)) {
      if (key in value) errors.push(...validate(childSchema, value[key], document, `${path}.${key}`));
    }
    const unexpected = Object.keys(value).filter((key) => !(key in properties));
    if (schema.additionalProperties === false) {
      for (const key of unexpected) errors.push(`${path}: unexpected property ${key}`);
    } else if (schema.additionalProperties !== undefined && typeof schema.additionalProperties === "object") {
      for (const key of unexpected) {
        errors.push(...validate(schema.additionalProperties, value[key], document, `${path}.${key}`));
      }
    }
  }

  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(`${path}: fewer than minItems`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${path}: more than maxItems`);
    if (schema.uniqueItems === true) {
      for (let index = 0; index < value.length; index += 1) {
        if (value.slice(0, index).some((candidate) => isDeepStrictEqual(candidate, value[index]))) {
          errors.push(`${path}: array items are not unique`);
          break;
        }
      }
    }
    if (schema.items !== undefined) {
      value.forEach((item, index) => errors.push(...validate(schema.items, item, document, `${path}[${index}]`)));
    }
    if (schema.contains !== undefined && !value.some((item) => validate(schema.contains, item, document, path).length === 0)) {
      errors.push(`${path}: contains condition did not match`);
    }
  }

  if (typeof value === "string") {
    if (schema.minLength !== undefined && [...value].length < schema.minLength) errors.push(`${path}: shorter than minLength`);
    if (schema.maxLength !== undefined && [...value].length > schema.maxLength) errors.push(`${path}: longer than maxLength`);
    if (schema.pattern !== undefined && !new RegExp(schema.pattern, "u").test(value)) errors.push(`${path}: pattern mismatch`);
    if (schema.format === "date-time") {
      const timestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u;
      if (!timestamp.test(value) || Number.isNaN(Date.parse(value))) errors.push(`${path}: invalid date-time`);
    }
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${path}: less than minimum`);
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${path}: greater than maximum`);
  }

  return errors;
}

const fixtureSets = [
  ["ir-submission.v1.schema.json", ["ir-submission.valid.json"], ["ir-submission.invalid-course.json", "ir-submission.invalid-owner.json"]],
  ["play-event.v1.schema.json", ["play-event.valid.json", "play-event.valid-backfill.json"], ["play-event.invalid-backfill-provenance.json"]],
  ["aggregate-eligibility.v1.schema.json", ["aggregate-eligibility.valid.json"], ["aggregate-eligibility.invalid-boolean.json"]],
  ["upload-manifest.v1.schema.json", ["upload-manifest.valid.json"], ["upload-manifest.invalid-sidecar.json"]],
  ["container-input-manifest.v1.schema.json", ["container-input.valid.json"], ["container-input.invalid-self-hosted-aggregate.json"]],
  ["container-output-manifest.v1.schema.json", ["container-output.valid.json"], ["container-output.invalid-self-hosted-aggregate.json"]],
  ["artifact-manifest.v1.schema.json", ["artifact-manifest.valid.json"], ["artifact-manifest.invalid-revision.json"]],
];

const mappedExamples = new Set(fixtureSets.flatMap(([, valid, invalid]) => [...valid, ...invalid]));
for (const file of exampleFiles) {
  if (!mappedExamples.has(file)) throw new Error(`schema fixture is not mapped: ${file}`);
}
for (const file of mappedExamples) {
  if (!exampleFiles.includes(file)) throw new Error(`missing schema fixture: ${file}`);
}

let validCount = 0;
let invalidCount = 0;
for (const [schemaFile, validFiles, invalidFiles] of fixtureSets) {
  const schema = schemasByFile.get(schemaFile);
  if (schema === undefined) throw new Error(`missing contract schema: ${schemaFile}`);
  for (const file of validFiles) {
    const example = await readJson(new URL(file, examplesDirectory));
    const errors = validate(schema, example, schema);
    if (errors.length > 0) throw new Error(`${file}: schema validation failed:\n${errors.join("\n")}`);
    validCount += 1;
  }
  for (const file of invalidFiles) {
    const example = await readJson(new URL(file, examplesDirectory));
    const errors = validate(schema, example, schema);
    if (errors.length === 0) throw new Error(`${file}: invalid fixture unexpectedly satisfies ${schemaFile}`);
    invalidCount += 1;
  }
}

console.log(`Contract JSON OK: ${contractFiles.length} definitions, ${validCount} valid fixtures, ${invalidCount} invalid fixtures rejected`);
