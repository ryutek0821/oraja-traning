import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const forbidden = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /AKIA[0-9A-Z]{16}/,
  /(?:\/Users\/|\/home\/)[^\s"']+/,
  new RegExp(["player", "file"].join("-"), "i"),
];

const root = execFileSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).trim();
const files = execFileSync("git", ["ls-files"], { cwd: root, encoding: "utf8" })
  .split("\n")
  .filter(Boolean)
  .filter((file) => !file.startsWith(".git/") && !file.endsWith(".db"));
const findings = [];
for (const file of files) {
  let source;
  try {
    source = await readFile(resolve(root, file), "utf8");
  } catch {
    continue;
  }
  for (const pattern of forbidden) {
    // The ignore rule prevents accidental staging of the legacy private-data directory.
    if (file === ".gitignore" && pattern === forbidden.at(-1)) continue;
    if (pattern.test(source)) findings.push(`${file}: ${pattern}`);
  }
}
if (findings.length > 0) {
  console.error(findings.join("\n"));
  process.exit(1);
}
console.log(`Public scan OK: ${files.length} tracked text candidates`);
