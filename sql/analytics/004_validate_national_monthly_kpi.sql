-- 以 core.fact_transactions 獨立重算的結果驗證 analytics.national_monthly_kpi；
-- 本檔不修改永久資料。

BEGIN;

-- 驗證規則的固定輸入；這張暫存表只存在於本次交易。
CREATE TEMP TABLE national_monthly_validation_parameter
ON COMMIT DROP
AS
SELECT
    DATE '2012-08-01' AS analysis_start_month,
    0.75::NUMERIC(5,4) AS tail_volume_threshold;

-- 從 core 重算候選截止月份，並讀取 Analytics 目前的截止月份。
CREATE TEMP TABLE national_monthly_validation_cutoff
ON COMMIT DROP
AS
WITH monthly_transaction_counts AS (
    SELECT
        fact.transaction_month AS month_start,
        COUNT(fact.source_transaction_id)::BIGINT
            AS price_complete_transaction_count
    FROM core.fact_transactions AS fact
    CROSS JOIN national_monthly_validation_parameter AS parameter
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
),
candidate_cutoff AS (
    SELECT MAX(monthly.month_start)::DATE AS candidate_end_month
    FROM monthly_transaction_counts AS monthly
    CROSS JOIN baseline
    CROSS JOIN national_monthly_validation_parameter AS parameter
    WHERE monthly.price_complete_transaction_count
          >= baseline.baseline_transaction_count
             * parameter.tail_volume_threshold
),
current_cutoff AS (
    SELECT MAX(target.month_start)::DATE AS current_end_month
    FROM analytics.national_monthly_kpi AS target
)
SELECT
    parameter.analysis_start_month,
    baseline.baseline_transaction_count,
    parameter.tail_volume_threshold,
    baseline.baseline_transaction_count
        * parameter.tail_volume_threshold
        AS threshold_transaction_count,
    candidate_cutoff.candidate_end_month,
    current_cutoff.current_end_month,
    CASE
        WHEN current_cutoff.current_end_month IS NULL
        THEN candidate_cutoff.candidate_end_month
        WHEN candidate_cutoff.candidate_end_month IS NULL
        THEN current_cutoff.current_end_month
        ELSE GREATEST(
            current_cutoff.current_end_month,
            candidate_cutoff.candidate_end_month
        )
    END AS validation_end_month
FROM national_monthly_validation_parameter AS parameter
CROSS JOIN baseline
CROSS JOIN candidate_cutoff
CROSS JOIN current_cutoff;

-- 從 core 獨立重算 validation_end_month 以前的月份骨架、月 KPI 與 YoY。
-- 若目前截止月份落後候選值，也建立尚缺月份，供後續涵蓋範圍驗證辨識。
CREATE TEMP TABLE national_monthly_expected_kpi
ON COMMIT DROP
AS
WITH month_spine AS (
    SELECT generated_month.month_start::DATE AS month_start
    FROM national_monthly_validation_cutoff AS cutoff
    CROSS JOIN LATERAL GENERATE_SERIES(
        cutoff.analysis_start_month,
        cutoff.validation_end_month,
        INTERVAL '1 month'
    ) AS generated_month(month_start)
),
monthly_aggregation AS (
    SELECT
        fact.transaction_month AS month_start,

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
    CROSS JOIN national_monthly_validation_cutoff AS cutoff
    WHERE fact.transaction_month
              BETWEEN cutoff.analysis_start_month
                  AND cutoff.validation_end_month
      AND fact.transaction_type IN (
          '房地(土地+建物)',
          '房地(土地+建物)+車位'
      )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
    GROUP BY fact.transaction_month
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
    FROM complete_months AS current_month
    LEFT JOIN complete_months AS previous_year
        ON previous_year.month_start =
           (current_month.month_start - INTERVAL '1 year')::DATE
)
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

-- 1. 截止月份摘要。
-- PASS_ALIGNED：目前值與候選值相同。
-- REVIEW_CURRENT_AHEAD_OF_CANDIDATE：符合增量更新不自動縮短的規則；
-- 若要縮短，必須人工檢查後執行完整更新。
SELECT
    cutoff.analysis_start_month,
    cutoff.baseline_transaction_count,
    cutoff.tail_volume_threshold,
    cutoff.threshold_transaction_count,
    cutoff.candidate_end_month,
    cutoff.current_end_month,
    cutoff.validation_end_month,
    CASE
        WHEN cutoff.baseline_transaction_count IS NULL
          OR cutoff.candidate_end_month IS NULL
        THEN 'FAIL_NO_QUALIFYING_CORE_MONTH'
        WHEN cutoff.current_end_month IS NULL
        THEN 'FAIL_TARGET_EMPTY'
        WHEN cutoff.current_end_month < cutoff.candidate_end_month
        THEN 'FAIL_CURRENT_BEHIND_CANDIDATE'
        WHEN cutoff.current_end_month > cutoff.candidate_end_month
        THEN 'REVIEW_CURRENT_AHEAD_OF_CANDIDATE'
        ELSE 'PASS_ALIGNED'
    END AS cutoff_status
FROM national_monthly_validation_cutoff AS cutoff;

-- 2. 月份骨架驗證；正常情況下缺少與多餘筆數都是 0。
SELECT
    COUNT(expected.month_start)::BIGINT AS expected_month_count,
    COUNT(target.month_start)::BIGINT AS actual_month_count,
    COUNT(*) FILTER (
        WHERE expected.month_start IS NOT NULL
          AND target.month_start IS NULL
    )::BIGINT AS missing_month_count,
    COUNT(*) FILTER (
        WHERE expected.month_start IS NULL
          AND target.month_start IS NOT NULL
    )::BIGINT AS unexpected_month_count
FROM national_monthly_expected_kpi AS expected
FULL JOIN analytics.national_monthly_kpi AS target
    ON target.month_start = expected.month_start;

-- 3. 基礎 KPI 對帳；所有差異筆數預期皆為 0。
SELECT
    COUNT(*) FILTER (
        WHERE target.month_start IS NULL
    )::BIGINT AS missing_target_month_count,
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
FROM national_monthly_expected_kpi AS expected
LEFT JOIN analytics.national_monthly_kpi AS target
    ON target.month_start = expected.month_start;

-- 4. YoY 對帳；所有差異筆數預期皆為 0。
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
FROM national_monthly_expected_kpi AS expected
LEFT JOIN analytics.national_monthly_kpi AS target
    ON target.month_start = expected.month_start;

-- 5. 只列出有差異的月份，最多 50 個，方便定位問題。
-- 正常情況下查詢回傳 0 筆。
SELECT
    COALESCE(expected.month_start, target.month_start) AS month_start,
    expected.month_start IS NULL AS unexpected_target_month,
    target.month_start IS NULL AS missing_target_month,
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
FROM national_monthly_expected_kpi AS expected
FULL JOIN analytics.national_monthly_kpi AS target
    ON target.month_start = expected.month_start
WHERE ROW(
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
ORDER BY month_start
LIMIT 50;

COMMIT;
