-- 依一個成功的 core 批次增量更新縣市月 KPI。
-- 必須透過 psql 傳入 --set=load_batch_id=<明確的批次 ID>。
-- 應先執行全國增量更新；其發布月份範圍是本次縣市更新的唯一依據。

\if :{?load_batch_id}
\else
\echo '缺少必要的 psql 變數：load_batch_id'
\quit 3
\endif

BEGIN;

-- 完整與增量更新共用同一把鎖，避免同時修改縣市月 KPI 表。
DO $city_monthly_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.city_monthly_kpi.refresh', 0)
    );
END
$city_monthly_refresh_lock$;

CREATE TEMP TABLE city_monthly_refresh_parameter (
    load_batch_id TEXT PRIMARY KEY,
    analysis_start_month DATE NOT NULL,

    CONSTRAINT city_monthly_refresh_parameter_month_check
        CHECK (EXTRACT(DAY FROM analysis_start_month) = 1)
) ON COMMIT DROP;

INSERT INTO city_monthly_refresh_parameter (
    load_batch_id,
    analysis_start_month
)
VALUES (
    :'load_batch_id',
    DATE '2012-08-01'
);

-- 讀取受影響月份前，先驗證指定的 core 批次。
DO $city_monthly_refresh_core_batch_gate$
DECLARE
    v_load_batch_id TEXT;
    v_core_status TEXT;
BEGIN
    SELECT parameter.load_batch_id
    INTO v_load_batch_id
    FROM city_monthly_refresh_parameter AS parameter;

    SELECT sync_run.status
    INTO v_core_status
    FROM core.sync_runs AS sync_run
    WHERE sync_run.load_batch_id = v_load_batch_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'City analytics refresh input validation failed: core batch % does not exist',
            v_load_batch_id;
    END IF;

    IF v_core_status <> 'success' THEN
        RAISE EXCEPTION
            'City analytics refresh input validation failed: core batch % has status %, expected success',
            v_load_batch_id,
            v_core_status;
    END IF;
END
$city_monthly_refresh_core_batch_gate$;

-- Core 保證 22 組一對一 county_id／city；若契約失效，暫存表約束會立即阻止更新。
CREATE TEMP TABLE city_monthly_refresh_counties (
    county_id TEXT PRIMARY KEY,
    city TEXT NOT NULL UNIQUE
) ON COMMIT DROP;

INSERT INTO city_monthly_refresh_counties (
    county_id,
    city
)
SELECT DISTINCT
    location.county_id,
    location.city
FROM core.dim_location AS location;

-- 全國表控制發布截止月份；縣市增量更新可延伸至該月，但不得超出。
CREATE TEMP TABLE city_monthly_refresh_range (
    published_end_month DATE,
    current_end_month DATE
) ON COMMIT DROP;

INSERT INTO city_monthly_refresh_range (
    published_end_month,
    current_end_month
)
SELECT
    (
        SELECT MAX(national.month_start)::DATE
        FROM analytics.national_monthly_kpi AS national
    ) AS published_end_month,
    (
        SELECT MAX(city.month_start)::DATE
        FROM analytics.city_monthly_kpi AS city
    ) AS current_end_month;

-- 更新任何目標列前，要求縣市完整更新已完成，且全國與縣市月份骨架連續。
DO $city_monthly_refresh_input_gate$
DECLARE
    v_analysis_start_month DATE;
    v_published_start_month DATE;
    v_published_end_month DATE;
    v_current_end_month DATE;
    v_missing_national_month DATE;
    v_missing_city_month DATE;
    v_missing_county_id TEXT;
    v_county_count BIGINT;
    v_unexpected_county_id TEXT;
BEGIN
    SELECT
        parameter.analysis_start_month,
        refresh_range.published_end_month,
        refresh_range.current_end_month
    INTO
        v_analysis_start_month,
        v_published_end_month,
        v_current_end_month
    FROM city_monthly_refresh_parameter AS parameter
    CROSS JOIN city_monthly_refresh_range AS refresh_range;

    IF v_published_end_month IS NULL THEN
        RAISE EXCEPTION
            'City analytics incremental refresh requires a populated analytics.national_monthly_kpi table';
    END IF;

    SELECT MIN(national.month_start)::DATE
    INTO v_published_start_month
    FROM analytics.national_monthly_kpi AS national;

    IF v_published_start_month <> v_analysis_start_month THEN
        RAISE EXCEPTION
            'City analytics incremental refresh expected national data to start at %, but found %',
            v_analysis_start_month,
            v_published_start_month;
    END IF;

    IF v_current_end_month IS NULL THEN
        RAISE EXCEPTION
            'City analytics incremental refresh requires a completed city full refresh; target table is empty';
    END IF;

    IF v_current_end_month < v_analysis_start_month THEN
        RAISE EXCEPTION
            'City analytics incremental refresh current end month % is before analysis start %',
            v_current_end_month,
            v_analysis_start_month;
    END IF;

    IF v_current_end_month > v_published_end_month THEN
        RAISE EXCEPTION
            'City analytics incremental refresh found city end month % beyond national published end month %; run a reviewed full refresh',
            v_current_end_month,
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
            'City analytics incremental refresh found a missing national month: %',
            v_missing_national_month;
    END IF;

    SELECT COUNT(*)::BIGINT
    INTO v_county_count
    FROM city_monthly_refresh_counties;

    IF v_county_count <> 22 THEN
        RAISE EXCEPTION
            'City analytics incremental refresh expected 22 county/city members from core.dim_location, but found %',
            v_county_count;
    END IF;

    -- 先找缺少的月份，再找該月缺少的縣市，使錯誤能指出確切資料列。
    SELECT expected_month.month_start::DATE
    INTO v_missing_city_month
    FROM GENERATE_SERIES(
        v_analysis_start_month,
        v_current_end_month,
        INTERVAL '1 month'
    ) AS expected_month(month_start)
    CROSS JOIN city_monthly_refresh_counties AS county
    LEFT JOIN analytics.city_monthly_kpi AS target
        ON target.month_start = expected_month.month_start::DATE
       AND target.county_id = county.county_id
    WHERE target.month_start IS NULL
    ORDER BY
        expected_month.month_start,
        county.county_id
    LIMIT 1;

    IF v_missing_city_month IS NOT NULL THEN
        SELECT county.county_id
        INTO v_missing_county_id
        FROM city_monthly_refresh_counties AS county
        LEFT JOIN analytics.city_monthly_kpi AS target
            ON target.month_start = v_missing_city_month
           AND target.county_id = county.county_id
        WHERE target.month_start IS NULL
        ORDER BY county.county_id
        LIMIT 1;

        RAISE EXCEPTION
            'City analytics incremental refresh requires a completed full refresh; missing target row for month %, county_id %',
            v_missing_city_month,
            v_missing_county_id;
    END IF;

    SELECT target.county_id
    INTO v_unexpected_county_id
    FROM analytics.city_monthly_kpi AS target
    LEFT JOIN city_monthly_refresh_counties AS county
        ON county.county_id = target.county_id
       AND county.city = target.city
    WHERE target.month_start
              BETWEEN v_analysis_start_month AND v_current_end_month
      AND county.county_id IS NULL
    ORDER BY target.county_id
    LIMIT 1;

    IF v_unexpected_county_id IS NOT NULL THEN
        RAISE EXCEPTION
            'City analytics incremental refresh found a target county/name not present in core.dim_location: %',
            v_unexpected_county_id;
    END IF;
END
$city_monthly_refresh_input_gate$;

-- 基礎月份 M 包含：發布範圍內受本批次影響的月份，以及目前縣市截止後
-- 所有新發布月份。
CREATE TEMP TABLE city_monthly_affected_base_months (
    month_start DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO city_monthly_affected_base_months (month_start)
SELECT month_change.month_start
FROM staging.load_month_changes AS month_change
JOIN city_monthly_refresh_parameter AS parameter
    ON month_change.load_batch_id = parameter.load_batch_id
CROSS JOIN city_monthly_refresh_range AS refresh_range
WHERE month_change.has_core_impact
  AND month_change.month_start
          BETWEEN parameter.analysis_start_month
              AND refresh_range.published_end_month

UNION

SELECT generated_month.month_start::DATE
FROM city_monthly_refresh_range AS refresh_range
CROSS JOIN LATERAL GENERATE_SERIES(
    refresh_range.current_end_month + INTERVAL '1 month',
    refresh_range.published_end_month,
    INTERVAL '1 month'
) AS generated_month(month_start);

-- M 本身會改變，而 M + 12 個月會以 M 作為去年基準。
CREATE TEMP TABLE city_monthly_affected_yoy_months (
    month_start DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO city_monthly_affected_yoy_months (month_start)
SELECT affected.month_start
FROM city_monthly_affected_base_months AS affected

UNION

SELECT (affected.month_start + INTERVAL '1 year')::DATE
FROM city_monthly_affected_base_months AS affected
CROSS JOIN city_monthly_refresh_range AS refresh_range
WHERE (affected.month_start + INTERVAL '1 year')::DATE
          <= refresh_range.published_end_month;

-- 對每個受影響月份重算所有縣市。load_month_changes 只有月份粒度，
-- 全部重算可避免遺漏受影響縣市，規模仍很小（通常每月 22 筆）。
WITH eligible_transactions AS (
    SELECT
        fact.transaction_month AS month_start,
        location.county_id,
        fact.source_transaction_id,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM core.fact_transactions AS fact
    JOIN core.dim_location AS location
        ON location.location_id = fact.location_id
    JOIN city_monthly_affected_base_months AS affected
        ON affected.month_start = fact.transaction_month
    WHERE fact.transaction_type IN (
        '房地(土地+建物)',
        '房地(土地+建物)+車位'
    )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
),

refreshed_base_values AS (
    SELECT
        affected.month_start,
        county.county_id,
        county.city,

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

        COUNT(kpi.source_transaction_id)::BIGINT
            AS price_complete_transaction_count

    FROM city_monthly_affected_base_months AS affected
    CROSS JOIN city_monthly_refresh_counties AS county
    LEFT JOIN eligible_transactions AS kpi
        ON kpi.month_start = affected.month_start
       AND kpi.county_id = county.county_id
    GROUP BY
        affected.month_start,
        county.county_id,
        county.city
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
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
FROM refreshed_base_values

ON CONFLICT (month_start, county_id)
DO UPDATE SET
    city = EXCLUDED.city,
    median_unit_price_10k_ping =
        EXCLUDED.median_unit_price_10k_ping,
    avg_unit_price_10k_ping =
        EXCLUDED.avg_unit_price_10k_ping,
    median_total_price_10k =
        EXCLUDED.median_total_price_10k,
    avg_total_price_10k =
        EXCLUDED.avg_total_price_10k,
    price_complete_transaction_count =
        EXCLUDED.price_complete_transaction_count,
    median_unit_price_yoy = NULL,
    avg_unit_price_yoy = NULL,
    median_total_price_yoy = NULL,
    avg_total_price_yoy = NULL,
    price_complete_transaction_count_yoy = NULL;

-- 使用 analytics 已更新的值重算受影響月份 YoY，不再次掃描事實表。
WITH recalculated_yoy AS (
    SELECT
        current_month.month_start,
        current_month.county_id,

        CASE
            WHEN current_month.median_unit_price_10k_ping IS NULL
              OR previous_year.median_unit_price_10k_ping IS NULL
            THEN NULL
            ELSE (
                current_month.median_unit_price_10k_ping
                / previous_year.median_unit_price_10k_ping
                - 1
            )::NUMERIC(14,8)
        END AS median_unit_price_yoy,

        CASE
            WHEN current_month.avg_unit_price_10k_ping IS NULL
              OR previous_year.avg_unit_price_10k_ping IS NULL
            THEN NULL
            ELSE (
                current_month.avg_unit_price_10k_ping
                / previous_year.avg_unit_price_10k_ping
                - 1
            )::NUMERIC(14,8)
        END AS avg_unit_price_yoy,

        CASE
            WHEN current_month.median_total_price_10k IS NULL
              OR previous_year.median_total_price_10k IS NULL
            THEN NULL
            ELSE (
                current_month.median_total_price_10k
                / previous_year.median_total_price_10k
                - 1
            )::NUMERIC(14,8)
        END AS median_total_price_yoy,

        CASE
            WHEN current_month.avg_total_price_10k IS NULL
              OR previous_year.avg_total_price_10k IS NULL
            THEN NULL
            ELSE (
                current_month.avg_total_price_10k
                / previous_year.avg_total_price_10k
                - 1
            )::NUMERIC(14,8)
        END AS avg_total_price_yoy,

        CASE
            WHEN previous_year.price_complete_transaction_count IS NULL
              OR previous_year.price_complete_transaction_count = 0
            THEN NULL
            ELSE (
                current_month.price_complete_transaction_count::NUMERIC
                / previous_year.price_complete_transaction_count
                - 1
            )::NUMERIC(14,8)
        END AS price_complete_transaction_count_yoy

    FROM city_monthly_affected_yoy_months AS affected
    JOIN analytics.city_monthly_kpi AS current_month
        ON current_month.month_start = affected.month_start
    LEFT JOIN analytics.city_monthly_kpi AS previous_year
        ON previous_year.county_id = current_month.county_id
       AND previous_year.month_start =
           (current_month.month_start - INTERVAL '1 year')::DATE
)

UPDATE analytics.city_monthly_kpi AS target
SET
    median_unit_price_yoy = recalculated.median_unit_price_yoy,
    avg_unit_price_yoy = recalculated.avg_unit_price_yoy,
    median_total_price_yoy = recalculated.median_total_price_yoy,
    avg_total_price_yoy = recalculated.avg_total_price_yoy,
    price_complete_transaction_count_yoy =
        recalculated.price_complete_transaction_count_yoy
FROM recalculated_yoy AS recalculated
WHERE target.month_start = recalculated.month_start
  AND target.county_id = recalculated.county_id;

-- 回傳載入器與協調器使用的精簡執行摘要。
SELECT
    parameter.load_batch_id,
    refresh_range.current_end_month,
    refresh_range.published_end_month,
    (
        SELECT COUNT(*)::BIGINT
        FROM city_monthly_refresh_counties
    ) AS county_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM city_monthly_affected_base_months
    ) AS affected_base_month_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM city_monthly_affected_yoy_months
    ) AS affected_yoy_month_count,
    (
        SELECT ARRAY_AGG(month_start ORDER BY month_start)
        FROM city_monthly_affected_base_months
    ) AS affected_base_months,
    (
        SELECT ARRAY_AGG(month_start ORDER BY month_start)
        FROM city_monthly_affected_yoy_months
    ) AS affected_yoy_months
FROM city_monthly_refresh_parameter AS parameter
CROSS JOIN city_monthly_refresh_range AS refresh_range;

COMMIT;
