from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest


MIGRATIONS = Path(__file__).parents[1] / "cloudflare/migrations"


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        conn.executescript(migration.read_text())
    return conn


def _seed_account(conn: sqlite3.Connection, account_id: str, profile_id: str) -> None:
    conn.execute(
        "INSERT INTO accounts VALUES (?, ?, ?, 'active', 1, 1)",
        (account_id, f"public-{account_id}", f"user-{account_id}"),
    )
    conn.execute(
        "INSERT INTO profile_limits VALUES (?, 1, 1)",
        (account_id,),
    )
    conn.execute(
        "INSERT INTO profiles VALUES (?, ?, ?, ?, ?, 'active', 1, 1, NULL)",
        (profile_id, f"profile-public-{profile_id}", account_id, "Synthetic", "UTC"),
    )
    conn.commit()


def test_control_plane_migration_builds_empty_database() -> None:
    conn = _database()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "accounts", "profiles", "profile_limits", "credentials", "sessions",
            "recovery_codes", "emails", "email_tokens", "terms_versions", "consents",
            "devices", "device_token_hashes", "capability_hashes", "jobs",
            "job_attempts", "audit_events", "deletion_requests", "exports",
            "chart_catalog_sources", "chart_catalog_versions", "chart_catalog_entries",
        }.issubset(tables)
    finally:
        conn.close()


def test_profile_limit_and_composite_ownership_are_enforced() -> None:
    conn = _database()
    try:
        _seed_account(conn, "account-a", "profile-a")
        with pytest.raises(sqlite3.IntegrityError, match="profile_limit_reached"):
            conn.execute(
                "INSERT INTO profiles VALUES (?, ?, ?, ?, ?, 'active', 2, 2, NULL)",
                ("profile-a2", "profile-public-a2", "account-a", "Other", "UTC"),
            )

        _seed_account(conn, "account-b", "profile-b")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, 1, NULL)",
                ("device-a", "device-public-a", "account-a", "profile-b", "wrong-owner"),
            )
    finally:
        conn.close()


def test_audit_and_consent_records_are_append_only_and_hash_only() -> None:
    conn = _database()
    try:
        _seed_account(conn, "account-a", "profile-a")
        conn.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("audit-1", "account-a", "profile-a", "service", "profile.created", None, "req", "resource-hash", "input-hash", "ok", 1),
        )
        with pytest.raises(sqlite3.IntegrityError, match="audit_events_are_append_only"):
            conn.execute("UPDATE audit_events SET status='changed' WHERE id='audit-1'")

        conn.execute(
            "INSERT INTO terms_versions VALUES ('v1', 'terms', 'hash', 1)"
        )
        conn.execute(
            "INSERT INTO consents VALUES "
            "('consent-1', 'account-a', 'profile-a', 'terms', 'v1', 1, NULL)"
        )

        _seed_account(conn, "account-b", "profile-b")
        with pytest.raises(sqlite3.IntegrityError, match="audit_owner_mismatch"):
            conn.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("audit-cross", "account-a", "profile-b", "service", "profile.created", None, "req", "resource-hash", "input-hash", "ok", 1),
            )

        conn.execute("DELETE FROM accounts WHERE id='account-a'")
        assert conn.execute(
            "SELECT account_id, profile_id FROM audit_events WHERE id='audit-1'"
        ).fetchone() == ("account-a", "profile-a")
        assert conn.execute(
            "SELECT account_id, profile_id FROM consents WHERE id='consent-1'"
        ).fetchone() == ("account-a", "profile-a")

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(audit_events)")
        }
        assert {"token", "email", "raw_payload", "body"}.isdisjoint(columns)
    finally:
        conn.close()
