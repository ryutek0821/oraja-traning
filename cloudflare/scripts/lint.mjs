import { readdir, readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptsDirectory = new URL("./", import.meta.url);
const workerSourceDirectory = new URL("../worker/src/", import.meta.url);
const scriptFiles = (await readdir(scriptsDirectory))
  .filter((file) => file.endsWith(".mjs"))
  .sort();
const workerSourceFiles = (await readdir(workerSourceDirectory))
  .filter((file) => file.endsWith(".ts"))
  .sort();

for (const file of scriptFiles) {
  const path = new URL(file, scriptsDirectory);
  const result = spawnSync(process.execPath, ["--check", fileURLToPath(path)], { encoding: "utf8" });
  if (result.status !== 0) {
    process.stderr.write(result.stderr || `${file}: syntax check failed\n`);
    process.exit(result.status ?? 1);
  }
}

const sourceFiles = [
  ...scriptFiles.map((file) => new URL(file, scriptsDirectory)),
  ...workerSourceFiles.map((file) => new URL(file, workerSourceDirectory)),
];

for (const path of sourceFiles) {
  const source = await readFile(path, "utf8");
  if (!path.pathname.endsWith("/lint.mjs") && /\b(?:TODO|FIXME)\b/.test(source)) {
    throw new Error(`${path.pathname}: unresolved TODO/FIXME marker`);
  }
  if (/\bany\b/.test(source) && path.pathname.endsWith(".ts")) {
    throw new Error(`${path.pathname}: explicit any is not allowed in Worker code`);
  }
}

console.log(`Lint OK: ${sourceFiles.length} source files and ${scriptFiles.length} scripts`);
