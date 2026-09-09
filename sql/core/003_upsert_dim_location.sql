BEGIN;

-- Validate and UPSERT the versioned location reference into core.dim_location.
-- Run from the project root; writes persistent dimension rows and returns no result set.
-- dim_location 的權威來源是專案內已驗證的 368 筆行政區參照資料，不由
-- 目前交易曾出現的行政區推導。此相對路徑以 psql 的執行目錄為基準；
-- 請從專案根目錄執行本檔案。
CREATE TEMP TABLE location_reference_buffer (
    legacy_county_code TEXT NOT NULL,
    legacy_town_code TEXT NOT NULL,
    county_id TEXT NOT NULL,
    town_id TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT NOT NULL
) ON COMMIT DROP;

\copy location_reference_buffer (legacy_county_code, legacy_town_code, county_id, town_id, city, district) FROM 'data/reference/location_lookup.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

DO $location_reference_gate$
DECLARE
    v_rows BIGINT;
    v_unique_county_town_ids BIGINT;
    v_unique_town_ids BIGINT;
    v_unique_county_ids BIGINT;
    v_unique_cities BIGINT;
    v_unique_county_city_pairs BIGINT;
    v_invalid_rows BIGINT;
BEGIN
    SELECT
        count(*),
        count(DISTINCT (county_id, town_id)),
        count(DISTINCT town_id),
        count(DISTINCT county_id),
        count(DISTINCT city),
        count(DISTINCT (county_id, city)),
        count(*) FILTER (
            WHERE county_id !~ '^[0-9]{5}$'
               OR town_id !~ '^[0-9]{8}$'
               OR city <> btrim(city)
               OR city = ''
               OR district <> btrim(district)
               OR district = ''
        )
    INTO
        v_rows,
        v_unique_county_town_ids,
        v_unique_town_ids,
        v_unique_county_ids,
        v_unique_cities,
        v_unique_county_city_pairs,
        v_invalid_rows
    FROM location_reference_buffer;

    IF v_rows <> 368
       OR v_unique_county_town_ids <> 368
       OR v_unique_town_ids <> 368
       OR v_unique_county_ids <> 22
       OR v_unique_cities <> 22
       OR v_unique_county_city_pairs <> 22
       OR v_invalid_rows <> 0 THEN
        RAISE EXCEPTION
            'Location reference validation failed: rows %, unique county/town IDs %, unique town IDs %, county IDs %, cities %, county/city pairs %, invalid rows %; expected 368/368/368/22/22/22/0',
            v_rows,
            v_unique_county_town_ids,
            v_unique_town_ids,
            v_unique_county_ids,
            v_unique_cities,
            v_unique_county_city_pairs,
            v_invalid_rows;
    END IF;
END
$location_reference_gate$;

INSERT INTO core.dim_location AS current_location (
    county_id,
    town_id,
    city,
    district
)
SELECT DISTINCT
    source.county_id,
    source.town_id,
    source.city,
    source.district
FROM location_reference_buffer AS source
ORDER BY
    source.county_id,
    source.town_id
ON CONFLICT (town_id)
DO UPDATE SET
    county_id = EXCLUDED.county_id,
    city = EXCLUDED.city,
    district = EXCLUDED.district
WHERE
    current_location.county_id IS DISTINCT FROM EXCLUDED.county_id
    OR current_location.city IS DISTINCT FROM EXCLUDED.city
    OR current_location.district IS DISTINCT FROM EXCLUDED.district;

COMMIT;
