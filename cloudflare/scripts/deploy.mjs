import { spawnSync } from "node:child_process";
import { validateReleaseGate } from "./release-gate.mjs";

const environment = process.argv[2];
const dryRun = process.argv.slice(3).includes("--dry-run");
const environments = new Set(["preview", "staging"]);

if (!environments.has(environment)) {
  throw new Error("deploy.mjs only handles preview and staging; production uses guarded-deploy.mjs");
}

const buildVersion = process.env.BUILD_VERSION ?? `${environment}-local`;
if (!/^[A-Za-z0-9._-]+$/.test(buildVersion)) {
  throw new Error("BUILD_VERSION contains unsupported characters");
}

const args = ["deploy", "--env", environment, "--var", `BUILD_VERSION:${buildVersion}`];
if (dryRun) args.push("--dry-run");

if (environment === "staging" && !dryRun) validateReleaseGate(environment);

const result = spawnSync("wrangler", args, { stdio: "inherit" });
process.exit(result.status ?? 1);
