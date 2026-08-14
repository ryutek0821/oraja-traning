-- Issue #11: durable queue/workflow ledger and monotonic revision publication.
-- Existing migrations are never edited after application; this migration only
-- adds the async control-plane contract.

PRAGMA foreign_keys = ON;

ALTER TABLE jobs ADD COLUMN event_id TEXT;
ALTER TABLE jobs ADD COLUMN request_id TEXT;
ALTER TABLE jobs ADD COLUMN contract_version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE jobs ADD COLUMN trust_domain TEXT NOT NULL DEFAULT 'official'
  CHECK (trust_domain IN ('official', 'self_hosted'));
ALTER TABLE jobs ADD COLUMN correlation_id TEXT;
ALTER TABLE jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 4
  CHECK (max_attempts > 0);
ALTER TABLE jobs ADD COLUMN next_attempt_at INTEGER;
ALTER TABLE jobs ADD COLUMN started_at INTEGER;
ALTER TABLE jobs ADD COLUMN cancel_requested_at INTEGER;
ALTER TABLE jobs ADD COLUMN terminal_reason TEXT;
ALTER TABLE jobs ADD COLUMN workflow_instance_id TEXT;
ALTER TABLE jobs ADD COLUMN workflow_run INTEGER NOT NULL DEFAULT 0
  CHECK (workflow_run >= 0);
ALTER TABLE jobs ADD COLUMN input_key TEXT;
ALTER TABLE jobs ADD COLUMN artifact_key TEXT;

CREATE INDEX jobs_by_event
  ON jobs(account_id, profile_id, event_id);

CREATE INDEX jobs_by_correlation
  ON jobs(account_id, profile_id, correlation_id, created_at);

-- This is the immutable acceptance record. A duplicate Queue delivery is not
-- a new event, even when Cloudflare assigns it a different message ID.
CREATE TABLE job_events (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  request_id TEXT,
  event_kind TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  accepted_at INTEGER NOT NULL,
  UNIQUE(profile_id, event_id),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX job_events_by_owner
  ON job_events(account_id, profile_id, accepted_at);

CREATE TRIGGER job_events_append_only_update
BEFORE UPDATE ON job_events
BEGIN
  SELECT RAISE(ABORT, 'job_events_are_append_only');
END;

CREATE TRIGGER job_events_append_only_delete
BEFORE DELETE ON job_events
BEGIN
  SELECT RAISE(ABORT, 'job_events_are_append_only');
END;

-- A counter is partitioned by owner and trust domain. Gaps are harmless (for
-- example, a concurrent insert can reserve a number before losing a unique
-- event race), but a number is never reused.
CREATE TABLE profile_revision_counters (
  account_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  trust_domain TEXT NOT NULL CHECK (trust_domain IN ('official', 'self_hosted')),
  next_revision INTEGER NOT NULL DEFAULT 0 CHECK (next_revision >= 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(account_id, profile_id, trust_domain),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

-- Reservation is immutable. It is the bridge between an accepted event and
-- every output/artifact produced by that event.
CREATE TABLE job_revisions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  trust_domain TEXT NOT NULL CHECK (trust_domain IN ('official', 'self_hosted')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  previous_revision INTEGER CHECK (previous_revision IS NULL OR previous_revision >= 0),
  input_digest TEXT NOT NULL,
  reserved_at INTEGER NOT NULL,
  UNIQUE(account_id, profile_id, trust_domain, revision),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX job_revisions_by_owner
  ON job_revisions(account_id, profile_id, trust_domain, revision);

CREATE TRIGGER job_revisions_append_only_update
BEFORE UPDATE ON job_revisions
BEGIN
  SELECT RAISE(ABORT, 'job_revisions_are_append_only');
END;

CREATE TRIGGER job_revisions_append_only_delete
BEFORE DELETE ON job_revisions
BEGIN
  SELECT RAISE(ABORT, 'job_revisions_are_append_only');
END;

-- One immutable result per workflow run. A manual rerun increments
-- workflow_run, so the failed result remains auditable and is never replaced.
CREATE TABLE revision_outcomes (
  id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES job_revisions(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  workflow_run INTEGER NOT NULL CHECK (workflow_run >= 0),
  status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'cancelled')),
  output_digest TEXT,
  manifest_sha256 TEXT,
  error_code TEXT,
  error_hash TEXT,
  recorded_at INTEGER NOT NULL,
  UNIQUE(revision_id, workflow_run)
);

CREATE INDEX revision_outcomes_by_job
  ON revision_outcomes(job_id, workflow_run, recorded_at);

CREATE TRIGGER revision_outcomes_append_only_update
BEFORE UPDATE ON revision_outcomes
BEGIN
  SELECT RAISE(ABORT, 'revision_outcomes_are_append_only');
END;

CREATE TRIGGER revision_outcomes_append_only_delete
BEFORE DELETE ON revision_outcomes
BEGIN
  SELECT RAISE(ABORT, 'revision_outcomes_are_append_only');
END;

-- Mutable pointer only. The Worker updates it with
-- `candidate_revision > current_revision`; old completions are retained but
-- can never move this row backwards.
CREATE TABLE latest_pointers (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  trust_domain TEXT NOT NULL CHECK (trust_domain IN ('official', 'self_hosted')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  revision_id TEXT NOT NULL REFERENCES job_revisions(id) ON DELETE RESTRICT,
  manifest_sha256 TEXT NOT NULL,
  artifact_key TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(account_id, profile_id, trust_domain),
  FOREIGN KEY(account_id, profile_id) REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

CREATE INDEX latest_pointers_by_revision
  ON latest_pointers(account_id, profile_id, trust_domain, revision);

-- Step attempts are mutable operational metadata, not the immutable result.
-- `workflow_run` separates an operator rerun from the original attempt 1.
CREATE TABLE workflow_step_attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  workflow_run INTEGER NOT NULL CHECK (workflow_run >= 0),
  step_name TEXT NOT NULL CHECK (
    step_name IN (
      'r2-verify', 'normalize', 'model-update', 'generate-tables',
      'artifact-verify', 'publish'
    )
  ),
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0, 1)),
  started_at INTEGER NOT NULL,
  finished_at INTEGER,
  timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0),
  error_code TEXT,
  error_hash TEXT,
  UNIQUE(job_id, workflow_run, step_name, attempt)
);

CREATE INDEX workflow_step_attempts_by_job
  ON workflow_step_attempts(job_id, workflow_run, step_name, attempt);

-- The delivery table makes a repeated message ID a cheap no-op. The job's
-- deterministic workflow instance ID handles duplicates with a new message ID.
CREATE TABLE job_deliveries (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('claimed', 'duplicate', 'acked', 'retry')),
  received_at INTEGER NOT NULL,
  finished_at INTEGER,
  UNIQUE(job_id, message_id)
);

CREATE INDEX job_deliveries_by_job
  ON job_deliveries(job_id, received_at);

-- Outbox state is durable before a 202/Queue send boundary. If send succeeds
-- but marking `sent` times out, the next lease can send again; the consumer is
-- idempotent, so correctness does not depend on an exactly-once transport.
CREATE TABLE job_dispatches (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  dispatch_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  lease_until INTEGER,
  next_attempt_at INTEGER,
  last_error_code TEXT,
  updated_at INTEGER NOT NULL
);

-- Operator-triggered reruns are append-only and preserve the original failed
-- outcome. The job itself is returned to queued with a new workflow_run.
CREATE TABLE job_requeues (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  workflow_run INTEGER NOT NULL CHECK (workflow_run > 0),
  requested_by TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  requested_at INTEGER NOT NULL,
  UNIQUE(job_id, workflow_run)
);

CREATE TRIGGER job_requeues_append_only_update
BEFORE UPDATE ON job_requeues
BEGIN
  SELECT RAISE(ABORT, 'job_requeues_are_append_only');
END;

CREATE TRIGGER job_requeues_append_only_delete
BEFORE DELETE ON job_requeues
BEGIN
  SELECT RAISE(ABORT, 'job_requeues_are_append_only');
END;

-- Schedule delivery is also idempotent. It is keyed by the UTC scheduled
-- timestamp, not the current wall clock, so a retried cron invocation cannot
-- create a second monthly/export/delete job for the same profile.
CREATE TABLE schedule_runs (
  schedule_name TEXT NOT NULL CHECK (schedule_name IN ('monthly', 'daily-backup', 'deletion-sweep')),
  profile_id TEXT NOT NULL,
  scheduled_at INTEGER NOT NULL,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'duplicate')),
  recorded_at INTEGER NOT NULL,
  PRIMARY KEY(schedule_name, profile_id, scheduled_at)
);

CREATE INDEX schedule_runs_by_time
  ON schedule_runs(schedule_name, scheduled_at);
