-- Create the persistent staging schema used by validated loads and load history.
-- Requires permission to create schemas; returns no result set.
BEGIN;

CREATE SCHEMA IF NOT EXISTS staging;

COMMENT ON SCHEMA staging IS
    'Validated landing layer for the current clean Parquet snapshot and its load history.';

COMMIT;
