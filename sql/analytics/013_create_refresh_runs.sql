-- Create the persistent analytics refresh ledger used for full and incremental runs.
-- Requires staging.load_runs; preserves existing rows and returns no result set.
BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.refresh_runs (
    analytics_run_id UUID PRIMARY KEY,
    refresh_mode TEXT NOT NULL,
    load_batch_id TEXT,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    refresh_summary JSONB,
    validation_summary JSONB,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,

    CONSTRAINT refresh_runs_load_batch_fk
        FOREIGN KEY (load_batch_id)
        REFERENCES core.sync_runs (load_batch_id)
        ON DELETE RESTRICT,

    CONSTRAINT refresh_runs_mode_check
        CHECK (refresh_mode IN ('full', 'incremental')),

    CONSTRAINT refresh_runs_status_check
        CHECK (status IN ('running', 'success', 'failed')),

    CONSTRAINT refresh_runs_mode_batch_check
        CHECK (
            (refresh_mode = 'full' AND load_batch_id IS NULL)
            OR
            (
                refresh_mode = 'incremental'
                AND load_batch_id IS NOT NULL
                AND load_batch_id = BTRIM(load_batch_id)
                AND load_batch_id <> ''
            )
        ),

    CONSTRAINT refresh_runs_attempt_count_check
        CHECK (attempt_count > 0),

    CONSTRAINT refresh_runs_state_check
        CHECK (
            (
                status = 'running'
                AND refresh_summary IS NULL
                AND validation_summary IS NULL
                AND error_message IS NULL
                AND completed_at IS NULL
            )
            OR
            (
                status = 'success'
                AND refresh_summary IS NOT NULL
                AND validation_summary IS NOT NULL
                AND error_message IS NULL
                AND completed_at IS NOT NULL
            )
            OR
            (
                status = 'failed'
                AND validation_summary IS NULL
                AND error_message IS NOT NULL
                AND BTRIM(error_message) <> ''
                AND completed_at IS NOT NULL
            )
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_refresh_runs_incremental_batch
    ON analytics.refresh_runs (load_batch_id)
    WHERE refresh_mode = 'incremental';

CREATE INDEX IF NOT EXISTS idx_refresh_runs_mode_status_started_at
    ON analytics.refresh_runs (refresh_mode, status, started_at DESC);

COMMENT ON TABLE analytics.refresh_runs IS
    'Tracks full and per-core-batch incremental Analytics refresh attempts.';

COMMENT ON COLUMN analytics.refresh_runs.analytics_run_id IS
    'Stable identifier for one Analytics refresh run; failed incremental retries reuse the same row.';

COMMENT ON COLUMN analytics.refresh_runs.load_batch_id IS
    'Required idempotency key for incremental refreshes; NULL for reviewed full refreshes.';

COMMENT ON COLUMN analytics.refresh_runs.refresh_summary IS
    'Machine-readable summaries returned by the three refresh SQL scripts.';

COMMENT ON COLUMN analytics.refresh_runs.validation_summary IS
    'Machine-readable summaries from the three independent validation scripts.';

COMMIT;
