import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
const withoutComments = source
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/(^|\s)\/\/.*$/gm, "$1")
  .replace(/,\s*([}\]])/g, "$1");
const config = JSON.parse(withoutComments);
const environments = ["preview", "staging", "production"];
const requiredBindings = [
  "CONTROL_DB", "PROFILE_DO", "PYTHON_PROCESSOR", "RAW_BUCKET",
  "ARTIFACT_BUCKET", "BACKUP_BUCKET", "JOB_QUEUE", "GENERATE_WORKFLOW",
];

const bindingNames = (environment) => new Set([
  ...(environment.durable_objects?.bindings ?? []).map((item) => item.name),
  ...(environment.r2_buckets ?? []).map((item) => item.binding),
  ...(environment.queues?.producers ?? []).map((item) => item.binding),
  ...(environment.workflows ?? []).map((item) => item.binding),
  ...(environment.d1_databases ?? []).map((item) => item.binding),
]);

for (const name of environments) {
  const environment = config.env?.[name];
  if (!environment) throw new Error(`missing Wrangler environment: ${name}`);
  if (!environment.name || !environment.name.includes(`-${name}`)) {
    throw new Error(`${name}: Worker name is not environment-scoped`);
  }
  if (environment.workers_dev !== false) {
    throw new Error(`${name}: workers_dev must be disabled; use the environment route`);
  }
  if (!Array.isArray(environment.routes) || environment.routes.length !== 1) {
    throw new Error(`${name}: exactly one custom-domain route is required`);
  }
  const route = environment.routes[0];
  if (route.custom_domain !== true || !route.pattern || route.pattern.includes("/*")) {
    throw new Error(`${name}: route must be a custom-domain hostname`);
  }
  if (!Array.isArray(environment.send_email) || environment.send_email.length !== 1) {
    throw new Error(`${name}: send_email binding must be declared per environment`);
  }
  if (environment.vars?.ENVIRONMENT !== name) {
    throw new Error(`${name}: ENVIRONMENT var does not match environment name`);
  }
  if (!environment.vars?.BUILD_VERSION) throw new Error(`${name}: missing BUILD_VERSION`);
  if (!environment.vars?.CORS_ORIGINS || !environment.vars?.ORIGIN_ALLOWLIST) {
    throw new Error(`${name}: missing origin policy vars`);
  }
  const names = bindingNames(environment);
  for (const binding of requiredBindings) {
    if (!names.has(binding)) throw new Error(`${name}: missing binding ${binding}`);
  }
  const bucketNames = (environment.r2_buckets ?? []).map((item) => item.bucket_name);
  if (bucketNames.some((bucket) => !bucket.includes(`-${name}-`))) {
    throw new Error(`${name}: R2 bucket is not environment-scoped`);
  }
  const queueNames = (environment.queues?.producers ?? []).map((item) => item.queue);
  if (queueNames.some((queue) => !queue.includes(`-${name}-`))) {
    throw new Error(`${name}: queue is not environment-scoped`);
  }
  const consumerQueueNames = (environment.queues?.consumers ?? [])
    .flatMap((consumer) => [consumer.queue, consumer.dead_letter_queue])
    .filter(Boolean);
  if (consumerQueueNames.some((queue) => !queue.includes(`-${name}-`))) {
    throw new Error(`${name}: consumer/dead-letter queue is not environment-scoped`);
  }
  const d1Names = (environment.d1_databases ?? []).map((item) => item.database_name);
  if (d1Names.some((database) => !database.includes(`-${name}-`))) {
    throw new Error(`${name}: D1 database is not environment-scoped`);
  }
  const workflowNames = (environment.workflows ?? []).map((item) => item.name);
  if (workflowNames.some((workflow) => !workflow.includes(`-${name}-`))) {
    throw new Error(`${name}: Workflow is not environment-scoped`);
  }
  const containerNames = (environment.containers ?? []).map((item) => item.name);
  if (containerNames.some((container) => !container.includes(`-${name}-`))) {
    throw new Error(`${name}: Container is not environment-scoped`);
  }
  const origin = new URL(environment.vars.PUBLIC_ORIGIN);
  if (origin.protocol !== "https:" || origin.pathname !== "/") {
    throw new Error(`${name}: PUBLIC_ORIGIN must be an HTTPS origin`);
  }
  const corsOrigins = environment.vars.CORS_ORIGINS.split(",").map((item) => item.trim());
  if (!corsOrigins.includes(environment.vars.PUBLIC_ORIGIN)) {
    throw new Error(`${name}: PUBLIC_ORIGIN must be present in CORS_ORIGINS`);
  }
  for (const corsOrigin of corsOrigins) {
    const parsedCorsOrigin = new URL(corsOrigin);
    if (parsedCorsOrigin.protocol !== "https:" || parsedCorsOrigin.pathname !== "/") {
      throw new Error(`${name}: CORS_ORIGINS must contain HTTPS origins`);
    }
  }
  const allowlistedHosts = environment.vars.ORIGIN_ALLOWLIST.split(",").map((item) => item.trim());
  if (!allowlistedHosts.includes(origin.hostname)) {
    throw new Error(`${name}: PUBLIC_ORIGIN host must be in ORIGIN_ALLOWLIST`);
  }
  if (route.pattern !== origin.hostname) {
    throw new Error(`${name}: custom domain and PUBLIC_ORIGIN host differ`);
  }
}

const workerNames = environments.map((name) => config.env[name].name);
if (new Set(workerNames).size !== workerNames.length) {
  throw new Error("Worker names must be unique across environments");
}

if (JSON.stringify(config).match(/(?:password|token|secret|private_key)"\s*:/i)) {
  throw new Error("configuration contains a secret-looking value; use secret bindings");
}

console.log(`Wrangler configuration OK: ${environments.join(", ")}`);
