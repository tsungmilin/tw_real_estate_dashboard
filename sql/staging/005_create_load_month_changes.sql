-- Create the persistent month-impact ledger for incremental downstream refreshes.
-- Requires staging.load_runs; preserves existing rows and returns no result set.
BEGIN;

CREATE TABLE IF NOT EXISTS staging.load_month_changes (
    load_batch_id TEXT NOT NULL,
    month_start DATE NOT NULL,

    rows_entered BIGINT NOT NULL DEFAULT 0,
    rows_changed BIGINT NOT NULL DEFAULT 0,
    rows_exited BIGINT NOT NULL DEFAULT 0,

    has_core_impact BOOLEAN NOT NULL,

    recorded_at TIMESTAMPTZ NOT NULL
        DEFAULT clock_timestamp(),

    CONSTRAINT load_month_changes_pk
        PRIMARY KEY (load_batch_id, month_start),

    CONSTRAINT load_month_changes_load_batch_fk
        FOREIGN KEY (load_batch_id)
        REFERENCES staging.load_runs (load_batch_id)
        ON DELETE RESTRICT,

    CONSTRAINT load_month_changes_month_check
        CHECK (EXTRACT(DAY FROM month_start) = 1),

    CONSTRAINT load_month_changes_counts_check
        CHECK (
            rows_entered >= 0
            AND rows_changed >= 0
            AND rows_exited >= 0
            AND rows_entered + rows_changed + rows_exited > 0
        ),

    CONSTRAINT load_month_changes_core_impact_check
        CHECK (
            has_core_impact
            OR (
                rows_entered = 0
                AND rows_exited = 0
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_load_month_changes_month
    ON staging.load_month_changes (month_start, load_batch_id);

COMMENT ON TABLE staging.load_month_changes IS
    'Permanent month-level change log captured from actual staging inserts and updates.';

COMMENT ON COLUMN staging.load_month_changes.rows_entered IS
    'Transactions newly entering this month, including transactions moved in from another month.';

COMMENT ON COLUMN staging.load_month_changes.rows_changed IS
    'Transactions whose month is unchanged but whose staged content changed.';

COMMENT ON COLUMN staging.load_month_changes.rows_exited IS
    'Transactions leaving this month because their transaction month moved.';

COMMENT ON COLUMN staging.load_month_changes.has_core_impact IS
    'True when at least one change in this month affects the current core data model.';

COMMIT;
