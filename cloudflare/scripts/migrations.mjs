import { readFile, readdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";

const migrationsDirectory = new URL("../migrations/", import.meta.url);
const directoryEntries = await readdir(migrationsDirectory).catch((error) => {
  if (error?.code === "ENOENT") return [];
  throw error;
});
const files = directoryEntries
  .filter((file) => file.endsWith(".sql"))
  .sort();
const names = files.map((file) => file.split("_", 1)[0]);
if (new Set(names).size !== names.length || names.some((name) => !/^\d{4}$/.test(name))) {
  throw new Error("migration files must have unique four-digit prefixes");
}
if (files.length > 0 && names.some((name, index) => Number(name) !== index)) {
  throw new Error("migration files must start at 0000 and have no gaps");
}
for (const file of files) {
  if (!(await readFile(new URL(`../migrations/${file}`, import.meta.url), "utf8")).trim()) {
    throw new Error(`migration file is empty: ${file}`);
  }
}

const wranglerSource = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
const wranglerConfig = JSON.parse(
  wranglerSource
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/.*$/gm, "$1")
    .replace(/,\s*([}\]])/g, "$1"),
);

const command = process.argv[2] ?? "check";
const environments = ["preview", "staging", "production"];

function assertEnvironment(environment) {
  if (!environments.includes(environment)) {
    throw new Error(`unknown environment: ${environment ?? "<missing>"}`);
  }
}

function printForwardPlan() {
  console.log("Forward plan (review before applying):");
  for (const environment of environments) {
    console.log(`  npx wrangler d1 migrations apply CONTROL_DB --env ${environment} --remote`);
  }
}

if (command === "check") {
  console.log(`Migration order OK: ${files.join(", ")}`);
} else if (command === "plan") {
  printForwardPlan();
  console.log("Rollback plan: stop writes, restore the approved backup, then deploy the previous Worker version.");
} else if (command === "apply") {
  const environment = process.argv[3];
  assertEnvironment(environment);
  if (!process.argv.includes("--confirm")) {
    throw new Error("migration apply requires --confirm; use npm run migration:plan first");
  }
  const approval = process.env.ORAJA_MIGRATIONS_APPLY_APPROVED
    ?? process.env.ORaja_MIGRATIONS_APPLY_APPROVED;
  if (approval !== "true") {
    throw new Error("set ORAJA_MIGRATIONS_APPLY_APPROVED=true in the approved environment to apply migrations");
  }
  const result = spawnSync(
    "wrangler",
    ["d1", "migrations", "apply", "CONTROL_DB", "--env", environment, "--remote"],
    { stdio: "inherit" },
  );
  process.exit(result.status ?? 1);
} else if (command === "rollback") {
  const environment = process.argv[3];
  assertEnvironment(environment);
  console.log(`Rollback plan for ${environment}:`);
  console.log("  1. Stop writes and keep /healthz and /version available.");
  console.log("  2. Restore the approved environment backup after recording its ID.");
  console.log("  3. Deploy the previously approved Worker revision through its environment gate.");
  console.log("  4. Verify migration state and read-only endpoints before reopening writes.");
  console.log("  Never edit an applied SQL file; add a forward repair migration instead.");
} else if (command === "preview-cleanup") {
  const preview = wranglerConfig.env.preview;
  console.log("Review-only preview cleanup plan:");
  console.log(`  Worker: ${preview.name}`);
  console.log(`  route: ${preview.routes.map((route) => route.pattern).join(", ")}`);
  console.log(`  D1: ${preview.d1_databases.map((database) => database.database_name).join(", ")}`);
  console.log(`  R2: ${preview.r2_buckets.map((bucket) => bucket.bucket_name).join(", ")}`);
  console.log(`  Queue: ${preview.queues.producers.map((queue) => queue.queue).join(", ")}`);
  console.log(`  Queue DLQ: ${preview.queues.consumers.map((queue) => queue.dead_letter_queue).join(", ")}`);
  console.log(`  Workflow: ${preview.workflows.map((workflow) => workflow.name).join(", ")}`);
  console.log(`  Container: ${preview.containers.map((container) => container.name).join(", ")}`);
  console.log("  verify the Worker revision and migration state before cleanup");
  console.log("  obtain operator approval before deleting D1/DO/R2/Queue resources");
  console.log("  delete only the exact approved resource names; never use a wildcard");
} else {
  throw new Error(`unknown migration command: ${command}`);
}
