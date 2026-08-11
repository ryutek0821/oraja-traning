import { spawnSync } from "node:child_process";

const environment = process.argv[2];
if (environment !== "production") throw new Error("only the production guard is supported");
const approval = process.env.ORAJA_PRODUCTION_DEPLOY_APPROVED
  ?? process.env.ORaja_PRODUCTION_DEPLOY_APPROVED;
if (approval !== "true") {
  throw new Error("set ORAJA_PRODUCTION_DEPLOY_APPROVED=true in an approved environment to deploy production");
}
for (const name of ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]) {
  if (!process.env[name]) throw new Error(`missing protected deployment secret: ${name}`);
}
const buildVersion = process.env.BUILD_VERSION ?? "production-local";
if (!/^[A-Za-z0-9._-]+$/.test(buildVersion)) throw new Error("BUILD_VERSION contains unsupported characters");
const result = spawnSync(
  "wrangler",
  ["deploy", "--env", environment, "--var", `BUILD_VERSION:${buildVersion}`],
  { stdio: "inherit" },
);
process.exit(result.status ?? 1);
