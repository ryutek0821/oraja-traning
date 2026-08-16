import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

test("dashboard never accepts a free-form profile capability", async () => {
  const dashboard = await readFile(new URL("worker/src/dashboard.ts", root), "utf8");
  const html = await readFile(new URL("web/public/index.html", root), "utf8");
  assert.match(dashboard, /sessionAccount/);
  assert.match(dashboard, /WHERE account_id = \?1/);
  assert.doesNotMatch(html, /id="profile"|プロフィールID|프로필 ID/);
  assert.doesNotMatch(dashboard, /object_key/);
});

test("account and dashboard surfaces cover bilingual and one-time secret flows", async () => {
  const app = await readFile(new URL("web/public/app.js", root), "utf8");
  const auth = await readFile(new URL("web/public/auth.js", root), "utf8");
  const css = await readFile(new URL("web/public/app.css", root), "utf8");
  assert.match(app, /ja:/); assert.match(app, /ko:/);
  assert.match(auth, /recovery_codes/); assert.match(auth, /textContent=""/);
  assert.match(app, /device-token/); assert.match(app, /textContent=""/);
  assert.match(css, /prefers-reduced-motion/); assert.match(css, /:focus-visible/);
});
