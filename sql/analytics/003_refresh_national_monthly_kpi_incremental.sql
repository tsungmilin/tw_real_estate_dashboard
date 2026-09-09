-- 依一個成功的 core 批次增量更新全國月 KPI。
-- 必須透過 psql 傳入 --set=load_batch_id=<明確的批次 ID>。

\if :{?load_batch_id}
\else
\echo '缺少必要的 psql 變數：load_batch_id'
\quit 3
\endif

BEGIN;

-- 同一時間只允許一個全國月更新，避免不同批次
-- 同時更新相同月份。
DO $national_monthly_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.national_monthly_kpi.refresh', 0)
    );
END
$national_monthly_refresh_lock$;

-- 將 psql 輸入、固定起始月份與尾端案件數門檻集中在一列，
-- 供後續所有陳述式使用。
CREATE TEMP TABLE national_monthly_refresh_parameter (
    load_batch_id TEXT PRIMARY KEY,
    analysis_start_month DATE NOT NULL,
    tail_volume_threshold NUMERIC(5,4) NOT NULL,

    CONSTRAINT national_monthly_refresh_parameter_month_check
        CHECK (EXTRACT(DAY FROM analysis_start_month) = 1),

    CONSTRAINT national_monthly_refresh_parameter_threshold_check
        CHECK (
            tail_volume_threshold > 0
            AND tail_volume_threshold <= 1
        )
) ON COMMIT DROP;

INSERT INTO national_monthly_refresh_parameter (
    load_batch_id,
    analysis_start_month,
    tail_volume_threshold
)
VALUES (
    :'load_batch_id',
    DATE '2012-08-01',
    0.75
);

-- 先驗證指定的 core batch，避免無效輸入仍掃描整張 core fact。
DO $national_monthly_refresh_core_batch_gate$
DECLARE
    v_load_batch_id TEXT;
    v_core_status TEXT;
BEGIN
    SELECT parameter.load_batch_id
    INTO v_load_batch_id
    FROM national_monthly_refresh_parameter AS parameter;

    SELECT sync_run.status
    INTO v_core_status
    FROM core.sync_runs AS sync_run
    WHERE sync_run.load_batch_id = v_load_batch_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Analytics refresh input validation failed: core batch % does not exist',
            v_load_batch_id;
    END IF;

    IF v_core_status <> 'success' THEN
        RAISE EXCEPTION
            'Analytics refresh input validation failed: core batch % has status %, expected success',
            v_load_batch_id,
            v_core_status;
    END IF;
END
$national_monthly_refresh_core_batch_gate$;

-- 直接從 core 計算尾端完整性判斷：
-- 1. 先算每月價格完整房屋交易案件數。
-- 2. 以所有月份案件數的中位數作為基準。
-- 3. 取案件數 >= 基準 75% 的最晚月份為候選截止。
-- 4. 增量更新只允許延伸，不自動縮短既有分析範圍。
CREATE TEMP TABLE national_monthly_refresh_cutoff (
    baseline_transaction_count NUMERIC,
    tail_volume_threshold NUMERIC(5,4) NOT NULL,
    candidate_end_month DATE,
    current_end_month DATE,
    effective_end_month DATE
) ON COMMIT DROP;

WITH monthly_transaction_counts AS (
    SELECT
        fact.transaction_month AS month_start,
        COUNT(fact.source_transaction_id)::BIGINT
            AS price_complete_transaction_count
    FROM core.fact_transactions AS fact
    CROSS JOIN national_monthly_refresh_parameter AS parameter
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
    SELECT
        MAX(monthly.month_start)::DATE AS candidate_end_month
    FROM monthly_transaction_counts AS monthly
    CROSS JOIN baseline
    CROSS JOIN national_monthly_refresh_parameter AS parameter
    WHERE monthly.price_complete_transaction_count
          >= baseline.baseline_transaction_count
             * parameter.tail_volume_threshold
),
current_cutoff AS (
    SELECT MAX(target.month_start)::DATE AS current_end_month
    FROM analytics.national_monthly_kpi AS target
)
INSERT INTO national_monthly_refresh_cutoff (
    baseline_transaction_count,
    tail_volume_threshold,
    candidate_end_month,
    current_end_month,
    effective_end_month
)
SELECT
    baseline.baseline_transaction_count,
    parameter.tail_volume_threshold,
    candidate_cutoff.candidate_end_month,
    current_cutoff.current_end_month,
    GREATEST(
        current_cutoff.current_end_month,
        candidate_cutoff.candidate_end_month
    ) AS effective_end_month
FROM baseline
CROSS JOIN candidate_cutoff
CROSS JOIN current_cutoff
CROSS JOIN national_monthly_refresh_parameter AS parameter;

-- 增量更新只接受已完成的 core 批次，而且要求完整更新
-- 已先建立從固定起點到目前截止月份的完整月份骨架。
DO $national_monthly_refresh_input_gate$
DECLARE
    v_analysis_start_month DATE;
    v_baseline_transaction_count NUMERIC;
    v_candidate_end_month DATE;
    v_current_end_month DATE;
    v_effective_end_month DATE;
    v_missing_month DATE;
BEGIN
    SELECT
        parameter.analysis_start_month,
        cutoff.baseline_transaction_count,
        cutoff.candidate_end_month,
        cutoff.current_end_month,
        cutoff.effective_end_month
    INTO
        v_analysis_start_month,
        v_baseline_transaction_count,
        v_candidate_end_month,
        v_current_end_month,
        v_effective_end_month
    FROM national_monthly_refresh_parameter AS parameter
    CROSS JOIN national_monthly_refresh_cutoff AS cutoff;

    IF v_baseline_transaction_count IS NULL
       OR v_candidate_end_month IS NULL THEN
        RAISE EXCEPTION
            'Analytics cutoff calculation failed: no qualifying core month exists on or after %',
            v_analysis_start_month;
    END IF;

    IF v_current_end_month IS NULL THEN
        RAISE EXCEPTION
            'Analytics incremental refresh requires a completed full refresh; target table is empty';
    END IF;

    IF v_current_end_month < v_analysis_start_month THEN
        RAISE EXCEPTION
            'Analytics incremental refresh requires a completed full refresh; current cutoff % is before analysis start %',
            v_current_end_month,
            v_analysis_start_month;
    END IF;

    IF v_effective_end_month IS NULL THEN
        RAISE EXCEPTION
            'Analytics cutoff calculation failed: effective cutoff is NULL';
    END IF;

    SELECT expected_month.month_start::DATE
    INTO v_missing_month
    FROM GENERATE_SERIES(
        v_analysis_start_month,
        v_current_end_month,
        INTERVAL '1 month'
    ) AS expected_month(month_start)
    LEFT JOIN analytics.national_monthly_kpi AS target
        ON target.month_start = expected_month.month_start::DATE
    WHERE target.month_start IS NULL
    ORDER BY expected_month.month_start
    LIMIT 1;

    IF v_missing_month IS NOT NULL THEN
        RAISE EXCEPTION
            'Analytics incremental refresh requires a completed full refresh; missing target month %',
            v_missing_month;
    END IF;
END
$national_monthly_refresh_input_gate$;

-- M 包含兩類月份：
-- 1. 這個 core batch 實際影響、且已位於有效分析範圍內的月份。
-- 2. 候選截止向後延伸時，從原截止下一月至新截止的所有月份。
-- 第二類使用完整月序列，因此不會因中間月份未達 75% 而產生缺口。
CREATE TEMP TABLE national_monthly_affected_base_months (
    month_start DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO national_monthly_affected_base_months (month_start)
SELECT month_change.month_start
FROM staging.load_month_changes AS month_change
JOIN national_monthly_refresh_parameter AS parameter
    ON month_change.load_batch_id = parameter.load_batch_id
CROSS JOIN national_monthly_refresh_cutoff AS cutoff
WHERE month_change.has_core_impact
  AND month_change.month_start
          BETWEEN parameter.analysis_start_month
              AND cutoff.effective_end_month

UNION

SELECT generated_month.month_start::DATE
FROM national_monthly_refresh_cutoff AS cutoff
CROSS JOIN LATERAL GENERATE_SERIES(
    cutoff.current_end_month + INTERVAL '1 month',
    cutoff.effective_end_month,
    INTERVAL '1 month'
) AS generated_month(month_start);

-- M 本身的 YoY 會改變；M + 12 個月以 M 為去年基準，YoY 也會改變。
CREATE TEMP TABLE national_monthly_affected_yoy_months (
    month_start DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO national_monthly_affected_yoy_months (month_start)
SELECT affected.month_start
FROM national_monthly_affected_base_months AS affected

UNION

SELECT (affected.month_start + INTERVAL '1 year')::DATE
FROM national_monthly_affected_base_months AS affected
CROSS JOIN national_monthly_refresh_cutoff AS cutoff
WHERE (affected.month_start + INTERVAL '1 year')::DATE
          <= cutoff.effective_end_month;

-- 只從 core 重算受影響月份 M 的價格與交易量。LEFT JOIN 保留
-- 已無符合 KPI 交易的月份，使該月 count = 0、價格 = NULL。
WITH refreshed_base_values AS (
    SELECT
        affected.month_start,

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

        COUNT(fact.source_transaction_id)
            AS price_complete_transaction_count

    FROM national_monthly_affected_base_months AS affected
    LEFT JOIN core.fact_transactions AS fact
        ON fact.transaction_month = affected.month_start
       AND fact.transaction_type IN (
           '房地(土地+建物)',
           '房地(土地+建物)+車位'
       )
       AND fact.total_price_ntd IS NOT NULL
       AND fact.unit_price_ntd_m2 IS NOT NULL
    GROUP BY affected.month_start
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
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
FROM refreshed_base_values

ON CONFLICT (month_start)
DO UPDATE SET
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

-- 更新 M 的月統計後，用目前 analytics 表的值重算 M 與 M+12 的 YoY；
-- 不需要再次掃描 core.fact_transactions。
WITH recalculated_yoy AS (
    SELECT
        current_month.month_start,

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

    FROM national_monthly_affected_yoy_months AS affected
    JOIN analytics.national_monthly_kpi AS current_month
        ON current_month.month_start = affected.month_start
    LEFT JOIN analytics.national_monthly_kpi AS previous_year
        ON previous_year.month_start =
           (current_month.month_start - INTERVAL '1 year')::DATE
)

UPDATE analytics.national_monthly_kpi AS target
SET
    median_unit_price_yoy = recalculated.median_unit_price_yoy,
    avg_unit_price_yoy = recalculated.avg_unit_price_yoy,
    median_total_price_yoy = recalculated.median_total_price_yoy,
    avg_total_price_yoy = recalculated.avg_total_price_yoy,
    price_complete_transaction_count_yoy =
        recalculated.price_complete_transaction_count_yoy
FROM recalculated_yoy AS recalculated
WHERE target.month_start = recalculated.month_start;

-- 回傳載入器與協調器可讀取的摘要。候選截止早於 current_end_month 時，
-- effective_end_month 仍保留目前截止，交由人工檢查。
SELECT
    parameter.load_batch_id,
    cutoff.baseline_transaction_count,
    cutoff.tail_volume_threshold,
    cutoff.candidate_end_month,
    cutoff.current_end_month,
    cutoff.effective_end_month,
    (
        SELECT COUNT(*)::BIGINT
        FROM national_monthly_affected_base_months
    ) AS affected_base_month_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM national_monthly_affected_yoy_months
    ) AS affected_yoy_month_count,
    (
        SELECT ARRAY_AGG(month_start ORDER BY month_start)
        FROM national_monthly_affected_base_months
    ) AS affected_base_months,
    (
        SELECT ARRAY_AGG(month_start ORDER BY month_start)
        FROM national_monthly_affected_yoy_months
    ) AS affected_yoy_months
FROM national_monthly_refresh_parameter AS parameter
CROSS JOIN national_monthly_refresh_cutoff AS cutoff;

COMMIT;
