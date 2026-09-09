-- 必須由 psql 以 --set=load_batch_id=<明確批次 ID> 執行。
-- core loader 會先把該批次的 core.sync_runs 建立或重啟為 running；本檔案
-- 負責同一個 transaction 內的來源驗證、分類、fact UPSERT 與成功狀態發布。
\if :{?load_batch_id}
\else
\echo '缺少必要的 psql 變數：load_batch_id'
\quit 3
\endif

BEGIN;

-- 所有日常 core batch 共用同一把 transaction lock。第二個 batch 會等待第一個
-- batch COMMIT 或 ROLLBACK，避免同時分類及寫入相同 fact。
DO $core_batch_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('core.fact_transactions.batch_sync', 0)
    );
END
$core_batch_lock$;

-- 先把 psql 參數放進 transaction-local table，讓後續 dollar-quoted PL/pgSQL
-- block 不需要直接做 psql 變數替換。
CREATE TEMP TABLE core_batch_parameter (
    load_batch_id TEXT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO core_batch_parameter (load_batch_id)
VALUES (:'load_batch_id');

-- context 固定只建立一列，集中保存 staging 對帳基準與 core 執行狀態。
CREATE TEMP TABLE core_batch_context ON COMMIT DROP AS
SELECT
    parameter.load_batch_id,
    load_run.load_batch_id IS NOT NULL AS staging_batch_exists,
    load_run.status AS staging_status,
    load_run.parquet_rows,
    load_run.rows_inserted AS staging_rows_inserted,
    load_run.rows_updated AS staging_rows_updated,
    load_run.rows_unchanged AS staging_rows_unchanged,
    load_run.rows_deleted AS staging_rows_deleted,
    (
        load_run.rows_inserted + load_run.rows_updated
    )::BIGINT AS expected_source_rows,
    sync_run.status AS sync_status,
    COALESCE(sync_run.status = 'success', FALSE) AS already_success
FROM core_batch_parameter AS parameter
LEFT JOIN staging.load_runs AS load_run
    USING (load_batch_id)
LEFT JOIN core.sync_runs AS sync_run
    USING (load_batch_id)
;

-- 這個 gate 會阻擋不存在、未成功或 staging 對帳不完整的 batch。012 只接受
-- core loader 已正式登記為 running 的嘗試；success 則走冪等回傳路徑。
DO $core_batch_input_gate$
DECLARE
    v_load_batch_id TEXT;
    v_staging_batch_exists BOOLEAN;
    v_staging_status TEXT;
    v_parquet_rows BIGINT;
    v_staging_rows_inserted BIGINT;
    v_staging_rows_updated BIGINT;
    v_staging_rows_unchanged BIGINT;
    v_staging_rows_deleted BIGINT;
    v_sync_status TEXT;
BEGIN
    SELECT
        load_batch_id,
        staging_batch_exists,
        staging_status,
        parquet_rows,
        staging_rows_inserted,
        staging_rows_updated,
        staging_rows_unchanged,
        staging_rows_deleted,
        sync_status
    INTO
        v_load_batch_id,
        v_staging_batch_exists,
        v_staging_status,
        v_parquet_rows,
        v_staging_rows_inserted,
        v_staging_rows_updated,
        v_staging_rows_unchanged,
        v_staging_rows_deleted,
        v_sync_status
    FROM core_batch_context;

    IF NOT v_staging_batch_exists THEN
        RAISE EXCEPTION
            'Core batch input validation failed: staging load batch % does not exist',
            v_load_batch_id;
    END IF;

    IF v_staging_status <> 'success' THEN
        RAISE EXCEPTION
            'Core batch input validation failed: staging load batch % has status %, expected success',
            v_load_batch_id,
            v_staging_status;
    END IF;

    IF v_staging_rows_inserted IS NULL
       OR v_staging_rows_updated IS NULL
       OR v_staging_rows_unchanged IS NULL
       OR v_staging_rows_deleted IS NULL
       OR v_parquet_rows <>
            v_staging_rows_inserted
            + v_staging_rows_updated
            + v_staging_rows_unchanged
       OR v_staging_rows_deleted <> 0 THEN
        RAISE EXCEPTION
            'Core batch input validation failed: invalid staging reconciliation for batch %',
            v_load_batch_id;
    END IF;

    IF v_sync_status IS NULL THEN
        RAISE EXCEPTION
            'Core batch % has no core.sync_runs attempt; register it as running first',
            v_load_batch_id;
    END IF;

    IF v_sync_status NOT IN ('running', 'success') THEN
        RAISE EXCEPTION
            'Core batch % has status %; restart it as running before execution',
            v_load_batch_id,
            v_sync_status;
    END IF;
END
$core_batch_input_gate$;

-- success 批次的來源表會刻意保持空白，因此後續所有寫入都是 no-op，最後只
-- 回傳既有 sync_runs。running 批次只擷取最後由本 load_batch_id 實際改變的列。
CREATE TEMP TABLE core_batch_source ON COMMIT DROP AS
SELECT
    source.source_transaction_id,
    MAKE_DATE(
        source.transaction_year,
        source.transaction_month,
        1
    ) AS transaction_month,
    source.county_id,
    source.town_id,
    source.city,
    source.district,
    location.location_id,
    location.city AS mapped_city,
    location.district AS mapped_district,
    source.building_type,
    building_type.building_type_id,
    source.transaction_type,
    source.total_price_ntd,
    source.unit_price_ntd_m2
FROM core_batch_context AS context
JOIN staging.stg_transactions AS source
    ON source.load_batch_id = context.load_batch_id
LEFT JOIN core.dim_location AS location
    ON source.county_id = location.county_id
    AND source.town_id = location.town_id
LEFT JOIN core.dim_building_type AS building_type
    ON (
        source.transaction_type = '土地'
        AND building_type.building_type_id = 0
    )
    OR (
        source.transaction_type <> '土地'
        AND source.building_type IS NULL
        AND building_type.building_type_id = 1
    )
    OR (
        source.transaction_type <> '土地'
        AND source.building_type IS NOT NULL
        AND building_type.member_type = 'official'
        AND source.building_type = building_type.building_type_name
    )
WHERE NOT context.already_success;

CREATE UNIQUE INDEX core_batch_source_transaction_id_idx
    ON core_batch_source (source_transaction_id);
ANALYZE core_batch_source;

-- mapping 統計只掃描本批來源；維度表本身很小，可以在每批做阻擋式完整檢查。
CREATE TEMP TABLE core_batch_mapping_stats ON COMMIT DROP AS
SELECT
    count(*)::BIGINT AS source_rows,
    count(*) FILTER (
        WHERE location_id IS NULL
    )::BIGINT AS unmapped_location_rows,
    count(*) FILTER (
        WHERE location_id IS NOT NULL
          AND ROW(city, district)
              IS DISTINCT FROM ROW(mapped_city, mapped_district)
    )::BIGINT AS mismatched_location_name_rows,
    count(*) FILTER (
        WHERE building_type_id IS NULL
    )::BIGINT AS unmapped_building_type_rows,
    (
        SELECT count(*)::BIGINT
        FROM core.dim_location
    ) AS location_rows,
    (
        SELECT count(*)::BIGINT
        FROM core.dim_location
        WHERE county_id !~ '^[0-9]{5}$'
           OR town_id !~ '^[0-9]{8}$'
           OR city <> BTRIM(city)
           OR city = ''
           OR district <> BTRIM(district)
           OR district = ''
    ) AS invalid_location_rows,
    (
        SELECT count(*)::BIGINT
        FROM core.dim_building_type
    ) AS building_type_rows,
    (
        SELECT count(*)::BIGINT
        FROM core.dim_building_type
        WHERE building_type_id BETWEEN 0 AND 13
    ) AS building_type_ids_in_range,
    (
        SELECT count(*)::BIGINT
        FROM core.dim_building_type
        WHERE
            (
                building_type_id = 0
                AND building_type_name = '不適用'
                AND member_type = 'not_applicable'
            )
            OR (
                building_type_id = 1
                AND building_type_name = '未提供'
                AND member_type = 'missing'
            )
            OR (
                building_type_id BETWEEN 2 AND 13
                AND member_type = 'official'
            )
    ) AS valid_building_type_rows
FROM core_batch_source;

DO $core_batch_mapping_gate$
DECLARE
    v_load_batch_id TEXT;
    v_already_success BOOLEAN;
    v_expected_source_rows BIGINT;
    v_source_rows BIGINT;
    v_unmapped_location_rows BIGINT;
    v_mismatched_location_name_rows BIGINT;
    v_unmapped_building_type_rows BIGINT;
    v_location_rows BIGINT;
    v_invalid_location_rows BIGINT;
    v_building_type_rows BIGINT;
    v_building_type_ids_in_range BIGINT;
    v_valid_building_type_rows BIGINT;
BEGIN
    SELECT load_batch_id, already_success, expected_source_rows
    INTO v_load_batch_id, v_already_success, v_expected_source_rows
    FROM core_batch_context;

    IF v_already_success THEN
        RETURN;
    END IF;

    SELECT
        source_rows,
        unmapped_location_rows,
        mismatched_location_name_rows,
        unmapped_building_type_rows,
        location_rows,
        invalid_location_rows,
        building_type_rows,
        building_type_ids_in_range,
        valid_building_type_rows
    INTO
        v_source_rows,
        v_unmapped_location_rows,
        v_mismatched_location_name_rows,
        v_unmapped_building_type_rows,
        v_location_rows,
        v_invalid_location_rows,
        v_building_type_rows,
        v_building_type_ids_in_range,
        v_valid_building_type_rows
    FROM core_batch_mapping_stats;

    IF v_source_rows <> v_expected_source_rows THEN
        RAISE EXCEPTION
            'Core batch source reconciliation failed: batch %, expected %, found %. A later staging batch may have replaced this batch ID; use full repair instead',
            v_load_batch_id,
            v_expected_source_rows,
            v_source_rows;
    END IF;

    IF v_location_rows <> 368
       OR v_invalid_location_rows <> 0 THEN
        RAISE EXCEPTION
            'Core location dimension validation failed: rows %, invalid rows %; expected 368/0',
            v_location_rows,
            v_invalid_location_rows;
    END IF;

    IF v_unmapped_location_rows <> 0
       OR v_mismatched_location_name_rows <> 0 THEN
        RAISE EXCEPTION
            'Core batch location mapping failed: unmapped %, mismatched names %',
            v_unmapped_location_rows,
            v_mismatched_location_name_rows;
    END IF;

    IF v_building_type_rows <> 14
       OR v_building_type_ids_in_range <> 14
       OR v_valid_building_type_rows <> 14
       OR v_unmapped_building_type_rows <> 0 THEN
        RAISE EXCEPTION
            'Core batch building type validation failed: rows %, IDs 0-13 %, valid definitions %, unmapped %',
            v_building_type_rows,
            v_building_type_ids_in_range,
            v_valid_building_type_rows,
            v_unmapped_building_type_rows;
    END IF;
END
$core_batch_mapping_gate$;

-- 只以 core 實際保存的欄位分類。staging 若只改了格局、面積或其他非 core
-- 欄位，這裡會歸類為 unchanged，不重寫 fact。
CREATE TEMP TABLE core_batch_classification ON COMMIT DROP AS
SELECT
    source.*,
    CASE
        WHEN fact.source_transaction_id IS NULL THEN 'inserted'
        WHEN ROW(
            fact.transaction_month,
            fact.location_id,
            fact.building_type_id,
            fact.transaction_type,
            fact.total_price_ntd,
            fact.unit_price_ntd_m2
        ) IS DISTINCT FROM ROW(
            source.transaction_month,
            source.location_id,
            source.building_type_id,
            source.transaction_type,
            source.total_price_ntd,
            source.unit_price_ntd_m2
        ) THEN 'updated'
        ELSE 'unchanged'
    END AS change_type
FROM core_batch_source AS source
LEFT JOIN core.fact_transactions AS fact
    USING (source_transaction_id);

CREATE UNIQUE INDEX core_batch_classification_transaction_id_idx
    ON core_batch_classification (source_transaction_id);
ANALYZE core_batch_classification;

CREATE TEMP TABLE core_batch_stats ON COMMIT DROP AS
SELECT
    count(*)::BIGINT AS source_rows,
    count(*) FILTER (
        WHERE change_type = 'inserted'
    )::BIGINT AS rows_inserted,
    count(*) FILTER (
        WHERE change_type = 'updated'
    )::BIGINT AS rows_updated,
    count(*) FILTER (
        WHERE change_type = 'unchanged'
    )::BIGINT AS rows_unchanged
FROM core_batch_classification;

CREATE TEMP TABLE core_batch_apply_stats ON COMMIT DROP AS
WITH applied_rows AS (
    INSERT INTO core.fact_transactions AS fact (
        source_transaction_id,
        transaction_month,
        location_id,
        building_type_id,
        transaction_type,
        total_price_ntd,
        unit_price_ntd_m2
    )
    SELECT
        source_transaction_id,
        transaction_month,
        location_id,
        building_type_id,
        transaction_type,
        total_price_ntd,
        unit_price_ntd_m2
    FROM core_batch_classification
    WHERE change_type IN ('inserted', 'updated')
    ON CONFLICT (source_transaction_id)
    DO UPDATE SET
        transaction_month = EXCLUDED.transaction_month,
        location_id = EXCLUDED.location_id,
        building_type_id = EXCLUDED.building_type_id,
        transaction_type = EXCLUDED.transaction_type,
        total_price_ntd = EXCLUDED.total_price_ntd,
        unit_price_ntd_m2 = EXCLUDED.unit_price_ntd_m2
    WHERE ROW(
        fact.transaction_month,
        fact.location_id,
        fact.building_type_id,
        fact.transaction_type,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    ) IS DISTINCT FROM ROW(
        EXCLUDED.transaction_month,
        EXCLUDED.location_id,
        EXCLUDED.building_type_id,
        EXCLUDED.transaction_type,
        EXCLUDED.total_price_ntd,
        EXCLUDED.unit_price_ntd_m2
    )
    RETURNING source_transaction_id
)
SELECT count(*)::BIGINT AS rows_applied
FROM applied_rows;

-- 寫入後只重查本批 ID，確認 fact 最終值與已 mapping 的來源完全一致。
CREATE TEMP TABLE core_batch_final_stats ON COMMIT DROP AS
SELECT
    count(*) FILTER (
        WHERE fact.source_transaction_id IS NULL
           OR ROW(
                fact.transaction_month,
                fact.location_id,
                fact.building_type_id,
                fact.transaction_type,
                fact.total_price_ntd,
                fact.unit_price_ntd_m2
           ) IS DISTINCT FROM ROW(
                source.transaction_month,
                source.location_id,
                source.building_type_id,
                source.transaction_type,
                source.total_price_ntd,
                source.unit_price_ntd_m2
           )
    )::BIGINT AS final_mismatch_rows
FROM core_batch_source AS source
LEFT JOIN core.fact_transactions AS fact
    USING (source_transaction_id);

DO $core_batch_reconciliation_gate$
DECLARE
    v_already_success BOOLEAN;
    v_expected_source_rows BIGINT;
    v_source_rows BIGINT;
    v_rows_inserted BIGINT;
    v_rows_updated BIGINT;
    v_rows_unchanged BIGINT;
    v_rows_applied BIGINT;
    v_final_mismatch_rows BIGINT;
BEGIN
    SELECT already_success, expected_source_rows
    INTO v_already_success, v_expected_source_rows
    FROM core_batch_context;

    IF v_already_success THEN
        RETURN;
    END IF;

    SELECT
        source_rows,
        rows_inserted,
        rows_updated,
        rows_unchanged
    INTO
        v_source_rows,
        v_rows_inserted,
        v_rows_updated,
        v_rows_unchanged
    FROM core_batch_stats;

    SELECT rows_applied
    INTO v_rows_applied
    FROM core_batch_apply_stats;

    SELECT final_mismatch_rows
    INTO v_final_mismatch_rows
    FROM core_batch_final_stats;

    IF v_source_rows <> v_expected_source_rows
       OR v_source_rows <>
            v_rows_inserted + v_rows_updated + v_rows_unchanged THEN
        RAISE EXCEPTION
            'Core batch classification reconciliation failed: source %, expected %, inserted %, updated %, unchanged %',
            v_source_rows,
            v_expected_source_rows,
            v_rows_inserted,
            v_rows_updated,
            v_rows_unchanged;
    END IF;

    IF v_rows_applied <> v_rows_inserted + v_rows_updated THEN
        RAISE EXCEPTION
            'Core batch write reconciliation failed: applied %, expected %',
            v_rows_applied,
            v_rows_inserted + v_rows_updated;
    END IF;

    IF v_final_mismatch_rows <> 0 THEN
        RAISE EXCEPTION
            'Core batch final-value validation failed: mismatched rows %',
            v_final_mismatch_rows;
    END IF;
END
$core_batch_reconciliation_gate$;

CREATE TEMP TABLE core_batch_validation_result ON COMMIT DROP AS
SELECT jsonb_build_object(
    'status', 'success',
    'mode', 'incremental_upsert_only',
    'load_batch_id', context.load_batch_id,
    'source_rows', stats.source_rows,
    'expected_source_rows', context.expected_source_rows,
    'rows_inserted', stats.rows_inserted,
    'rows_updated', stats.rows_updated,
    'rows_unchanged', stats.rows_unchanged,
    'rows_applied', applied.rows_applied,
    'location_rows', mapping.location_rows,
    'unmapped_location_rows', mapping.unmapped_location_rows,
    'mismatched_location_name_rows',
        mapping.mismatched_location_name_rows,
    'building_type_rows', mapping.building_type_rows,
    'unmapped_building_type_rows', mapping.unmapped_building_type_rows,
    'final_mismatch_rows', final.final_mismatch_rows,
    'source_transaction_id_constraint', 'PRIMARY KEY'
) AS summary
FROM core_batch_context AS context
CROSS JOIN core_batch_stats AS stats
CROSS JOIN core_batch_apply_stats AS applied
CROSS JOIN core_batch_mapping_stats AS mapping
CROSS JOIN core_batch_final_stats AS final;

CREATE TEMP TABLE core_sync_write_stats ON COMMIT DROP AS
WITH updated_run AS (
    UPDATE core.sync_runs AS sync_run
    SET
        status = 'success',
        source_rows = stats.source_rows,
        rows_inserted = stats.rows_inserted,
        rows_updated = stats.rows_updated,
        rows_unchanged = stats.rows_unchanged,
        rows_applied = applied.rows_applied,
        validation_summary = validation.summary,
        error_message = NULL,
        completed_at = clock_timestamp()
    FROM core_batch_context AS context
    CROSS JOIN core_batch_stats AS stats
    CROSS JOIN core_batch_apply_stats AS applied
    CROSS JOIN core_batch_validation_result AS validation
    WHERE sync_run.load_batch_id = context.load_batch_id
      AND sync_run.status = 'running'
      AND NOT context.already_success
    RETURNING sync_run.load_batch_id
)
SELECT count(*)::BIGINT AS updated_rows
FROM updated_run;

DO $core_sync_status_gate$
DECLARE
    v_already_success BOOLEAN;
    v_updated_rows BIGINT;
BEGIN
    SELECT already_success
    INTO v_already_success
    FROM core_batch_context;

    SELECT updated_rows
    INTO v_updated_rows
    FROM core_sync_write_stats;

    IF (v_already_success AND v_updated_rows <> 0)
       OR (NOT v_already_success AND v_updated_rows <> 1) THEN
        RAISE EXCEPTION
            'Core sync status publication failed: already success %, updated rows %',
            v_already_success,
            v_updated_rows;
    END IF;
END
$core_sync_status_gate$;

-- 最後一個 result set 是未來 core loader 的唯一正式輸出；成功批次重跑時
-- 會取得同一份已提交統計。
SELECT
    load_batch_id,
    status,
    source_rows,
    rows_inserted,
    rows_updated,
    rows_unchanged,
    rows_applied,
    attempt_count,
    validation_summary::TEXT,
    started_at,
    completed_at
FROM core.sync_runs
WHERE load_batch_id = :'load_batch_id';

COMMIT;
