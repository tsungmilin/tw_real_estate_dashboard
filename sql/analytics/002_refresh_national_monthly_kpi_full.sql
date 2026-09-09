-- 依目前符合條件的截止月份，完整重建全國月 KPI。
-- PostgreSQL 的 TRUNCATE 屬於交易；後續失敗會一併復原。

BEGIN;

-- 完整與增量更新共用同一把鎖，避免同時修改
-- analytics.national_monthly_kpi。
DO $national_monthly_full_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.national_monthly_kpi.refresh', 0)
    );
END
$national_monthly_full_refresh_lock$;

CREATE TEMP TABLE national_monthly_full_refresh_parameter (
    analysis_start_month DATE PRIMARY KEY,
    tail_volume_threshold NUMERIC(5,4) NOT NULL,

    CONSTRAINT national_monthly_full_refresh_parameter_month_check
        CHECK (EXTRACT(DAY FROM analysis_start_month) = 1),

    CONSTRAINT national_monthly_full_refresh_parameter_threshold_check
        CHECK (
            tail_volume_threshold > 0
            AND tail_volume_threshold <= 1
        )
) ON COMMIT DROP;

INSERT INTO national_monthly_full_refresh_parameter (
    analysis_start_month,
    tail_volume_threshold
)
VALUES (
    DATE '2012-08-01',
    0.75
);

-- 完整更新直接採用目前 core 資料算出的候選截止月份；不保留舊截止月份，
-- 因此可在人工檢查後正式縮短或延伸發布範圍。
CREATE TEMP TABLE national_monthly_full_refresh_cutoff (
    baseline_transaction_count NUMERIC,
    tail_volume_threshold NUMERIC(5,4) NOT NULL,
    analysis_end_month DATE
) ON COMMIT DROP;

WITH monthly_transaction_counts AS (
    SELECT
        fact.transaction_month AS month_start,
        COUNT(fact.source_transaction_id)::BIGINT
            AS price_complete_transaction_count
    FROM core.fact_transactions AS fact
    CROSS JOIN national_monthly_full_refresh_parameter AS parameter
    WHERE fact.transaction_month >= parameter.analysis_start_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
    GROUP BY fact.transaction_month
),
baseline AS (
    SELECT
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY monthly.price_complete_transaction_count
        )::NUMERIC AS baseline_transaction_count
    FROM monthly_transaction_counts AS monthly
)
INSERT INTO national_monthly_full_refresh_cutoff (
    baseline_transaction_count,
    tail_volume_threshold,
    analysis_end_month
)
SELECT
    baseline.baseline_transaction_count,
    parameter.tail_volume_threshold,
    MAX(monthly.month_start)::DATE AS analysis_end_month
FROM monthly_transaction_counts AS monthly
CROSS JOIN baseline
CROSS JOIN national_monthly_full_refresh_parameter AS parameter
WHERE monthly.price_complete_transaction_count
      >= baseline.baseline_transaction_count
         * parameter.tail_volume_threshold
GROUP BY
    baseline.baseline_transaction_count,
    parameter.tail_volume_threshold;

-- 必須先成功算出截止月份，才允許清空並重建正式 Analytics 表。
DO $national_monthly_full_refresh_cutoff_gate$
DECLARE
    v_analysis_start_month DATE;
    v_baseline_transaction_count NUMERIC;
    v_analysis_end_month DATE;
BEGIN
    SELECT
        parameter.analysis_start_month,
        cutoff.baseline_transaction_count,
        cutoff.analysis_end_month
    INTO
        v_analysis_start_month,
        v_baseline_transaction_count,
        v_analysis_end_month
    FROM national_monthly_full_refresh_parameter AS parameter
    LEFT JOIN national_monthly_full_refresh_cutoff AS cutoff
        ON TRUE;

    IF v_baseline_transaction_count IS NULL
       OR v_analysis_end_month IS NULL THEN
        RAISE EXCEPTION
            'Analytics full refresh cutoff calculation failed: no qualifying core month exists on or after %',
            v_analysis_start_month;
    END IF;
END
$national_monthly_full_refresh_cutoff_gate$;

TRUNCATE TABLE analytics.national_monthly_kpi;

WITH params AS (
    SELECT
        parameter.analysis_start_month,
        cutoff.analysis_end_month
    FROM national_monthly_full_refresh_parameter AS parameter
    CROSS JOIN national_monthly_full_refresh_cutoff AS cutoff
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

kpi_transactions AS (
    SELECT
        fact.transaction_month AS month_start,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM core.fact_transactions AS fact
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

monthly_aggregation AS (
    SELECT
        month_start,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS median_unit_price_10k_ping,

        AVG(unit_price_ntd_m2)
            * 3.305785 / 10000.0
            AS avg_unit_price_10k_ping,

        PERCENTILE_CONT(0.5)
            WITHIN GROUP (ORDER BY total_price_ntd)
            / 10000.0
            AS median_total_price_10k,

        AVG(total_price_ntd)
            / 10000.0
            AS avg_total_price_10k,

        COUNT(*) AS price_complete_transaction_count

    FROM kpi_transactions
    GROUP BY month_start
),

complete_months AS (
    SELECT
        months.month_start,

        monthly.median_unit_price_10k_ping::NUMERIC(18,6)
            AS median_unit_price_10k_ping,
        monthly.avg_unit_price_10k_ping::NUMERIC(18,6)
            AS avg_unit_price_10k_ping,

        monthly.median_total_price_10k::NUMERIC(18,6)
            AS median_total_price_10k,
        monthly.avg_total_price_10k::NUMERIC(18,6)
            AS avg_total_price_10k,

        COALESCE(
            monthly.price_complete_transaction_count,
            0::BIGINT
        ) AS price_complete_transaction_count

    FROM month_spine AS months
    LEFT JOIN monthly_aggregation AS monthly
        ON monthly.month_start = months.month_start
),

year_pairs AS (
    SELECT
        current_month.month_start,

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

    FROM complete_months AS current_month
    LEFT JOIN complete_months AS previous_year
        ON previous_year.month_start =
           (current_month.month_start - INTERVAL '1 year')::DATE
),

with_yoy AS (
    SELECT
        month_start,

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

INSERT INTO analytics.national_monthly_kpi (
    month_start,

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

-- 回傳本次完整更新採用的截止月份與完成後月份數，供人工核對。
SELECT
    parameter.analysis_start_month,
    cutoff.baseline_transaction_count,
    cutoff.tail_volume_threshold,
    cutoff.analysis_end_month,
    COUNT(target.month_start)::BIGINT AS refreshed_month_count
FROM national_monthly_full_refresh_parameter AS parameter
CROSS JOIN national_monthly_full_refresh_cutoff AS cutoff
LEFT JOIN analytics.national_monthly_kpi AS target
    ON target.month_start BETWEEN
       parameter.analysis_start_month AND cutoff.analysis_end_month
GROUP BY
    parameter.analysis_start_month,
    cutoff.baseline_transaction_count,
    cutoff.tail_volume_threshold,
    cutoff.analysis_end_month;

COMMIT;
