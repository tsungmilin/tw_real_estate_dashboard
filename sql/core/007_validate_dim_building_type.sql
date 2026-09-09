-- 預期：total_rows = 14、official_rows = 12、
-- not_applicable_rows = 1、missing_rows = 1。
SELECT
    COUNT(*) AS total_rows,
    COUNT(*) FILTER (
        WHERE member_type = 'official'
    ) AS official_rows,
    COUNT(*) FILTER (
        WHERE member_type = 'not_applicable'
    ) AS not_applicable_rows,
    COUNT(*) FILTER (
        WHERE member_type = 'missing'
    ) AS missing_rows
FROM core.dim_building_type;

-- 預期：unmapped_building_type_rows = 0。
SELECT
    COUNT(*) AS unmapped_building_type_rows
FROM staging.stg_transactions AS source
LEFT JOIN core.dim_building_type AS building_type
    ON source.building_type = building_type.building_type_name
WHERE
    source.building_type IS NOT NULL
    AND building_type.building_type_id IS NULL;
