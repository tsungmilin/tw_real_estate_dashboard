-- 依 analytics.national_monthly_kpi 已發布的月份範圍，完整重建縣市月 KPI。
-- 執行順序：先更新全國月 KPI，再執行本檔。
-- PostgreSQL 的 TRUNCATE 屬於交易；後續失敗會復原清空與替代寫入。

BEGIN;

-- 完整與增量更新共用同一把鎖，避免同時修改縣市月 KPI 表。
DO $city_monthly_full_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.city_monthly_kpi.refresh', 0)
    );
END
$city_monthly_full_refresh_lock$;

-- 全國表是儀表板發布月份範圍的唯一依據；縣市表必須涵蓋相同連續範圍。
CREATE TEMP TABLE city_monthly_full_refresh_parameter (
    analysis_start_month DATE PRIMARY KEY,
    analysis_end_month DATE NOT NULL,

    CONSTRAINT city_monthly_full_refresh_parameter_range_check
        CHECK (analysis_start_month <= analysis_end_month),

    CONSTRAINT city_monthly_full_refresh_parameter_month_check
        CHECK (
            EXTRACT(DAY FROM analysis_start_month) = 1
            AND EXTRACT(DAY FROM analysis_end_month) = 1
        )
) ON COMMIT DROP;

INSERT INTO city_monthly_full_refresh_parameter (
    analysis_start_month,
    analysis_end_month
)
SELECT
    DATE '2012-08-01',
    MAX(national.month_start)::DATE
FROM analytics.national_monthly_kpi AS national
HAVING MAX(national.month_start) IS NOT NULL;

-- Core 保證 22 組一對一 county_id／city；若契約失效，暫存表的主鍵與
-- UNIQUE 約束會立即阻止更新。
CREATE TEMP TABLE city_monthly_full_refresh_counties (
    county_id TEXT PRIMARY KEY,
    city TEXT NOT NULL UNIQUE
) ON COMMIT DROP;

INSERT INTO city_monthly_full_refresh_counties (
    county_id,
    city
)
SELECT DISTINCT
    location.county_id,
    location.city
FROM core.dim_location AS location;

-- 下列情況拒絕重建：全國表沒有發布範圍、起點不符約定、月份不連續，
-- 或 Core 縣市契約不是 22 個成員。
DO $city_monthly_full_refresh_input_gate$
DECLARE
    v_analysis_start_month DATE := DATE '2012-08-01';
    v_analysis_end_month DATE;
    v_first_national_month DATE;
    v_missing_month DATE;
    v_county_count BIGINT;
BEGIN
    SELECT parameter.analysis_end_month
    INTO v_analysis_end_month
    FROM city_monthly_full_refresh_parameter AS parameter;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'City monthly full refresh requires a populated analytics.national_monthly_kpi table';
    END IF;

    SELECT MIN(national.month_start)::DATE
    INTO v_first_national_month
    FROM analytics.national_monthly_kpi AS national;

    IF v_first_national_month <> v_analysis_start_month THEN
        RAISE EXCEPTION
            'City monthly full refresh expected national data to start at %, but found %',
            v_analysis_start_month,
            v_first_national_month;
    END IF;

    SELECT expected_month.month_start::DATE
    INTO v_missing_month
    FROM GENERATE_SERIES(
        v_analysis_start_month,
        v_analysis_end_month,
        INTERVAL '1 month'
    ) AS expected_month(month_start)
    LEFT JOIN analytics.national_monthly_kpi AS national
        ON national.month_start = expected_month.month_start::DATE
    WHERE national.month_start IS NULL
    ORDER BY expected_month.month_start
    LIMIT 1;

    IF v_missing_month IS NOT NULL THEN
        RAISE EXCEPTION
            'City monthly full refresh found a missing national month: %',
            v_missing_month;
    END IF;

    SELECT COUNT(*)::BIGINT
    INTO v_county_count
    FROM city_monthly_full_refresh_counties;

    IF v_county_count <> 22 THEN
        RAISE EXCEPTION
            'City monthly full refresh expected 22 county/city members from core.dim_location, but found %',
            v_county_count;
    END IF;
END
$city_monthly_full_refresh_input_gate$;

TRUNCATE TABLE analytics.city_monthly_kpi;

WITH params AS (
    SELECT
        parameter.analysis_start_month,
        parameter.analysis_end_month
    FROM city_monthly_full_refresh_parameter AS parameter
),

month_spine AS (
    SELECT
        generated_month.month_start::DATE AS month_start
    FROM params
    CROSS JOIN LATERAL GENERATE_SERIES(
        params.analysis_start_month,
        params.analysis_end_month,
        INTERVAL '1 month'
    ) AS generated_month(month_start)
),

month_county_spine AS (
    SELECT
        month.month_start,
        county.county_id,
        county.city
    FROM month_spine AS month
    CROSS JOIN city_monthly_full_refresh_counties AS county
),

kpi_transactions AS (
    SELECT
        fact.transaction_month AS month_start,
        location.county_id,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM core.fact_transactions AS fact
    JOIN core.dim_location AS location
        ON location.location_id = fact.location_id
    CROSS JOIN params
    WHERE fact.transaction_month
              BETWEEN params.analysis_start_month
                  AND params.analysis_end_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
),

monthly_city_aggregation AS (
    SELECT
        kpi.month_start,
        kpi.county_id,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY kpi.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS median_unit_price_10k_ping,

        AVG(kpi.unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS avg_unit_price_10k_ping,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY kpi.total_price_ntd)
            / 10000.0
            AS median_total_price_10k,

        AVG(kpi.total_price_ntd)
            / 10000.0
            AS avg_total_price_10k,

        COUNT(*)::BIGINT AS price_complete_transaction_count

    FROM kpi_transactions AS kpi
    GROUP BY
        kpi.month_start,
        kpi.county_id
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
        current_month.month_start,
        current_month.county_id,
        current_month.city,

        current_month.median_unit_price_10k_ping,
        current_month.avg_unit_price_10k_ping,

        current_month.median_total_price_10k,
        current_month.avg_total_price_10k,

        current_month.price_complete_transaction_count,

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
),

with_yoy AS (
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
            ELSE
                median_unit_price_10k_ping
                / previous_year_median_unit_price_10k_ping
                - 1
        END AS median_unit_price_yoy,

        CASE
            WHEN avg_unit_price_10k_ping IS NULL
              OR previous_year_avg_unit_price_10k_ping IS NULL
            THEN NULL
            ELSE
                avg_unit_price_10k_ping
                / previous_year_avg_unit_price_10k_ping
                - 1
        END AS avg_unit_price_yoy,

        CASE
            WHEN median_total_price_10k IS NULL
              OR previous_year_median_total_price_10k IS NULL
            THEN NULL
            ELSE
                median_total_price_10k
                / previous_year_median_total_price_10k
                - 1
        END AS median_total_price_yoy,

        CASE
            WHEN avg_total_price_10k IS NULL
              OR previous_year_avg_total_price_10k IS NULL
            THEN NULL
            ELSE
                avg_total_price_10k
                / previous_year_avg_total_price_10k
                - 1
        END AS avg_total_price_yoy,

        CASE
            WHEN previous_year_price_complete_transaction_count IS NULL
              OR previous_year_price_complete_transaction_count = 0
            THEN NULL
            ELSE
                price_complete_transaction_count::NUMERIC
                / previous_year_price_complete_transaction_count
                - 1
        END AS price_complete_transaction_count_yoy

    FROM year_pairs
)

INSERT INTO analytics.city_monthly_kpi (
    month_start,
    county_id,
    city,

    median_unit_price_10k_ping,
    avg_unit_price_10k_ping,

    median_total_price_10k,
    avg_total_price_10k,

    price_complete_transaction_count,

    median_unit_price_yoy,
    avg_unit_price_yoy,

    median_total_price_yoy,
    avg_total_price_yoy,

    price_complete_transaction_count_yoy
)
SELECT
    month_start,
    county_id,
    city,

    median_unit_price_10k_ping::NUMERIC(18,6),
    avg_unit_price_10k_ping::NUMERIC(18,6),

    median_total_price_10k::NUMERIC(18,6),
    avg_total_price_10k::NUMERIC(18,6),

    price_complete_transaction_count,

    median_unit_price_yoy::NUMERIC(14,8),
    avg_unit_price_yoy::NUMERIC(14,8),

    median_total_price_yoy::NUMERIC(14,8),
    avg_total_price_yoy::NUMERIC(14,8),

    price_complete_transaction_count_yoy::NUMERIC(14,8)

FROM with_yoy;

-- 回傳發布範圍與重建筆數，供人工核對。
SELECT
    parameter.analysis_start_month,
    parameter.analysis_end_month,
    COUNT(DISTINCT target.month_start)::BIGINT AS refreshed_month_count,
    COUNT(DISTINCT target.county_id)::BIGINT AS refreshed_county_count,
    COUNT(target.month_start)::BIGINT AS refreshed_row_count
FROM city_monthly_full_refresh_parameter AS parameter
LEFT JOIN analytics.city_monthly_kpi AS target
    ON target.month_start BETWEEN
       parameter.analysis_start_month AND parameter.analysis_end_month
GROUP BY
    parameter.analysis_start_month,
    parameter.analysis_end_month;

COMMIT;
