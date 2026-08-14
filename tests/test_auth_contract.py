from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
AUTH = (ROOT / "cloudflare/worker/src/auth.ts").read_text()
EMAIL = (ROOT / "cloudflare/worker/src/email.ts").read_text()
MIGRATION = (ROOT / "cloudflare/migrations/0002_auth.sql").read_text()
AUTH_DOC = (ROOT / "docs/authentication.md").read_text()


def test_auth_policy_has_explicit_ascii_and_password_boundaries() -> None:
    assert "const USER_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{2,31}$/" in AUTH
    assert "length < 12 || length > 128" in AUTH
    assert "passwordParamsVersion: 1" in AUTH
    assert "argon2id" in AUTH
    assert "rateLimitWindowSeconds" in AUTH
    assert "documentVersion" in AUTH


def test_secrets_are_hashed_or_single_use_and_not_logged() -> None:
    assert "recovery_codes" in MIGRATION
    assert "salt TEXT NOT NULL" in MIGRATION
    assert "token_hash TEXT NOT NULL" in MIGRATION or "token_hash" in (ROOT / "cloudflare/migrations/0001_control_plane.sql").read_text()
    assert "console.log" not in AUTH
    assert "console.log" not in EMAIL
    assert "recovery_codes" in AUTH and "code_hash" in AUTH
    assert "UPDATE email_tokens SET used_at" in AUTH
    assert "e.verified_at IS NOT NULL" in AUTH
    assert "email_change_requested" in AUTH
    assert "email_changed" in AUTH


def test_cookie_contract_prevents_session_fixation_and_csrf_cookie_leakage() -> None:
    assert "__Host-oraja_session" in AUTH
    assert "HttpOnly" in AUTH
    assert "Secure" in AUTH
    assert "SameSite=Lax" in AUTH
    assert "csrf_hash" in AUTH
    assert "csrf_required" in (ROOT / "cloudflare/worker/src/index.ts").read_text()
    assert "session_rotated" in AUTH


def test_email_body_has_only_opaque_capability_link_and_no_play_payload() -> None:
    assert "Auto-Submitted: auto-generated" in EMAIL
    assert "play" not in EMAIL.lower()
    assert re.search(r"short.*失効|15分で失効", EMAIL)
    email_body = EMAIL.split("export function buildAuthEmail", 1)[1]
    assert "password" not in email_body.lower().replace("password_reset", "")
    assert "OPAQUE_TOKEN_PATTERN" in EMAIL
    assert "email_origin_must_be_https" in EMAIL


def test_rate_limit_is_ip_and_account_scoped_without_raw_key_columns() -> None:
    assert "scope IN ('ip', 'account', 'recovery', 'email')" in MIGRATION
    assert "key_hash TEXT NOT NULL" in MIGRATION
    assert "rateKey" in AUTH and "recordFailure" in AUTH
    assert "await enforceRateLimit(db, \"account\", row.account_id, now, options.hashingSecret)" in AUTH
    assert "HMAC" in AUTH and "AUTH_HASH_PEPPER" in (ROOT / "cloudflare/worker/src/index.ts").read_text()
    assert "export async function requestEmailChange" in AUTH


def test_auth_runbook_documents_secret_free_mock_and_production_gate() -> None:
    assert "mock" in AUTH_DOC.lower()
    assert "EMAIL_ENCRYPTION_KEY" in AUTH_DOC
    assert "デプロイ" in AUTH_DOC
    assert "回復コード" in AUTH_DOC
