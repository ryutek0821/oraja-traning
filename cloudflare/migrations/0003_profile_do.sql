-- Profile Durable Object storage is isolated per profile and uses the DO
-- storage API rather than D1 tables.  Keep this explicit no-op migration as
-- the schema boundary before IR/API and workflow migrations.

-- No D1 statements are required for this boundary.
