import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";

const forbidden = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /AKIA[0-9A-Z]{16}/,
  /(?:\/Users\/|\/home\/)[^\s"']+/,
  /player-file/i,
];

const files = execFileSync("git", ["ls-files"], { encoding: "utf8" })
  .split("\n")
  .filter(Boolean)
  .filter((file) => !file.startsWith(".git/") && !file.endsWith(".db"));
const findings = [];
for (const file of files) {
  let source;
  try {
    source = await readFile(file, "utf8");
  } catch {
    continue;
  }
  for (const pattern of forbidden) {
    if (pattern.test(source)) findings.push(`${file}: ${pattern}`);
  }
}
if (findings.length > 0) {
  console.error(findings.join("\n"));
  process.exit(1);
}
console.log(`Public scan OK: ${files.length} tracked text candidates`);
