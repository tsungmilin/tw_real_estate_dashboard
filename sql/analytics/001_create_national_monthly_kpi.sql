-- 建立全國月 KPI 表；本檔只調整結構，不計算 KPI 資料。

BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.national_monthly_kpi (
    month_start DATE NOT NULL,

    median_unit_price_10k_ping NUMERIC(18,6),
    avg_unit_price_10k_ping NUMERIC(18,6),

    median_total_price_10k NUMERIC(18,6),
    avg_total_price_10k NUMERIC(18,6),

    price_complete_transaction_count BIGINT NOT NULL,

    median_unit_price_yoy NUMERIC(14,8),
    avg_unit_price_yoy NUMERIC(14,8),

    median_total_price_yoy NUMERIC(14,8),
    avg_total_price_yoy NUMERIC(14,8),

    price_complete_transaction_count_yoy NUMERIC(14,8),

    CONSTRAINT national_monthly_kpi_pk
        PRIMARY KEY (month_start),

    CONSTRAINT national_monthly_kpi_month_start_check
        CHECK (EXTRACT(DAY FROM month_start) = 1),

    CONSTRAINT national_monthly_kpi_count_check
        CHECK (price_complete_transaction_count >= 0),

    CONSTRAINT national_monthly_kpi_price_population_check
        CHECK (
            (
                price_complete_transaction_count = 0
                AND median_unit_price_10k_ping IS NULL
                AND avg_unit_price_10k_ping IS NULL
                AND median_total_price_10k IS NULL
                AND avg_total_price_10k IS NULL
            )
            OR
            (
                price_complete_transaction_count > 0
                AND median_unit_price_10k_ping > 0
                AND avg_unit_price_10k_ping > 0
                AND median_total_price_10k > 0
                AND avg_total_price_10k > 0
            )
        ),

    CONSTRAINT national_monthly_kpi_median_unit_price_yoy_check
        CHECK (
            median_unit_price_yoy IS NULL
            OR median_unit_price_yoy > -1
        ),

    CONSTRAINT national_monthly_kpi_avg_unit_price_yoy_check
        CHECK (
            avg_unit_price_yoy IS NULL
            OR avg_unit_price_yoy > -1
        ),

    CONSTRAINT national_monthly_kpi_median_total_price_yoy_check
        CHECK (
            median_total_price_yoy IS NULL
            OR median_total_price_yoy > -1
        ),

    CONSTRAINT national_monthly_kpi_avg_total_price_yoy_check
        CHECK (
            avg_total_price_yoy IS NULL
            OR avg_total_price_yoy > -1
        ),

    CONSTRAINT national_monthly_kpi_count_yoy_check
        CHECK (
            price_complete_transaction_count_yoy IS NULL
            OR price_complete_transaction_count_yoy >= -1
        ),

    CONSTRAINT national_monthly_kpi_median_unit_price_yoy_dependency
        CHECK (
            median_unit_price_10k_ping IS NOT NULL
            OR median_unit_price_yoy IS NULL
        ),

    CONSTRAINT national_monthly_kpi_avg_unit_price_yoy_dependency
        CHECK (
            avg_unit_price_10k_ping IS NOT NULL
            OR avg_unit_price_yoy IS NULL
        ),

    CONSTRAINT national_monthly_kpi_median_total_price_yoy_dependency
        CHECK (
            median_total_price_10k IS NOT NULL
            OR median_total_price_yoy IS NULL
        ),

    CONSTRAINT national_monthly_kpi_avg_total_price_yoy_dependency
        CHECK (
            avg_total_price_10k IS NOT NULL
            OR avg_total_price_yoy IS NULL
        )
);

COMMENT ON TABLE analytics.national_monthly_kpi IS
    'One row per calendar month containing national price and transaction-count KPIs.';

COMMENT ON COLUMN analytics.national_monthly_kpi.month_start IS
    'First day of the calendar month represented by this KPI row.';

COMMENT ON COLUMN analytics.national_monthly_kpi.median_unit_price_10k_ping IS
    'Median unit price in ten-thousand New Taiwan dollars per ping.';

COMMENT ON COLUMN analytics.national_monthly_kpi.avg_unit_price_10k_ping IS
    'Average unit price in ten-thousand New Taiwan dollars per ping.';

COMMENT ON COLUMN analytics.national_monthly_kpi.median_total_price_10k IS
    'Median total transaction price in ten-thousand New Taiwan dollars.';

COMMENT ON COLUMN analytics.national_monthly_kpi.avg_total_price_10k IS
    'Average total transaction price in ten-thousand New Taiwan dollars.';

COMMENT ON COLUMN analytics.national_monthly_kpi.price_complete_transaction_count IS
    'Count of in-scope transactions with both total price and unit price present.';

COMMENT ON COLUMN analytics.national_monthly_kpi.median_unit_price_yoy IS
    'Year-over-year change of median unit price as a decimal ratio.';

COMMENT ON COLUMN analytics.national_monthly_kpi.avg_unit_price_yoy IS
    'Year-over-year change of average unit price as a decimal ratio.';

COMMENT ON COLUMN analytics.national_monthly_kpi.median_total_price_yoy IS
    'Year-over-year change of median total price as a decimal ratio.';

COMMENT ON COLUMN analytics.national_monthly_kpi.avg_total_price_yoy IS
    'Year-over-year change of average total price as a decimal ratio.';

COMMENT ON COLUMN analytics.national_monthly_kpi.price_complete_transaction_count_yoy IS
    'Year-over-year change of price-complete transaction count as a decimal ratio.';

COMMIT;
