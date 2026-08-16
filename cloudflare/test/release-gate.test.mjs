import assert from "node:assert/strict";
import test from "node:test";

import { validateReleaseGate } from "../scripts/release-gate.mjs";

const valid = {
  ORAJA_CHANGE_ISSUE: "#20",
  ORAJA_BACKUP_EVIDENCE: "d1-backup:2026-08-14T120000Z",
  ORAJA_RESTORE_DRILL_EVIDENCE: "drill:isolated-2026-08-14",
  ORAJA_ROLLBACK_VERSION: "a".repeat(40),
  ORAJA_RELEASE_MANIFEST_SHA256: "b".repeat(64),
  ORAJA_MIGRATION_PLAN_SHA256: "c".repeat(64),
  ORAJA_GO_NO_GO: "go",
};

test("release gate accepts complete non-secret evidence", () => {
  assert.doesNotThrow(() => validateReleaseGate("staging", valid));
  assert.doesNotThrow(() => validateReleaseGate("production", valid));
});

test("release gate fails closed on missing, malformed, or held evidence", () => {
  for (const key of Object.keys(valid)) {
    assert.throws(() => validateReleaseGate("production", { ...valid, [key]: "" }));
  }
  assert.throws(() => validateReleaseGate("preview", valid));
  assert.throws(() => validateReleaseGate("production", { ...valid, ORAJA_GO_NO_GO: "GO" }));
});
