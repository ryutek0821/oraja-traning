-- Difficulty-table bands are required by the shared recommendation core.
-- Existing catalog rows remain unusable until their publisher supplies an
-- explicit level; the Worker never invents a band from title or ordering.

ALTER TABLE chart_catalog_entries ADD COLUMN level TEXT
  CHECK (level IS NULL OR (length(level) BETWEEN 1 AND 64));

CREATE INDEX chart_catalog_active_table_level
  ON chart_catalog_entries(catalog_version_id, level, sha256)
  WHERE level IS NOT NULL;
