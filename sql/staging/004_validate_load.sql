-- Define the read-only validation function for a staged transaction snapshot.
-- Requires staging.stg_transactions and returns a JSONB validation summary when called.
CREATE OR REPLACE FUNCTION staging.validate_transaction_snapshot(
    p_expected_rows BIGINT,
    p_cleaning_run_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
AS $function$
DECLARE
    v_target_rows BIGINT;
    v_expected_run_rows BIGINT;
    v_invalid_period_rows BIGINT;
    v_null_required_rows BIGINT;
    v_result JSONB;
BEGIN
    SELECT
        count(*),
        count(*) FILTER (WHERE cleaning_run_id = p_cleaning_run_id),
        count(*) FILTER (
            WHERE transaction_year NOT BETWEEN 2012 AND 2024
               OR transaction_month NOT BETWEEN 1 AND 12
        ),
        count(*) FILTER (
            WHERE source_transaction_id IS NULL
               OR cleaning_run_id IS NULL
               OR county_id IS NULL
               OR town_id IS NULL
               OR city IS NULL
               OR district IS NULL
               OR transaction_type IS NULL
        )
    INTO
        v_target_rows,
        v_expected_run_rows,
        v_invalid_period_rows,
        v_null_required_rows
    FROM staging.stg_transactions;

    IF v_target_rows <> p_expected_rows THEN
        RAISE EXCEPTION
            'staging row reconciliation failed: expected %, found %',
            p_expected_rows,
            v_target_rows;
    END IF;

    IF v_expected_run_rows <> p_expected_rows THEN
        RAISE EXCEPTION
            'cleaning run reconciliation failed: expected % rows for %, found %',
            p_expected_rows,
            p_cleaning_run_id,
            v_expected_run_rows;
    END IF;

    IF v_invalid_period_rows <> 0 OR v_null_required_rows <> 0 THEN
        RAISE EXCEPTION
            'staging content validation failed: invalid period %, null required %',
            v_invalid_period_rows,
            v_null_required_rows;
    END IF;

    v_result := jsonb_build_object(
        'status', 'success',
        'target_rows', v_target_rows,
        'expected_rows', p_expected_rows,
        'cleaning_run_id', p_cleaning_run_id,
        'cleaning_run_rows', v_expected_run_rows,
        'invalid_period_rows', v_invalid_period_rows,
        'null_required_rows', v_null_required_rows,
        'source_transaction_id_constraint', 'PRIMARY KEY'
    );

    RETURN v_result;
END
$function$;

COMMENT ON FUNCTION staging.validate_transaction_snapshot(BIGINT, TEXT) IS
    'Raises on failed load reconciliation; returns a compact JSONB validation summary on success.';
