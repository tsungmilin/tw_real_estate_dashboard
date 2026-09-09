-- 目前已驗證快照預期：fact_rows = staging_rows = 4,261,466，
-- unique_transaction_ids = 4,261,466。
SELECT
    COUNT(*) AS fact_rows,
    COUNT(DISTINCT source_transaction_id) AS unique_transaction_ids,
    (
        SELECT COUNT(*)
        FROM staging.stg_transactions
    ) AS staging_rows
FROM core.fact_transactions;

-- 預期：missing_fact_rows = 0、stale_fact_rows = 0。
SELECT
    COUNT(*) FILTER (
        WHERE fact.source_transaction_id IS NULL
    ) AS missing_fact_rows,
    (
        SELECT COUNT(*)
        FROM core.fact_transactions AS fact
        LEFT JOIN staging.stg_transactions AS source
            ON source.source_transaction_id =
                fact.source_transaction_id
        WHERE source.source_transaction_id IS NULL
    ) AS stale_fact_rows
FROM staging.stg_transactions AS source
LEFT JOIN core.fact_transactions AS fact
    ON fact.source_transaction_id = source.source_transaction_id;

-- 預期：unmapped_location_rows = 0、unmapped_building_type_rows = 0。
SELECT
    COUNT(*) FILTER (
        WHERE location.location_id IS NULL
    ) AS unmapped_location_rows,
    COUNT(*) FILTER (
        WHERE building_type.building_type_id IS NULL
    ) AS unmapped_building_type_rows
FROM core.fact_transactions AS fact
LEFT JOIN core.dim_location AS location
    ON fact.location_id = location.location_id
LEFT JOIN core.dim_building_type AS building_type
    ON fact.building_type_id = building_type.building_type_id;

-- 目前已驗證快照預期：land_rows = 1,141,664、
-- housing_rows = 3,119,802、not_applicable_rows = 1,141,664、
-- missing_building_type_rows = 0、official_building_type_rows = 3,119,802。
SELECT
    COUNT(*) FILTER (
        WHERE transaction_type = '土地'
    ) AS land_rows,
    COUNT(*) FILTER (
        WHERE transaction_type IN (
            '房地(土地+建物)',
            '房地(土地+建物)+車位'
        )
    ) AS housing_rows,
    COUNT(*) FILTER (
        WHERE building_type_id = 0
    ) AS not_applicable_rows,
    COUNT(*) FILTER (
        WHERE building_type_id = 1
    ) AS missing_building_type_rows,
    COUNT(*) FILTER (
        WHERE building_type_id BETWEEN 2 AND 13
    ) AS official_building_type_rows
FROM core.fact_transactions;

-- 預期：building_type_rule_violations = 0。
SELECT
    COUNT(*) AS building_type_rule_violations
FROM core.fact_transactions
WHERE
    (
        transaction_type = '土地'
        AND building_type_id <> 0
    )
    OR (
        transaction_type IN (
            '房地(土地+建物)',
            '房地(土地+建物)+車位'
        )
        AND building_type_id = 0
    );

-- KPI 使用總價與單價皆完整的同一批房屋交易。
-- 目前已驗證快照預期：
-- housing_rows = 3,119,802、complete_price_rows = 3,119,648、
-- incomplete_price_rows = 154、missing_unit_price_only_rows = 51、
-- missing_total_price_only_rows = 0、missing_both_prices_rows = 103。
SELECT
    COUNT(*) AS housing_rows,
    COUNT(*) FILTER (
        WHERE
            total_price_ntd IS NOT NULL
            AND unit_price_ntd_m2 IS NOT NULL
    ) AS complete_price_rows,
    COUNT(*) FILTER (
        WHERE
            total_price_ntd IS NULL
            OR unit_price_ntd_m2 IS NULL
    ) AS incomplete_price_rows,
    COUNT(*) FILTER (
        WHERE
            total_price_ntd IS NOT NULL
            AND unit_price_ntd_m2 IS NULL
    ) AS missing_unit_price_only_rows,
    COUNT(*) FILTER (
        WHERE
            total_price_ntd IS NULL
            AND unit_price_ntd_m2 IS NOT NULL
    ) AS missing_total_price_only_rows,
    COUNT(*) FILTER (
        WHERE
            total_price_ntd IS NULL
            AND unit_price_ntd_m2 IS NULL
    ) AS missing_both_prices_rows
FROM core.fact_transactions
WHERE transaction_type IN (
    '房地(土地+建物)',
    '房地(土地+建物)+車位'
);
