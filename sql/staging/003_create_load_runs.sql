-- Create the persistent staging load-run ledger used for retries and reconciliation.
-- Requires the staging schema; preserves existing rows and returns no result set.
BEGIN;

CREATE TABLE IF NOT EXISTS staging.load_runs (
    load_batch_id TEXT PRIMARY KEY,
    cleaning_run_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    parquet_rows BIGINT NOT NULL CHECK (parquet_rows >= 0),
    rows_inserted BIGINT CHECK (rows_inserted >= 0),
    rows_updated BIGINT CHECK (rows_updated >= 0),
    rows_unchanged BIGINT CHECK (rows_unchanged >= 0),
    rows_deleted BIGINT CHECK (rows_deleted >= 0),
    target_rows BIGINT CHECK (target_rows >= 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    validation_summary JSONB,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT load_runs_sha256_check
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_load_runs_cleaning_run
    ON staging.load_runs (cleaning_run_id, started_at DESC);

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'stg_transactions_load_batch_fk'
          AND conrelid = 'staging.stg_transactions'::regclass
    ) THEN
        ALTER TABLE staging.stg_transactions
            ADD CONSTRAINT stg_transactions_load_batch_fk
            FOREIGN KEY (load_batch_id)
            REFERENCES staging.load_runs (load_batch_id)
            ON DELETE RESTRICT;
    END IF;
END
$block$;

COMMENT ON TABLE staging.load_runs IS
    'One row per PostgreSQL load attempt, including reconciliation and failure details.';

COMMIT;
