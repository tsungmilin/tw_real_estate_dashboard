-- 依一個成功的 core 批次增量更新鄉鎮市區三個月滾動 KPI。
-- 必須透過 psql 傳入 --set=load_batch_id=<明確的批次 ID>。
-- 應先執行全國與縣市增量更新；全國 Analytics 管理本次使用的發布截止月份。

\if :{?load_batch_id}
\else
\echo '缺少必要的 psql 變數：load_batch_id'
\quit 3
\endif

BEGIN;

-- 完整與增量更新共用同一把鎖，避免同時修改鄉鎮市區 KPI 表。
DO $district_rolling_refresh_lock$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('analytics.district_rolling_3m.refresh', 0)
    );
END
$district_rolling_refresh_lock$;

CREATE TEMP TABLE district_rolling_refresh_parameter (
    load_batch_id TEXT PRIMARY KEY,
    analysis_start_month DATE NOT NULL,
    first_anchor_month DATE NOT NULL,

    CONSTRAINT district_rolling_refresh_parameter_month_check
        CHECK (
            EXTRACT(DAY FROM analysis_start_month) = 1
            AND EXTRACT(DAY FROM first_anchor_month) = 1
        ),

    CONSTRAINT district_rolling_refresh_parameter_window_check
        CHECK (
            first_anchor_month =
            (analysis_start_month + INTERVAL '2 months')::DATE
        )
) ON COMMIT DROP;

INSERT INTO district_rolling_refresh_parameter (
    load_batch_id,
    analysis_start_month,
    first_anchor_month
)
VALUES (
    :'load_batch_id',
    DATE '2012-08-01',
    DATE '2012-10-01'
);

-- 讀取受影響交易月份前，先拒絕不存在或未完成的 Core 批次。
DO $district_rolling_refresh_core_batch_gate$
DECLARE
    v_load_batch_id TEXT;
    v_core_status TEXT;
BEGIN
    SELECT parameter.load_batch_id
    INTO v_load_batch_id
    FROM district_rolling_refresh_parameter AS parameter;

    SELECT sync_run.status
    INTO v_core_status
    FROM core.sync_runs AS sync_run
    WHERE sync_run.load_batch_id = v_load_batch_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'District Analytics refresh input validation failed: core batch % does not exist',
            v_load_batch_id;
    END IF;

    IF v_core_status <> 'success' THEN
        RAISE EXCEPTION
            'District Analytics refresh input validation failed: core batch % has status %, expected success',
            v_load_batch_id,
            v_core_status;
    END IF;
END
$district_rolling_refresh_core_batch_gate$;

-- 從 Core 動態取得目前鄉鎮市區成員，不寫死現行參照筆數。
CREATE TEMP TABLE district_rolling_refresh_locations
ON COMMIT DROP
AS
SELECT
    location.location_id,
    location.county_id,
    location.town_id,
    location.city,
    location.district
FROM core.dim_location AS location;

CREATE TEMP TABLE district_rolling_refresh_range
ON COMMIT DROP
AS
SELECT
    MIN(national.month_start)::DATE AS national_start_month,
    MAX(national.month_start)::DATE AS published_end_month,
    (
        SELECT MIN(district.month_start)::DATE
        FROM analytics.district_rolling_3m AS district
    ) AS current_start_month,
    (
        SELECT MAX(district.month_start)::DATE
        FROM analytics.district_rolling_3m AS district
    ) AS current_end_month
FROM analytics.national_monthly_kpi AS national;

-- 修改目標列前，要求上游發布月份連續且既有鄉鎮市區骨架完整。
-- 地理參照成員異動必須經檢查後完整更新，讓所有歷史錨點套用新骨架。
DO $district_rolling_refresh_input_gate$
DECLARE
    v_analysis_start_month DATE;
    v_first_anchor_month DATE;
    v_national_start_month DATE;
    v_published_end_month DATE;
    v_current_start_month DATE;
    v_current_end_month DATE;
    v_missing_national_month DATE;
    v_missing_anchor_month DATE;
    v_missing_town_id TEXT;
    v_unexpected_anchor_month DATE;
    v_unexpected_town_id TEXT;
    v_location_count BIGINT;
BEGIN
    SELECT
        parameter.analysis_start_month,
        parameter.first_anchor_month,
        refresh_range.national_start_month,
        refresh_range.published_end_month,
        refresh_range.current_start_month,
        refresh_range.current_end_month
    INTO
        v_analysis_start_month,
        v_first_anchor_month,
        v_national_start_month,
        v_published_end_month,
        v_current_start_month,
        v_current_end_month
    FROM district_rolling_refresh_parameter AS parameter
    CROSS JOIN district_rolling_refresh_range AS refresh_range;

    IF v_published_end_month IS NULL THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh requires a populated analytics.national_monthly_kpi table';
    END IF;

    IF v_national_start_month <> v_analysis_start_month THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh expected national data to start at %, but found %',
            v_analysis_start_month,
            v_national_start_month;
    END IF;

    IF v_published_end_month < v_first_anchor_month THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh requires a published end month on or after %, but found %',
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
            'District Analytics incremental refresh found a missing national month: %',
            v_missing_national_month;
    END IF;

    SELECT COUNT(*)::BIGINT
    INTO v_location_count
    FROM district_rolling_refresh_locations;

    IF v_location_count = 0 THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh requires a non-empty core.dim_location table';
    END IF;

    IF v_current_end_month IS NULL THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh requires a completed full refresh; target table is empty';
    END IF;

    IF v_current_start_month <> v_first_anchor_month THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh expected target data to start at %, but found %',
            v_first_anchor_month,
            v_current_start_month;
    END IF;

    IF v_current_end_month > v_published_end_month THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh found district end month % beyond national published end month %; run a reviewed full refresh',
            v_current_end_month,
            v_published_end_month;
    END IF;

    SELECT
        expected_anchor.anchor_month::DATE,
        location.town_id
    INTO
        v_missing_anchor_month,
        v_missing_town_id
    FROM GENERATE_SERIES(
        v_first_anchor_month,
        v_current_end_month,
        INTERVAL '1 month'
    ) AS expected_anchor(anchor_month)
    CROSS JOIN district_rolling_refresh_locations AS location
    LEFT JOIN analytics.district_rolling_3m AS target
        ON target.month_start = expected_anchor.anchor_month::DATE
       AND target.town_id = location.town_id
    WHERE target.month_start IS NULL
    ORDER BY
        expected_anchor.anchor_month,
        location.town_id
    LIMIT 1;

    IF v_missing_anchor_month IS NOT NULL THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh requires a complete existing spine; missing target row for anchor %, town_id %',
            v_missing_anchor_month,
            v_missing_town_id;
    END IF;

    SELECT
        target.month_start,
        target.town_id
    INTO
        v_unexpected_anchor_month,
        v_unexpected_town_id
    FROM analytics.district_rolling_3m AS target
    LEFT JOIN district_rolling_refresh_locations AS location
        ON location.town_id = target.town_id
       AND location.county_id = target.county_id
       AND location.city = target.city
       AND location.district = target.district
    WHERE target.month_start
              BETWEEN v_first_anchor_month AND v_current_end_month
      AND location.town_id IS NULL
    ORDER BY
        target.month_start,
        target.town_id
    LIMIT 1;

    IF v_unexpected_anchor_month IS NOT NULL THEN
        RAISE EXCEPTION
            'District Analytics incremental refresh found a target location/name not present in core.dim_location: anchor %, town_id %; run a reviewed full refresh',
            v_unexpected_anchor_month,
            v_unexpected_town_id;
    END IF;
END
$district_rolling_refresh_input_gate$;

-- 異動交易月份 M 會影響滾動錨點 M、M+1 與 M+2；新發布的全國月份即使未列於
-- 本批次變更紀錄，也會納入。
CREATE TEMP TABLE district_rolling_affected_base_anchors (
    anchor_month DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO district_rolling_affected_base_anchors (anchor_month)
SELECT DISTINCT
    (
        month_change.month_start
        + MAKE_INTERVAL(months => month_offset.offset_value)
    )::DATE AS anchor_month
FROM staging.load_month_changes AS month_change
JOIN district_rolling_refresh_parameter AS parameter
    ON month_change.load_batch_id = parameter.load_batch_id
CROSS JOIN GENERATE_SERIES(0, 2) AS month_offset(offset_value)
CROSS JOIN district_rolling_refresh_range AS refresh_range
WHERE month_change.has_core_impact
  AND (
      month_change.month_start
      + MAKE_INTERVAL(months => month_offset.offset_value)
  )::DATE BETWEEN
      parameter.first_anchor_month
      AND refresh_range.published_end_month

UNION

SELECT generated_anchor.anchor_month::DATE
FROM district_rolling_refresh_range AS refresh_range
CROSS JOIN LATERAL GENERATE_SERIES(
    refresh_range.current_end_month + INTERVAL '1 month',
    refresh_range.published_end_month,
    INTERVAL '1 month'
) AS generated_anchor(anchor_month);

-- 重算基礎錨點會改變自身 YoY，以及一年後以它為比較基準的錨點 YoY。
CREATE TEMP TABLE district_rolling_affected_yoy_anchors (
    anchor_month DATE PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO district_rolling_affected_yoy_anchors (anchor_month)
SELECT affected.anchor_month
FROM district_rolling_affected_base_anchors AS affected

UNION

SELECT (affected.anchor_month + INTERVAL '1 year')::DATE
FROM district_rolling_affected_base_anchors AS affected
CROSS JOIN district_rolling_refresh_range AS refresh_range
WHERE (affected.anchor_month + INTERVAL '1 year')::DATE
          <= refresh_range.published_end_month;

-- 對每個受影響錨點重算目前所有地區。staging 變更紀錄只有月份粒度，無法安全判定
-- 更正交易的原地區與新地區。
WITH eligible_transactions AS (
    SELECT
        affected.anchor_month,
        fact.location_id,
        fact.source_transaction_id,
        fact.total_price_ntd,
        fact.unit_price_ntd_m2
    FROM district_rolling_affected_base_anchors AS affected
    JOIN core.fact_transactions AS fact
        ON fact.transaction_month BETWEEN
           (affected.anchor_month - INTERVAL '2 months')::DATE
           AND affected.anchor_month
    WHERE fact.transaction_type IN (
        '房地(土地+建物)',
        '房地(土地+建物)+車位'
    )
      AND fact.total_price_ntd IS NOT NULL
      AND fact.unit_price_ntd_m2 IS NOT NULL
),

rolling_aggregation AS (
    SELECT
        transaction.anchor_month,
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

    FROM eligible_transactions AS transaction
    GROUP BY
        transaction.anchor_month,
        transaction.location_id
),

refreshed_base_values AS (
    SELECT
        affected.anchor_month AS month_start,
        location.county_id,
        location.town_id,
        location.city,
        location.district,

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

    FROM district_rolling_affected_base_anchors AS affected
    CROSS JOIN district_rolling_refresh_locations AS location
    LEFT JOIN rolling_aggregation AS aggregation
        ON aggregation.anchor_month = affected.anchor_month
       AND aggregation.location_id = location.location_id
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
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
FROM refreshed_base_values

ON CONFLICT (month_start, town_id)
DO UPDATE SET
    county_id = EXCLUDED.county_id,
    city = EXCLUDED.city,
    district = EXCLUDED.district,
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

-- 從 Analytics 已更新的值重算 YoY，避免再次掃描 core.fact_transactions。
WITH recalculated_yoy AS (
    SELECT
        current_anchor.month_start,
        current_anchor.town_id,

        CASE
            WHEN current_anchor.median_unit_price_10k_ping IS NULL
              OR previous_year.median_unit_price_10k_ping IS NULL
            THEN NULL
            ELSE (
                current_anchor.median_unit_price_10k_ping
                / previous_year.median_unit_price_10k_ping
                - 1
            )::NUMERIC(14,8)
        END AS median_unit_price_yoy,

        CASE
            WHEN current_anchor.avg_unit_price_10k_ping IS NULL
              OR previous_year.avg_unit_price_10k_ping IS NULL
            THEN NULL
            ELSE (
                current_anchor.avg_unit_price_10k_ping
                / previous_year.avg_unit_price_10k_ping
                - 1
            )::NUMERIC(14,8)
        END AS avg_unit_price_yoy,

        CASE
            WHEN current_anchor.median_total_price_10k IS NULL
              OR previous_year.median_total_price_10k IS NULL
            THEN NULL
            ELSE (
                current_anchor.median_total_price_10k
                / previous_year.median_total_price_10k
                - 1
            )::NUMERIC(14,8)
        END AS median_total_price_yoy,

        CASE
            WHEN current_anchor.avg_total_price_10k IS NULL
              OR previous_year.avg_total_price_10k IS NULL
            THEN NULL
            ELSE (
                current_anchor.avg_total_price_10k
                / previous_year.avg_total_price_10k
                - 1
            )::NUMERIC(14,8)
        END AS avg_total_price_yoy,

        CASE
            WHEN previous_year.price_complete_transaction_count IS NULL
              OR previous_year.price_complete_transaction_count = 0
            THEN NULL
            ELSE (
                current_anchor.price_complete_transaction_count::NUMERIC
                / previous_year.price_complete_transaction_count
                - 1
            )::NUMERIC(14,8)
        END AS price_complete_transaction_count_yoy

    FROM district_rolling_affected_yoy_anchors AS affected
    JOIN analytics.district_rolling_3m AS current_anchor
        ON current_anchor.month_start = affected.anchor_month
    LEFT JOIN analytics.district_rolling_3m AS previous_year
        ON previous_year.town_id = current_anchor.town_id
       AND previous_year.month_start =
           (current_anchor.month_start - INTERVAL '1 year')::DATE
)

UPDATE analytics.district_rolling_3m AS target
SET
    median_unit_price_yoy = recalculated.median_unit_price_yoy,
    avg_unit_price_yoy = recalculated.avg_unit_price_yoy,
    median_total_price_yoy = recalculated.median_total_price_yoy,
    avg_total_price_yoy = recalculated.avg_total_price_yoy,
    price_complete_transaction_count_yoy =
        recalculated.price_complete_transaction_count_yoy
FROM recalculated_yoy AS recalculated
WHERE target.month_start = recalculated.month_start
  AND target.town_id = recalculated.town_id;

-- 回傳載入器與協調器使用的精簡執行摘要。
SELECT
    parameter.load_batch_id,
    refresh_range.current_end_month,
    refresh_range.published_end_month,
    (
        SELECT COUNT(*)::BIGINT
        FROM district_rolling_refresh_locations
    ) AS location_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM district_rolling_affected_base_anchors
    ) AS affected_base_anchor_count,
    (
        SELECT COUNT(*)::BIGINT
        FROM district_rolling_affected_yoy_anchors
    ) AS affected_yoy_anchor_count,
    (
        SELECT ARRAY_AGG(anchor_month ORDER BY anchor_month)
        FROM district_rolling_affected_base_anchors
    ) AS affected_base_anchors,
    (
        SELECT ARRAY_AGG(anchor_month ORDER BY anchor_month)
        FROM district_rolling_affected_yoy_anchors
    ) AS affected_yoy_anchors
FROM district_rolling_refresh_parameter AS parameter
CROSS JOIN district_rolling_refresh_range AS refresh_range;

COMMIT;
