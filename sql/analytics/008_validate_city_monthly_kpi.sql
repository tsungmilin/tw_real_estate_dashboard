-- 依 core.fact_transactions 獨立重算值與全國表發布範圍，驗證
-- analytics.city_monthly_kpi；本檔不修改永久資料。

BEGIN;

-- 將預期的全國範圍與目前縣市範圍集中在一列。
CREATE TEMP TABLE city_monthly_validation_range
ON COMMIT DROP
AS
SELECT
    DATE '2012-08-01' AS analysis_start_month,
    MIN(national.month_start)::DATE AS national_start_month,
    MAX(national.month_start)::DATE AS national_end_month,
    (
        SELECT MIN(city.month_start)::DATE
        FROM analytics.city_monthly_kpi AS city
    ) AS city_start_month,
    (
        SELECT MAX(city.month_start)::DATE
        FROM analytics.city_monthly_kpi AS city
    ) AS city_end_month
FROM analytics.national_monthly_kpi AS national;

-- 一對一 county_id／city 契約由 Core 管理；將鄉鎮市區粒度合併為 Analytics
-- 使用的縣市成員。
CREATE TEMP TABLE city_monthly_validation_counties
ON COMMIT DROP
AS
SELECT DISTINCT
    location.county_id,
    location.city
FROM core.dim_location AS location;

-- 獨立重建預期的月份／縣市骨架、基礎 KPI 與 YoY。
CREATE TEMP TABLE city_monthly_expected_kpi
ON COMMIT DROP
AS
WITH month_spine AS (
    SELECT generated_month.month_start::DATE AS month_start
    FROM city_monthly_validation_range AS validation_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        validation_range.analysis_start_month,
        validation_range.national_end_month,
        INTERVAL '1 month'
    ) AS generated_month(month_start)
),

month_county_spine AS (
    SELECT
        month.month_start,
        county.county_id,
        county.city
    FROM month_spine AS month
    CROSS JOIN city_monthly_validation_counties AS county
),

monthly_city_aggregation AS (
    SELECT
        fact.transaction_month AS month_start,
        location.county_id,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY fact.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS median_unit_price_10k_ping,

        AVG(fact.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS avg_unit_price_10k_ping,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY fact.total_price_ntd)
            / 10000.0
            AS median_total_price_10k,

        AVG(fact.total_price_ntd)
            / 10000.0
            AS avg_total_price_10k,

        COUNT(fact.source_transaction_id)::BIGINT
            AS price_complete_transaction_count

    FROM core.fact_transactions AS fact
    JOIN core.dim_location AS location
        ON location.location_id = fact.location_id
    CROSS JOIN city_monthly_validation_range AS validation_range
    WHERE fact.transaction_month
              BETWEEN validation_range.analysis_start_month
                  AND validation_range.national_end_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
    GROUP BY
        fact.transaction_month,
        location.county_id
),

complete_city_months AS (
    SELECT
        spine.month_start,
        spine.county_id,
        spine.city,

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

    FROM month_county_spine AS spine
    LEFT JOIN monthly_city_aggregation AS aggregation
        ON aggregation.month_start = spine.month_start
       AND aggregation.county_id = spine.county_id
),

year_pairs AS (
    SELECT
        current_month.*,

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

    FROM complete_city_months AS current_month
    LEFT JOIN complete_city_months AS previous_year
        ON previous_year.county_id = current_month.county_id
       AND previous_year.month_start =
           (current_month.month_start - INTERVAL '1 year')::DATE
)

SELECT
    month_start,
    county_id,
    city,
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

-- 1. 發布範圍對齊與全國月份連續性。
WITH national_month_coverage AS (
    SELECT
        COUNT(expected_month.month_start)::BIGINT
            AS expected_national_month_count,
        COUNT(national.month_start)::BIGINT
            AS actual_national_month_count,
        COUNT(*) FILTER (
            WHERE national.month_start IS NULL
        )::BIGINT AS missing_national_month_count
    FROM city_monthly_validation_range AS validation_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        validation_range.analysis_start_month,
        validation_range.national_end_month,
        INTERVAL '1 month'
    ) AS expected_month(month_start)
    LEFT JOIN analytics.national_monthly_kpi AS national
        ON national.month_start = expected_month.month_start::DATE
)
SELECT
    validation_range.*,
    coverage.expected_national_month_count,
    coverage.actual_national_month_count,
    coverage.missing_national_month_count,
    CASE
        WHEN validation_range.national_end_month IS NULL
        THEN 'FAIL_NATIONAL_EMPTY'
        WHEN validation_range.national_start_month
             <> validation_range.analysis_start_month
        THEN 'FAIL_NATIONAL_START_MISMATCH'
        WHEN coverage.missing_national_month_count > 0
        THEN 'FAIL_NATIONAL_MONTH_GAP'
        WHEN validation_range.city_end_month IS NULL
        THEN 'FAIL_CITY_EMPTY'
        WHEN validation_range.city_start_month
             <> validation_range.analysis_start_month
        THEN 'FAIL_CITY_START_MISMATCH'
        WHEN validation_range.city_end_month
             < validation_range.national_end_month
        THEN 'FAIL_CITY_BEHIND_NATIONAL'
        WHEN validation_range.city_end_month
             > validation_range.national_end_month
        THEN 'FAIL_CITY_AHEAD_OF_NATIONAL'
        ELSE 'PASS_ALIGNED'
    END AS range_status
FROM city_monthly_validation_range AS validation_range
CROSS JOIN national_month_coverage AS coverage;

-- 2. 下游相依摘要。一對一對應屬於 Core 地理契約；縣市 Analytics 只要求其 22 個成員。
SELECT
    COUNT(*)::BIGINT AS county_member_count,
    CASE
        WHEN COUNT(*) <> 22
        THEN 'FAIL_CORE_COUNTY_CONTRACT'
        ELSE 'PASS_MAPPING'
    END AS county_mapping_status
FROM city_monthly_validation_counties;

-- 3. 月份／縣市骨架；缺少、多餘與名稱不符筆數都應為 0。
SELECT
    COUNT(expected.month_start)::BIGINT AS expected_row_count,
    COUNT(target.month_start)::BIGINT AS actual_row_count,
    COUNT(DISTINCT expected.month_start)::BIGINT AS expected_month_count,
    COUNT(DISTINCT target.month_start)::BIGINT AS actual_month_count,
    COUNT(DISTINCT expected.county_id)::BIGINT AS expected_county_count,
    COUNT(DISTINCT target.county_id)::BIGINT AS actual_county_count,
    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND target.month_start IS NULL
    )::BIGINT AS missing_row_count,
    COUNT(*) FILTER (
        WHERE expected.month_start IS NULL
          AND target.month_start IS NOT NULL
    )::BIGINT AS unexpected_row_count,
    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND target.month_start IS NOT NULL
          AND target.city IS DISTINCT FROM expected.city
    )::BIGINT AS city_name_mismatch_count
FROM city_monthly_expected_kpi AS expected
FULL JOIN analytics.city_monthly_kpi AS target
    ON target.month_start = expected.month_start
   AND target.county_id = expected.county_id;

-- 4. 基礎 KPI 對帳；所有差異筆數都應為 0。
SELECT
    COUNT(*) FILTER (
        WHERE target.month_start IS NULL
    )::BIGINT AS missing_target_row_count,
    COUNT(*) FILTER (
        WHERE target.city IS DISTINCT FROM expected.city
    )::BIGINT AS city_name_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.median_unit_price_10k_ping
              IS DISTINCT FROM expected.median_unit_price_10k_ping
    )::BIGINT AS median_unit_price_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.avg_unit_price_10k_ping
              IS DISTINCT FROM expected.avg_unit_price_10k_ping
    )::BIGINT AS avg_unit_price_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.median_total_price_10k
              IS DISTINCT FROM expected.median_total_price_10k
    )::BIGINT AS median_total_price_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.avg_total_price_10k
              IS DISTINCT FROM expected.avg_total_price_10k
    )::BIGINT AS avg_total_price_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.price_complete_transaction_count
              IS DISTINCT FROM expected.price_complete_transaction_count
    )::BIGINT AS transaction_count_mismatch_count
FROM city_monthly_expected_kpi AS expected
LEFT JOIN analytics.city_monthly_kpi AS target
    ON target.month_start = expected.month_start
   AND target.county_id = expected.county_id;

-- 5. YoY 對帳；所有差異筆數都應為 0。
SELECT
    COUNT(*) FILTER (
        WHERE target.median_unit_price_yoy
              IS DISTINCT FROM expected.median_unit_price_yoy
    )::BIGINT AS median_unit_price_yoy_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.avg_unit_price_yoy
              IS DISTINCT FROM expected.avg_unit_price_yoy
    )::BIGINT AS avg_unit_price_yoy_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.median_total_price_yoy
              IS DISTINCT FROM expected.median_total_price_yoy
    )::BIGINT AS median_total_price_yoy_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.avg_total_price_yoy
              IS DISTINCT FROM expected.avg_total_price_yoy
    )::BIGINT AS avg_total_price_yoy_mismatch_count,
    COUNT(*) FILTER (
        WHERE target.price_complete_transaction_count_yoy
              IS DISTINCT FROM expected.price_complete_transaction_count_yoy
    )::BIGINT AS transaction_count_yoy_mismatch_count
FROM city_monthly_expected_kpi AS expected
LEFT JOIN analytics.city_monthly_kpi AS target
    ON target.month_start = expected.month_start
   AND target.county_id = expected.county_id;

-- 6. Analytics 不同粒度的案件數必須對帳；價格不以此方式對帳，因為全國中位數與
-- 平均數直接從交易重算，不由縣市統計聚合。
WITH city_month_totals AS (
    SELECT
        city.month_start,
        SUM(city.price_complete_transaction_count)::BIGINT
            AS city_transaction_count
    FROM analytics.city_monthly_kpi AS city
    GROUP BY city.month_start
)
SELECT
    COUNT(*) FILTER (
        WHERE city_total.city_transaction_count
              IS DISTINCT FROM national.price_complete_transaction_count
    )::BIGINT AS national_city_transaction_count_mismatch_month_count
FROM analytics.national_monthly_kpi AS national
FULL JOIN city_month_totals AS city_total
    ON city_total.month_start = national.month_start;

-- 7. 最多列出 50 筆月份／縣市差異供診斷；正常表應回傳 0 筆。
SELECT
    COALESCE(expected.month_start, target.month_start) AS month_start,
    COALESCE(expected.county_id, target.county_id) AS county_id,
    expected.city AS expected_city,
    target.city AS actual_city,
    expected.month_start IS NULL AS unexpected_target_row,
    target.month_start IS NULL AS missing_target_row,
    target.city IS DISTINCT FROM expected.city
        AS city_name_mismatch,
    target.median_unit_price_10k_ping
        IS DISTINCT FROM expected.median_unit_price_10k_ping
        AS median_unit_price_mismatch,
    target.avg_unit_price_10k_ping
        IS DISTINCT FROM expected.avg_unit_price_10k_ping
        AS avg_unit_price_mismatch,
    target.median_total_price_10k
        IS DISTINCT FROM expected.median_total_price_10k
        AS median_total_price_mismatch,
    target.avg_total_price_10k
        IS DISTINCT FROM expected.avg_total_price_10k
        AS avg_total_price_mismatch,
    target.price_complete_transaction_count
        IS DISTINCT FROM expected.price_complete_transaction_count
        AS transaction_count_mismatch,
    target.median_unit_price_yoy
        IS DISTINCT FROM expected.median_unit_price_yoy
        AS median_unit_price_yoy_mismatch,
    target.avg_unit_price_yoy
        IS DISTINCT FROM expected.avg_unit_price_yoy
        AS avg_unit_price_yoy_mismatch,
    target.median_total_price_yoy
        IS DISTINCT FROM expected.median_total_price_yoy
        AS median_total_price_yoy_mismatch,
    target.avg_total_price_yoy
        IS DISTINCT FROM expected.avg_total_price_yoy
        AS avg_total_price_yoy_mismatch,
    target.price_complete_transaction_count_yoy
        IS DISTINCT FROM expected.price_complete_transaction_count_yoy
        AS transaction_count_yoy_mismatch
FROM city_monthly_expected_kpi AS expected
FULL JOIN analytics.city_monthly_kpi AS target
    ON target.month_start = expected.month_start
   AND target.county_id = expected.county_id
WHERE target.city IS DISTINCT FROM expected.city
   OR ROW(
       target.median_unit_price_10k_ping,
       target.avg_unit_price_10k_ping,
       target.median_total_price_10k,
       target.avg_total_price_10k,
       target.price_complete_transaction_count,
       target.median_unit_price_yoy,
       target.avg_unit_price_yoy,
       target.median_total_price_yoy,
       target.avg_total_price_yoy,
       target.price_complete_transaction_count_yoy
   ) IS DISTINCT FROM ROW(
       expected.median_unit_price_10k_ping,
       expected.avg_unit_price_10k_ping,
       expected.median_total_price_10k,
       expected.avg_total_price_10k,
       expected.price_complete_transaction_count,
       expected.median_unit_price_yoy,
       expected.avg_unit_price_yoy,
       expected.median_total_price_yoy,
       expected.avg_total_price_yoy,
       expected.price_complete_transaction_count_yoy
   )
ORDER BY
    month_start,
    county_id
LIMIT 50;

COMMIT;
