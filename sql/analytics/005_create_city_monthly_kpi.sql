-- 建立儀表板使用的縣市月 KPI 表。
-- 粒度：每個曆月、每個台灣縣市一筆。
-- month_start 固定為每月第一天；county_id 是穩定鍵，city 是顯示名稱。
-- 沒有價格完整交易的月份仍保留，案件數為 0、價格指標為 NULL。
-- YoY 以小數比率儲存，例如 0.05000000 代表 5%。

BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.city_monthly_kpi (
    month_start DATE NOT NULL,
    county_id TEXT NOT NULL,
    city TEXT NOT NULL,

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

    CONSTRAINT city_monthly_kpi_pkey
        PRIMARY KEY (month_start, county_id),

    CONSTRAINT city_monthly_kpi_month_city_unique
        UNIQUE (month_start, city),

    CONSTRAINT city_monthly_kpi_month_start_check
        CHECK (EXTRACT(DAY FROM month_start) = 1),

    CONSTRAINT city_monthly_kpi_county_id_check
        CHECK (county_id ~ '^[0-9]{5}$'),

    CONSTRAINT city_monthly_kpi_city_check
        CHECK (
            city <> ''
            AND city = BTRIM(city)
        ),

    CONSTRAINT city_monthly_kpi_transaction_count_check
        CHECK (price_complete_transaction_count >= 0),

    CONSTRAINT city_monthly_kpi_price_population_check
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

    CONSTRAINT city_monthly_kpi_median_unit_price_yoy_range_check
        CHECK (
            median_unit_price_yoy IS NULL
            OR median_unit_price_yoy > -1
        ),

    CONSTRAINT city_monthly_kpi_avg_unit_price_yoy_range_check
        CHECK (
            avg_unit_price_yoy IS NULL
            OR avg_unit_price_yoy > -1
        ),

    CONSTRAINT city_monthly_kpi_median_total_price_yoy_range_check
        CHECK (
            median_total_price_yoy IS NULL
            OR median_total_price_yoy > -1
        ),

    CONSTRAINT city_monthly_kpi_avg_total_price_yoy_range_check
        CHECK (
            avg_total_price_yoy IS NULL
            OR avg_total_price_yoy > -1
        ),

    CONSTRAINT city_monthly_kpi_count_yoy_check
        CHECK (
            price_complete_transaction_count_yoy IS NULL
            OR price_complete_transaction_count_yoy >= -1
        ),

    CONSTRAINT city_monthly_kpi_median_unit_price_yoy_dependency_check
        CHECK (
            median_unit_price_10k_ping IS NOT NULL
            OR median_unit_price_yoy IS NULL
        ),

    CONSTRAINT city_monthly_kpi_avg_unit_price_yoy_dependency_check
        CHECK (
            avg_unit_price_10k_ping IS NOT NULL
            OR avg_unit_price_yoy IS NULL
        ),

    CONSTRAINT city_monthly_kpi_median_total_price_yoy_dependency_check
        CHECK (
            median_total_price_10k IS NOT NULL
            OR median_total_price_yoy IS NULL
        ),

    CONSTRAINT city_monthly_kpi_avg_total_price_yoy_dependency_check
        CHECK (
            avg_total_price_10k IS NOT NULL
            OR avg_total_price_yoy IS NULL
        )
);

-- 主鍵以 month_start 開頭，支援選定月份的地圖與排名查詢；
-- 第二個索引支援單一縣市在一段月份內的時間序列查詢。
CREATE INDEX IF NOT EXISTS city_monthly_kpi_county_month_idx
    ON analytics.city_monthly_kpi (county_id, month_start);

COMMENT ON TABLE analytics.city_monthly_kpi IS
    'Monthly housing-market KPI by Taiwan county/city for dashboard map, ranking, and trend views.';

COMMENT ON COLUMN analytics.city_monthly_kpi.month_start IS
    'First day of the transaction calendar month.';

COMMENT ON COLUMN analytics.city_monthly_kpi.county_id IS
    'Stable five-digit county/city identifier derived from core.dim_location.';

COMMENT ON COLUMN analytics.city_monthly_kpi.city IS
    'Canonical county/city display name derived from core.dim_location.';

COMMENT ON COLUMN analytics.city_monthly_kpi.median_unit_price_10k_ping IS
    'Median unit price, in NTD 10,000 per ping, among price-complete transactions.';

COMMENT ON COLUMN analytics.city_monthly_kpi.avg_unit_price_10k_ping IS
    'Average unit price, in NTD 10,000 per ping, among price-complete transactions.';

COMMENT ON COLUMN analytics.city_monthly_kpi.median_total_price_10k IS
    'Median total transaction price, in NTD 10,000, among price-complete transactions.';

COMMENT ON COLUMN analytics.city_monthly_kpi.avg_total_price_10k IS
    'Average total transaction price, in NTD 10,000, among price-complete transactions.';

COMMENT ON COLUMN analytics.city_monthly_kpi.price_complete_transaction_count IS
    'Number of eligible transactions with both total price and unit price present.';

COMMENT ON COLUMN analytics.city_monthly_kpi.median_unit_price_yoy IS
    'Year-over-year decimal rate for median unit price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.city_monthly_kpi.avg_unit_price_yoy IS
    'Year-over-year decimal rate for average unit price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.city_monthly_kpi.median_total_price_yoy IS
    'Year-over-year decimal rate for median total price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.city_monthly_kpi.avg_total_price_yoy IS
    'Year-over-year decimal rate for average total price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.city_monthly_kpi.price_complete_transaction_count_yoy IS
    'Year-over-year decimal rate for price-complete transaction count; NULL when the comparison is unavailable or the prior-year count is zero.';

COMMIT;
