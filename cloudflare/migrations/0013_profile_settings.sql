-- Owner-scoped training preferences and one-live-secret capability lifecycle.

CREATE TABLE profile_settings (
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT NOT NULL,
  target_judged INTEGER NOT NULL DEFAULT 100000
    CHECK (target_judged BETWEEN 10000 AND 1000000),
  reserve_judged INTEGER NOT NULL DEFAULT 10000
    CHECK (reserve_judged BETWEEN 0 AND 500000),
  readiness TEXT NOT NULL DEFAULT 'normal'
    CHECK (readiness IN ('normal', 'tired')),
  settings_revision INTEGER NOT NULL DEFAULT 1 CHECK (settings_revision > 0),
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(account_id, profile_id),
  FOREIGN KEY(account_id, profile_id)
    REFERENCES profiles(account_id, id) ON DELETE CASCADE
);

INSERT INTO profile_settings(account_id, profile_id, updated_at)
SELECT account_id, id, updated_at FROM profiles
WHERE status <> 'deleted';

-- Preserve the newest active capability if a pre-migration profile somehow
-- accumulated more than one. Older rows remain auditable and revoked.
UPDATE capability_hashes AS older
   SET revoked_at = created_at
 WHERE revoked_at IS NULL
   AND capability_kind IN ('recommend', 'today')
   AND EXISTS (
     SELECT 1 FROM capability_hashes AS newer
      WHERE newer.profile_id = older.profile_id
        AND newer.capability_kind = older.capability_kind
        AND newer.revoked_at IS NULL
        AND (newer.created_at > older.created_at
             OR (newer.created_at = older.created_at AND newer.id > older.id))
   );

CREATE UNIQUE INDEX one_live_table_capability
  ON capability_hashes(profile_id, capability_kind)
  WHERE revoked_at IS NULL AND capability_kind IN ('recommend', 'today');
