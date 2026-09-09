-- Backfill or fully repair core.fact_transactions from the current staging state.
-- Requires populated dimensions; performs UPSERT-only writes and returns no result set.
BEGIN;

-- 這個檔案只供首次回填或完整修復使用：從目前 staging 狀態新增或更新
-- core 交易。未出現在 staging 的既有 core 交易一律保留，不執行刪除。
INSERT INTO core.fact_transactions AS current_fact (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    source.source_transaction_id,
    MAKE_DATE(
        source.transaction_year,
        source.transaction_month,
        1
    ) AS transaction_month,
    location.location_id,
    CASE
        WHEN source.transaction_type = '土地' THEN 0
        WHEN source.building_type IS NULL THEN 1
        ELSE building_type.building_type_id
    END AS building_type_id,
    source.transaction_type,
    source.total_price_ntd,
    source.unit_price_ntd_m2
FROM staging.stg_transactions AS source
LEFT JOIN core.dim_location AS location
    ON source.county_id = location.county_id
    AND source.town_id = location.town_id
LEFT JOIN core.dim_building_type AS building_type
    ON source.building_type = building_type.building_type_name
WHERE TRUE
ON CONFLICT (source_transaction_id)
DO UPDATE SET
    transaction_month = EXCLUDED.transaction_month,
    location_id = EXCLUDED.location_id,
    building_type_id = EXCLUDED.building_type_id,
    transaction_type = EXCLUDED.transaction_type,
    total_price_ntd = EXCLUDED.total_price_ntd,
    unit_price_ntd_m2 = EXCLUDED.unit_price_ntd_m2
WHERE ROW(
    current_fact.transaction_month,
    current_fact.location_id,
    current_fact.building_type_id,
    current_fact.transaction_type,
    current_fact.total_price_ntd,
    current_fact.unit_price_ntd_m2
) IS DISTINCT FROM ROW(
    EXCLUDED.transaction_month,
    EXCLUDED.location_id,
    EXCLUDED.building_type_id,
    EXCLUDED.transaction_type,
    EXCLUDED.total_price_ntd,
    EXCLUDED.unit_price_ntd_m2
);

COMMIT;
