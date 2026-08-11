import { readdir, readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptsDirectory = new URL("./", import.meta.url);
const scriptFiles = (await readdir(scriptsDirectory))
  .filter((file) => file.endsWith(".mjs"))
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
  new URL("../worker/src/index.ts", import.meta.url),
  new URL("../worker/src/auth.ts", import.meta.url),
  new URL("../worker/src/control-plane.ts", import.meta.url),
  new URL("../worker/src/email.ts", import.meta.url),
  new URL("../worker/src/ir-api.ts", import.meta.url),
  new URL("../worker/src/profile-do.ts", import.meta.url),
  new URL("../worker/src/oauth.ts", import.meta.url),
  new URL("../worker/src/mcp.ts", import.meta.url),
  new URL("../worker/src/advisor.ts", import.meta.url),
  new URL("../worker/src/privacy.ts", import.meta.url),
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
