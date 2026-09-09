-- 以 core.fact_transactions 獨立重算的三個月滾動值驗證
-- analytics.district_rolling_3m；本檔只建立暫存工作表，不修改永久資料。

BEGIN;

CREATE TEMP TABLE district_rolling_validation_range
ON COMMIT DROP
AS
SELECT
    DATE '2012-08-01' AS analysis_start_month,
    DATE '2012-10-01' AS first_anchor_month,
    MIN(national.month_start)::DATE AS national_start_month,
    MAX(national.month_start)::DATE AS published_end_month,
    (
        SELECT MIN(district.month_start)::DATE
        FROM analytics.district_rolling_3m AS district
    ) AS district_start_month,
    (
        SELECT MAX(district.month_start)::DATE
        FROM analytics.district_rolling_3m AS district
    ) AS district_end_month
FROM analytics.national_monthly_kpi AS national;

-- 以目前 Core 維度作為預期鄉鎮市區成員，不寫死地區筆數。
CREATE TEMP TABLE district_rolling_validation_locations
ON COMMIT DROP
AS
SELECT
    location.location_id,
    location.county_id,
    location.town_id,
    location.city,
    location.district
FROM core.dim_location AS location;

-- 從交易資料獨立重建預期的錨點／地區骨架、滾動基礎 KPI 與 YoY。
CREATE TEMP TABLE district_rolling_expected_kpi
ON COMMIT DROP
AS
WITH anchor_months AS (
    SELECT generated_anchor.anchor_month::DATE AS anchor_month
    FROM district_rolling_validation_range AS validation_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        validation_range.first_anchor_month,
        validation_range.published_end_month,
        INTERVAL '1 month'
    ) AS generated_anchor(anchor_month)
),

anchor_location_spine AS (
    SELECT
        anchor.anchor_month,
        location.location_id,
        location.county_id,
        location.town_id,
        location.city,
        location.district
    FROM anchor_months AS anchor
    CROSS JOIN district_rolling_validation_locations AS location
),

eligible_transactions AS (
    SELECT
        fact.transaction_month,
        fact.location_id,
        fact.source_transaction_id,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM core.fact_transactions AS fact
    CROSS JOIN district_rolling_validation_range AS validation_range
    WHERE fact.transaction_month
              BETWEEN validation_range.analysis_start_month
                  AND validation_range.published_end_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
),

rolling_aggregation AS (
    SELECT
        anchor.anchor_month,
        transaction.location_id,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY transaction.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS median_unit_price_10k_ping,

        AVG(transaction.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS avg_unit_price_10k_ping,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY transaction.total_price_ntd)
            / 10000.0
            AS median_total_price_10k,

        AVG(transaction.total_price_ntd)
            / 10000.0
            AS avg_total_price_10k,

        COUNT(transaction.source_transaction_id)::BIGINT
            AS price_complete_transaction_count

    FROM anchor_months AS anchor
    JOIN eligible_transactions AS transaction
        ON transaction.transaction_month BETWEEN
           (anchor.anchor_month - INTERVAL '2 months')::DATE
           AND anchor.anchor_month
    GROUP BY
        anchor.anchor_month,
        transaction.location_id
),

complete_district_months AS (
    SELECT
        spine.anchor_month AS month_start,
        spine.county_id,
        spine.town_id,
        spine.city,
        spine.district,

        aggregation.median_unit_price_10k_ping::NUMERIC(18,6)
            AS median_unit_price_10k_ping,
        aggregation.avg_unit_price_10k_ping::NUMERIC(18,6)
            AS avg_unit_price_10k_ping,
        aggregation.median_total_price_10k::NUMERIC(18,6)
            AS median_total_price_10k,
        aggregation.avg_total_price_10k::NUMERIC(18,6)
            AS avg_total_price_10k,

        COALESCE(
            aggregation.price_complete_transaction_count,
            0::BIGINT
        ) AS price_complete_transaction_count

    FROM anchor_location_spine AS spine
    LEFT JOIN rolling_aggregation AS aggregation
        ON aggregation.anchor_month = spine.anchor_month
       AND aggregation.location_id = spine.location_id
),

year_pairs AS (
    SELECT
        current_anchor.*,

        previous_year.median_unit_price_10k_ping
            AS previous_year_median_unit_price_10k_ping,
        previous_year.avg_unit_price_10k_ping
            AS previous_year_avg_unit_price_10k_ping,
        previous_year.median_total_price_10k
            AS previous_year_median_total_price_10k,
        previous_year.avg_total_price_10k
            AS previous_year_avg_total_price_10k,
        previous_year.price_complete_transaction_count
            AS previous_year_price_complete_transaction_count

    FROM complete_district_months AS current_anchor
    LEFT JOIN complete_district_months AS previous_year
        ON previous_year.town_id = current_anchor.town_id
       AND previous_year.month_start =
           (current_anchor.month_start - INTERVAL '1 year')::DATE
)

SELECT
    month_start,
    county_id,
    town_id,
    city,
    district,
    median_unit_price_10k_ping,
    avg_unit_price_10k_ping,
    median_total_price_10k,
    avg_total_price_10k,
    price_complete_transaction_count,

    CASE
        WHEN median_unit_price_10k_ping IS NULL
          OR previous_year_median_unit_price_10k_ping IS NULL
        THEN NULL
        ELSE (
            median_unit_price_10k_ping
            / previous_year_median_unit_price_10k_ping
            - 1
        )::NUMERIC(14,8)
    END AS median_unit_price_yoy,

    CASE
        WHEN avg_unit_price_10k_ping IS NULL
          OR previous_year_avg_unit_price_10k_ping IS NULL
        THEN NULL
        ELSE (
            avg_unit_price_10k_ping
            / previous_year_avg_unit_price_10k_ping
            - 1
        )::NUMERIC(14,8)
    END AS avg_unit_price_yoy,

    CASE
        WHEN median_total_price_10k IS NULL
          OR previous_year_median_total_price_10k IS NULL
        THEN NULL
        ELSE (
            median_total_price_10k
            / previous_year_median_total_price_10k
            - 1
        )::NUMERIC(14,8)
    END AS median_total_price_yoy,

    CASE
        WHEN avg_total_price_10k IS NULL
          OR previous_year_avg_total_price_10k IS NULL
        THEN NULL
        ELSE (
            avg_total_price_10k
            / previous_year_avg_total_price_10k
            - 1
        )::NUMERIC(14,8)
    END AS avg_total_price_yoy,

    CASE
        WHEN previous_year_price_complete_transaction_count IS NULL
          OR previous_year_price_complete_transaction_count = 0
        THEN NULL
        ELSE (
            price_complete_transaction_count::NUMERIC
            / previous_year_price_complete_transaction_count
            - 1
        )::NUMERIC(14,8)
    END AS price_complete_transaction_count_yoy

FROM year_pairs;

-- 1. 日期範圍、動態地區筆數與完整骨架涵蓋範圍。
WITH expected_spine AS (
    SELECT
        generated_anchor.anchor_month::DATE AS month_start,
        location.town_id
    FROM district_rolling_validation_range AS validation_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        validation_range.first_anchor_month,
        validation_range.published_end_month,
        INTERVAL '1 month'
    ) AS generated_anchor(anchor_month)
    CROSS JOIN district_rolling_validation_locations AS location
),

missing_rows AS (
    SELECT COUNT(*)::BIGINT AS row_count
    FROM expected_spine AS expected
    LEFT JOIN analytics.district_rolling_3m AS actual
        USING (month_start, town_id)
    WHERE actual.month_start IS NULL
),

unexpected_rows AS (
    SELECT COUNT(*)::BIGINT AS row_count
    FROM analytics.district_rolling_3m AS actual
    LEFT JOIN expected_spine AS expected
        USING (month_start, town_id)
    WHERE expected.month_start IS NULL
),

name_mismatches AS (
    SELECT COUNT(*)::BIGINT AS row_count
    FROM analytics.district_rolling_3m AS actual
    JOIN district_rolling_validation_locations AS location
        USING (town_id)
    WHERE actual.county_id IS DISTINCT FROM location.county_id
       OR actual.city IS DISTINCT FROM location.city
       OR actual.district IS DISTINCT FROM location.district
)

SELECT
    validation_range.analysis_start_month,
    validation_range.first_anchor_month,
    validation_range.national_start_month,
    validation_range.published_end_month,
    validation_range.district_start_month,
    validation_range.district_end_month,
    (
        SELECT COUNT(*)::BIGINT
        FROM district_rolling_validation_locations
    ) AS location_count,
    (
        SELECT COUNT(DISTINCT expected.month_start)::BIGINT
        FROM expected_spine AS expected
    ) AS expected_anchor_month_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM expected_spine
    ) AS expected_row_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM analytics.district_rolling_3m
    ) AS actual_row_count,
    missing_rows.row_count AS missing_row_count,
    unexpected_rows.row_count AS unexpected_row_count,
    name_mismatches.row_count AS location_name_mismatch_count
FROM district_rolling_validation_range AS validation_range
CROSS JOIN missing_rows
CROSS JOIN unexpected_rows
CROSS JOIN name_mismatches;

-- 2. 基礎 KPI 與 YoY 差異筆數。預期值已採用目標儲存精度，因此刻意使用
-- 可安全處理 NULL 的精確比較。
SELECT
    COUNT(*) FILTER (
        WHERE expected.month_start IS NULL
           OR actual.month_start IS NULL
    )::BIGINT AS key_coverage_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.median_unit_price_10k_ping
              IS DISTINCT FROM expected.median_unit_price_10k_ping
    )::BIGINT AS median_unit_price_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.avg_unit_price_10k_ping
              IS DISTINCT FROM expected.avg_unit_price_10k_ping
    )::BIGINT AS avg_unit_price_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.median_total_price_10k
              IS DISTINCT FROM expected.median_total_price_10k
    )::BIGINT AS median_total_price_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.avg_total_price_10k
              IS DISTINCT FROM expected.avg_total_price_10k
    )::BIGINT AS avg_total_price_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.price_complete_transaction_count
              IS DISTINCT FROM expected.price_complete_transaction_count
    )::BIGINT AS transaction_count_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.median_unit_price_yoy
              IS DISTINCT FROM expected.median_unit_price_yoy
    )::BIGINT AS median_unit_price_yoy_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.avg_unit_price_yoy
              IS DISTINCT FROM expected.avg_unit_price_yoy
    )::BIGINT AS avg_unit_price_yoy_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.median_total_price_yoy
              IS DISTINCT FROM expected.median_total_price_yoy
    )::BIGINT AS median_total_price_yoy_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.avg_total_price_yoy
              IS DISTINCT FROM expected.avg_total_price_yoy
    )::BIGINT AS avg_total_price_yoy_mismatch_count,

    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND actual.month_start IS NOT NULL
          AND actual.price_complete_transaction_count_yoy
              IS DISTINCT FROM expected.price_complete_transaction_count_yoy
    )::BIGINT AS transaction_count_yoy_mismatch_count

FROM district_rolling_expected_kpi AS expected
FULL JOIN analytics.district_rolling_3m AS actual
    USING (month_start, town_id);

-- 3. 跨粒度對帳：縣市內各鄉鎮市區滾動案件數總和，必須等於 City Analytics
-- 該縣市三個月份的案件數總和。
WITH anchor_months AS (
    SELECT generated_anchor.anchor_month::DATE AS anchor_month
    FROM district_rolling_validation_range AS validation_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        validation_range.first_anchor_month,
        validation_range.published_end_month,
        INTERVAL '1 month'
    ) AS generated_anchor(anchor_month)
),

counties AS (
    SELECT DISTINCT location.county_id
    FROM district_rolling_validation_locations AS location
),

expected_city_rolling_counts AS (
    SELECT
        anchor.anchor_month,
        county.county_id,
        COALESCE(
            SUM(city.price_complete_transaction_count),
            0
        )::BIGINT AS city_rolling_transaction_count
    FROM anchor_months AS anchor
    CROSS JOIN counties AS county
    LEFT JOIN analytics.city_monthly_kpi AS city
        ON city.county_id = county.county_id
       AND city.month_start BETWEEN
           (anchor.anchor_month - INTERVAL '2 months')::DATE
           AND anchor.anchor_month
    GROUP BY
        anchor.anchor_month,
        county.county_id
),

district_rolling_counts AS (
    SELECT
        district.month_start AS anchor_month,
        district.county_id,
        SUM(district.price_complete_transaction_count)::BIGINT
            AS district_rolling_transaction_count
    FROM analytics.district_rolling_3m AS district
    GROUP BY
        district.month_start,
        district.county_id
)

SELECT
    COUNT(*) FILTER (
        WHERE city.anchor_month IS NULL
           OR district.anchor_month IS NULL
           OR city.city_rolling_transaction_count
              IS DISTINCT FROM district.district_rolling_transaction_count
    )::BIGINT AS city_district_transaction_count_mismatch_count
FROM expected_city_rolling_counts AS city
FULL JOIN district_rolling_counts AS district
    USING (anchor_month, county_id);

-- 4. 最多回傳 50 筆具體 KPI 差異供診斷。
SELECT
    COALESCE(expected.month_start, actual.month_start) AS month_start,
    COALESCE(expected.town_id, actual.town_id) AS town_id,
    expected.city AS expected_city,
    actual.city AS actual_city,
    expected.district AS expected_district,
    actual.district AS actual_district,
    expected.median_unit_price_10k_ping
        AS expected_median_unit_price_10k_ping,
    actual.median_unit_price_10k_ping
        AS actual_median_unit_price_10k_ping,
    expected.price_complete_transaction_count
        AS expected_price_complete_transaction_count,
    actual.price_complete_transaction_count
        AS actual_price_complete_transaction_count,
    expected.median_unit_price_yoy
        AS expected_median_unit_price_yoy,
    actual.median_unit_price_yoy
        AS actual_median_unit_price_yoy
FROM district_rolling_expected_kpi AS expected
FULL JOIN analytics.district_rolling_3m AS actual
    USING (month_start, town_id)
WHERE expected.month_start IS NULL
   OR actual.month_start IS NULL
   OR actual.county_id IS DISTINCT FROM expected.county_id
   OR actual.city IS DISTINCT FROM expected.city
   OR actual.district IS DISTINCT FROM expected.district
   OR actual.median_unit_price_10k_ping
      IS DISTINCT FROM expected.median_unit_price_10k_ping
   OR actual.avg_unit_price_10k_ping
      IS DISTINCT FROM expected.avg_unit_price_10k_ping
   OR actual.median_total_price_10k
      IS DISTINCT FROM expected.median_total_price_10k
   OR actual.avg_total_price_10k
      IS DISTINCT FROM expected.avg_total_price_10k
   OR actual.price_complete_transaction_count
      IS DISTINCT FROM expected.price_complete_transaction_count
   OR actual.median_unit_price_yoy
      IS DISTINCT FROM expected.median_unit_price_yoy
   OR actual.avg_unit_price_yoy
      IS DISTINCT FROM expected.avg_unit_price_yoy
   OR actual.median_total_price_yoy
      IS DISTINCT FROM expected.median_total_price_yoy
   OR actual.avg_total_price_yoy
      IS DISTINCT FROM expected.avg_total_price_yoy
   OR actual.price_complete_transaction_count_yoy
      IS DISTINCT FROM expected.price_complete_transaction_count_yoy
ORDER BY
    month_start,
    town_id
LIMIT 50;

COMMIT;
