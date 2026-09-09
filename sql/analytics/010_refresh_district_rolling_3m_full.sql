-- 依 analytics.national_monthly_kpi 已發布的月份範圍，完整重建鄉鎮市區三個月滾動 KPI。
-- 第一個有效錨點為 2012-10，完整視窗是 2012-08 至 2012-10；每個彙總值都由
-- 錨點月與前兩個曆月的交易重新計算。
-- PostgreSQL 的 TRUNCATE 屬於交易；後續失敗會復原清空與替代寫入。

BEGIN;

-- 完整與增量更新共用同一把鎖，避免同時修改鄉鎮市區 KPI 表。
DO $district_rolling_full_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.district_rolling_3m.refresh', 0)
    );
END
$district_rolling_full_refresh_lock$;

-- 全國 Analytics 管理發布日期範圍；鄉鎮市區 Analytics 使用相同截止月份，
-- 但晚兩個月開始，使每個錨點都有完整三個曆月視窗。
CREATE TEMP TABLE district_rolling_full_refresh_range
ON COMMIT DROP
AS
SELECT
    DATE '2012-08-01' AS analysis_start_month,
    DATE '2012-10-01' AS first_anchor_month,
    MIN(national.month_start)::DATE AS national_start_month,
    MAX(national.month_start)::DATE AS published_end_month
FROM analytics.national_monthly_kpi AS national;

-- 鄉鎮市區骨架固定從目前 Core 維度衍生，不寫死筆數；經確認的地理參照異動
-- 因此不必同步修改本檔。
CREATE TEMP TABLE district_rolling_full_refresh_locations
ON COMMIT DROP
AS
SELECT
    location.location_id,
    location.county_id,
    location.town_id,
    location.city,
    location.district
FROM core.dim_location AS location;

-- 只有上游發布範圍與目前地理維度能組成完整骨架時，才允許替換目標表。
DO $district_rolling_full_refresh_input_gate$
DECLARE
    v_analysis_start_month DATE;
    v_first_anchor_month DATE;
    v_national_start_month DATE;
    v_published_end_month DATE;
    v_missing_national_month DATE;
    v_location_count BIGINT;
BEGIN
    SELECT
        refresh_range.analysis_start_month,
        refresh_range.first_anchor_month,
        refresh_range.national_start_month,
        refresh_range.published_end_month
    INTO
        v_analysis_start_month,
        v_first_anchor_month,
        v_national_start_month,
        v_published_end_month
    FROM district_rolling_full_refresh_range AS refresh_range;

    IF v_published_end_month IS NULL THEN
        RAISE EXCEPTION
            'District rolling full refresh requires a populated analytics.national_monthly_kpi table';
    END IF;

    IF v_national_start_month <> v_analysis_start_month THEN
        RAISE EXCEPTION
            'District rolling full refresh expected national data to start at %, but found %',
            v_analysis_start_month,
            v_national_start_month;
    END IF;

    IF v_published_end_month < v_first_anchor_month THEN
        RAISE EXCEPTION
            'District rolling full refresh requires a published end month on or after %, but found %',
            v_first_anchor_month,
            v_published_end_month;
    END IF;

    SELECT expected_month.month_start::DATE
    INTO v_missing_national_month
    FROM GENERATE_SERIES(
        v_analysis_start_month,
        v_published_end_month,
        INTERVAL '1 month'
    ) AS expected_month(month_start)
    LEFT JOIN analytics.national_monthly_kpi AS national
        ON national.month_start = expected_month.month_start::DATE
    WHERE national.month_start IS NULL
    ORDER BY expected_month.month_start
    LIMIT 1;

    IF v_missing_national_month IS NOT NULL THEN
        RAISE EXCEPTION
            'District rolling full refresh found a missing national month: %',
            v_missing_national_month;
    END IF;

    SELECT COUNT(*)::BIGINT
    INTO v_location_count
    FROM district_rolling_full_refresh_locations;

    IF v_location_count = 0 THEN
        RAISE EXCEPTION
            'District rolling full refresh requires a non-empty core.dim_location table';
    END IF;
END
$district_rolling_full_refresh_input_gate$;

TRUNCATE TABLE analytics.district_rolling_3m;

WITH anchor_months AS (
    SELECT generated_month.anchor_month::DATE AS anchor_month
    FROM district_rolling_full_refresh_range AS refresh_range
    CROSS JOIN LATERAL GENERATE_SERIES(
        refresh_range.first_anchor_month,
        refresh_range.published_end_month,
        INTERVAL '1 month'
    ) AS generated_month(anchor_month)
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
    CROSS JOIN district_rolling_full_refresh_locations AS location
),

eligible_transactions AS (
    SELECT
        fact.transaction_month,
        fact.location_id,
        fact.source_transaction_id,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM core.fact_transactions AS fact
    CROSS JOIN district_rolling_full_refresh_range AS refresh_range
    WHERE fact.transaction_month
              BETWEEN refresh_range.analysis_start_month
                  AND refresh_range.published_end_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
),

-- 範圍連接會將交易分配給所有包含該交易的滾動錨點；一筆交易最多影響三個錨點，
-- 且在每個錨點／地區組合中只計一次。
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
),

with_yoy AS (
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

    FROM year_pairs
)

INSERT INTO analytics.district_rolling_3m (
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
    median_unit_price_yoy,
    avg_unit_price_yoy,
    median_total_price_yoy,
    avg_total_price_yoy,
    price_complete_transaction_count_yoy
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
    median_unit_price_yoy,
    avg_unit_price_yoy,
    median_total_price_yoy,
    avg_total_price_yoy,
    price_complete_transaction_count_yoy
FROM with_yoy;

-- 回傳載入器與協調器使用的精簡完整更新摘要。
SELECT
    refresh_range.first_anchor_month,
    refresh_range.published_end_month,
    (
        SELECT COUNT(*)::BIGINT
        FROM district_rolling_full_refresh_locations
    ) AS location_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM GENERATE_SERIES(
            refresh_range.first_anchor_month,
            refresh_range.published_end_month,
            INTERVAL '1 month'
        ) AS anchor(anchor_month)
    ) AS anchor_month_count,
    COUNT(target.month_start)::BIGINT AS refreshed_row_count
FROM district_rolling_full_refresh_range AS refresh_range
LEFT JOIN analytics.district_rolling_3m AS target
    ON target.month_start BETWEEN
       refresh_range.first_anchor_month
       AND refresh_range.published_end_month
GROUP BY
    refresh_range.first_anchor_month,
    refresh_range.published_end_month;

COMMIT;
