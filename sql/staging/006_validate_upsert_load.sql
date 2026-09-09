-- Define the read-only reconciliation function for one staging UPSERT operation.
-- Returns a JSONB summary from caller-supplied row counts and month-impact totals.
BEGIN;

CREATE OR REPLACE FUNCTION staging.validate_transaction_upsert(
    p_input_rows BIGINT,
    p_target_before BIGINT,
    p_rows_inserted BIGINT,
    p_rows_updated BIGINT,
    p_rows_unchanged BIGINT,
    p_rows_applied BIGINT,
    p_rows_moved BIGINT,
    p_month_rows_entered BIGINT,
    p_month_rows_changed BIGINT,
    p_month_rows_exited BIGINT
)
RETURNS JSONB
LANGUAGE plpgsql
AS $function$
DECLARE
    v_target_after BIGINT;
BEGIN
    IF p_input_rows IS NULL
       OR p_target_before IS NULL
       OR p_rows_inserted IS NULL
       OR p_rows_updated IS NULL
       OR p_rows_unchanged IS NULL
       OR p_rows_applied IS NULL
       OR p_rows_moved IS NULL
       OR p_month_rows_entered IS NULL
       OR p_month_rows_changed IS NULL
       OR p_month_rows_exited IS NULL
       OR p_input_rows < 0
       OR p_target_before < 0
       OR p_rows_inserted < 0
       OR p_rows_updated < 0
       OR p_rows_unchanged < 0
       OR p_rows_applied < 0
       OR p_rows_moved < 0
       OR p_month_rows_entered < 0
       OR p_month_rows_changed < 0
       OR p_month_rows_exited < 0 THEN
        RAISE EXCEPTION
            'UPSERT validation received NULL or negative reconciliation values';
    END IF;

    IF p_input_rows <>
       p_rows_inserted + p_rows_updated + p_rows_unchanged THEN
        RAISE EXCEPTION
            'UPSERT input reconciliation failed: input %, inserted %, updated %, unchanged %',
            p_input_rows,
            p_rows_inserted,
            p_rows_updated,
            p_rows_unchanged;
    END IF;

    IF p_rows_applied <> p_rows_inserted + p_rows_updated THEN
        RAISE EXCEPTION
            'UPSERT write reconciliation failed: applied %, expected %',
            p_rows_applied,
            p_rows_inserted + p_rows_updated;
    END IF;

    IF p_rows_moved > p_rows_updated THEN
        RAISE EXCEPTION
            'UPSERT month reconciliation failed: moved % exceeds updated %',
            p_rows_moved,
            p_rows_updated;
    END IF;

    IF p_month_rows_entered <> p_rows_inserted + p_rows_moved
       OR p_month_rows_changed <> p_rows_updated - p_rows_moved
       OR p_month_rows_exited <> p_rows_moved THEN
        RAISE EXCEPTION
            'UPSERT month reconciliation failed: entered %, changed %, exited %, inserted %, updated %, moved %',
            p_month_rows_entered,
            p_month_rows_changed,
            p_month_rows_exited,
            p_rows_inserted,
            p_rows_updated,
            p_rows_moved;
    END IF;

    v_target_after := p_target_before + p_rows_inserted;

    RETURN jsonb_build_object(
        'status', 'success',
        'mode', 'upsert_only',
        'input_rows', p_input_rows,
        'target_before', p_target_before,
        'target_rows', v_target_after,
        'rows_inserted', p_rows_inserted,
        'rows_updated', p_rows_updated,
        'rows_unchanged', p_rows_unchanged,
        'rows_deleted', 0,
        'rows_applied', p_rows_applied,
        'rows_moved', p_rows_moved,
        'month_rows_entered', p_month_rows_entered,
        'month_rows_changed', p_month_rows_changed,
        'month_rows_exited', p_month_rows_exited,
        'source_transaction_id_constraint', 'PRIMARY KEY'
    );
END
$function$;

COMMENT ON FUNCTION staging.validate_transaction_upsert(
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT
) IS
    'Validates a UPSERT-only staging load without requiring target rows to equal the incoming Parquet snapshot.';

COMMENT ON TABLE staging.stg_transactions IS
    'Current active validated transactions accumulated through UPSERT-only clean Parquet loads.';

COMMENT ON TABLE staging.load_month_changes IS
    'Permanent month-level change log captured from actual staging inserts and updates.';

COMMENT ON COLUMN staging.load_month_changes.has_core_impact IS
    'True when at least one change in this month affects the current core data model.';

COMMIT;
