-- Strict advisor proposals and immutable owner-scoped approval journal.

PRAGMA foreign_keys = OFF;

CREATE TABLE advisor_proposals_v2 (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  model_name TEXT NOT NULL DEFAULT 'legacy-unknown',
  provider_self_reported INTEGER NOT NULL DEFAULT 1 CHECK (provider_self_reported = 1),
  title TEXT NOT NULL,
  body_text TEXT NOT NULL DEFAULT '',
  evidence_from INTEGER NOT NULL DEFAULT 0,
  evidence_to INTEGER NOT NULL DEFAULT 0,
  proposal_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL DEFAULT 0,
  decided_at INTEGER,
  decision_reason TEXT,
  decided_by_client_id TEXT,
  decision_marker TEXT,
  scope_snapshot TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

INSERT INTO advisor_proposals_v2(
  id, account_id, profile_id, provider_name, title, proposal_hash,
  payload_json, status, created_at, decided_at, decision_reason, expires_at
)
SELECT id, account_id, profile_id, provider_name, title, proposal_hash,
       payload_json, status, created_at, decided_at, decision_reason,
       CASE WHEN status = 'pending' THEN created_at + 604800 ELSE created_at END
  FROM advisor_proposals;

DROP TABLE advisor_proposals;
ALTER TABLE advisor_proposals_v2 RENAME TO advisor_proposals;

CREATE INDEX advisor_proposals_by_profile
  ON advisor_proposals(account_id, profile_id, status, created_at);
CREATE INDEX advisor_proposals_expiry
  ON advisor_proposals(status, expires_at);

PRAGMA foreign_keys = ON;

CREATE TABLE advisor_journal (
  id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  provider_name TEXT NOT NULL,
  model_name TEXT NOT NULL,
  title TEXT NOT NULL,
  body_text TEXT NOT NULL,
  evidence_from INTEGER NOT NULL,
  evidence_to INTEGER NOT NULL,
  proposal_hash TEXT NOT NULL,
  proposal_created_at INTEGER NOT NULL,
  approved_at INTEGER NOT NULL,
  approved_by_client_id TEXT NOT NULL,
  scope_snapshot TEXT NOT NULL
);

CREATE INDEX advisor_journal_by_owner
  ON advisor_journal(account_id, profile_id, approved_at);

CREATE TRIGGER advisor_journal_append_only_update
BEFORE UPDATE ON advisor_journal
BEGIN
  SELECT RAISE(ABORT, 'advisor_journal_is_append_only');
END;

CREATE TRIGGER advisor_journal_append_only_delete
BEFORE DELETE ON advisor_journal
WHEN NOT EXISTS (
  SELECT 1 FROM deletion_requests
   WHERE account_id = OLD.account_id AND profile_id = OLD.profile_id
     AND status = 'confirmed'
)
BEGIN
  SELECT RAISE(ABORT, 'advisor_journal_is_append_only');
END;

CREATE TABLE advisor_decision_audits (
  id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  actor_client_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'expired')),
  proposal_hash TEXT NOT NULL,
  reason_code TEXT,
  occurred_at INTEGER NOT NULL
);

CREATE INDEX advisor_decisions_by_owner
  ON advisor_decision_audits(account_id, profile_id, occurred_at);

CREATE TRIGGER advisor_decision_audits_append_only_update
BEFORE UPDATE ON advisor_decision_audits
BEGIN
  SELECT RAISE(ABORT, 'advisor_decision_audits_are_append_only');
END;

CREATE TRIGGER advisor_decision_audits_append_only_delete
BEFORE DELETE ON advisor_decision_audits
WHEN NOT EXISTS (
  SELECT 1 FROM deletion_requests
   WHERE account_id = OLD.account_id AND profile_id = OLD.profile_id
     AND status = 'confirmed'
)
BEGIN
  SELECT RAISE(ABORT, 'advisor_decision_audits_are_append_only');
END;
