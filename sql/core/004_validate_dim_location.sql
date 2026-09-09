-- 完整行政區參照契約：location_rows、unique_county_town_ids 與
-- unique_town_ids 都必須是 368；unique_county_ids、unique_cities 與
-- unique_county_city_pairs 都必須是 22；invalid_reference_rows 必須是 0。
SELECT
    COUNT(*) AS location_rows,
    COUNT(DISTINCT (county_id, town_id)) AS unique_county_town_ids,
    COUNT(DISTINCT town_id) AS unique_town_ids,
    COUNT(DISTINCT county_id) AS unique_county_ids,
    COUNT(DISTINCT city) AS unique_cities,
    COUNT(DISTINCT (county_id, city)) AS unique_county_city_pairs,
    COUNT(*) FILTER (
        WHERE county_id !~ '^[0-9]{5}$'
           OR town_id !~ '^[0-9]{8}$'
           OR city <> BTRIM(city)
           OR city = ''
           OR district <> BTRIM(district)
           OR district = ''
    ) AS invalid_reference_rows
FROM core.dim_location;

-- 預期：unmapped_rows = 0。
SELECT
    COUNT(*) AS unmapped_rows
FROM staging.stg_transactions AS source
LEFT JOIN core.dim_location AS location
    ON source.town_id = location.town_id
    AND source.county_id = location.county_id
WHERE location.location_id IS NULL;

-- 預期：mismatched_name_rows = 0。
SELECT
    COUNT(*) AS mismatched_name_rows
FROM staging.stg_transactions AS source
LEFT JOIN core.dim_location AS location
    ON source.town_id = location.town_id
    AND source.county_id = location.county_id
    AND source.city = location.city
    AND source.district = location.district
WHERE location.location_id IS NULL;
