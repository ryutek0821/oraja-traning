import { pathToFileURL } from "node:url";

const sha256 = /^[a-f0-9]{64}$/;
const revision = /^[a-f0-9]{40}$/;
const evidence = /^[A-Za-z0-9][A-Za-z0-9._:/#-]{7,159}$/;

export function validateReleaseGate(environment, env = process.env) {
  if (!new Set(["staging", "production"]).has(environment)) {
    throw new Error("release evidence gate supports staging and production only");
  }

  const required = {
    ORAJA_CHANGE_ISSUE: /^#[1-9][0-9]*$/,
    ORAJA_BACKUP_EVIDENCE: evidence,
    ORAJA_RESTORE_DRILL_EVIDENCE: evidence,
    ORAJA_ROLLBACK_VERSION: revision,
    ORAJA_RELEASE_MANIFEST_SHA256: sha256,
    ORAJA_MIGRATION_PLAN_SHA256: sha256,
  };
  for (const [name, pattern] of Object.entries(required)) {
    const value = env[name] ?? "";
    if (!pattern.test(value)) throw new Error(`missing or invalid release evidence: ${name}`);
  }
  if (env.ORAJA_GO_NO_GO !== "go") {
    throw new Error("release is held: ORAJA_GO_NO_GO must be exactly go");
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  validateReleaseGate(process.argv[2]);
  console.log("Release evidence gate passed without printing evidence values");
}
