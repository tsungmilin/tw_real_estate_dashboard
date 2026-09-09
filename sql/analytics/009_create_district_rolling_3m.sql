-- 建立儀表板使用的鄉鎮市區三個月滾動 KPI 表。
-- 粒度：每個錨點月份、每個台灣鄉鎮市區一筆。
-- 每筆涵蓋錨點月及前兩個曆月；只發布擁有完整三個月視窗的錨點。
-- 視窗內沒有價格完整交易時仍保留該地區，案件數為 0、中位單價為 NULL。
-- 中位數與平均數直接從視窗內合格交易重算，不合併月彙總值。
-- YoY 比較目前滾動值與正好一年前錨點月份的滾動值。

BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.district_rolling_3m (
    month_start DATE NOT NULL,
    county_id TEXT NOT NULL,
    town_id TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT NOT NULL,

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

    CONSTRAINT district_rolling_3m_pkey
        PRIMARY KEY (month_start, town_id),

    CONSTRAINT district_rolling_3m_month_county_district_unique
        UNIQUE (month_start, county_id, district),

    CONSTRAINT district_rolling_3m_month_start_check
        CHECK (EXTRACT(DAY FROM month_start) = 1),

    CONSTRAINT district_rolling_3m_county_id_check
        CHECK (county_id ~ '^[0-9]{5}$'),

    CONSTRAINT district_rolling_3m_town_id_check
        CHECK (town_id ~ '^[0-9]{8}$'),

    CONSTRAINT district_rolling_3m_city_check
        CHECK (
            city <> ''
            AND city = BTRIM(city)
        ),

    CONSTRAINT district_rolling_3m_district_check
        CHECK (
            district <> ''
            AND district = BTRIM(district)
        ),

    CONSTRAINT district_rolling_3m_transaction_count_check
        CHECK (price_complete_transaction_count >= 0),

    CONSTRAINT district_rolling_3m_price_population_check
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

    CONSTRAINT district_rolling_3m_median_unit_price_yoy_range_check
        CHECK (
            median_unit_price_yoy IS NULL
            OR median_unit_price_yoy > -1
        ),

    CONSTRAINT district_rolling_3m_avg_unit_price_yoy_range_check
        CHECK (
            avg_unit_price_yoy IS NULL
            OR avg_unit_price_yoy > -1
        ),

    CONSTRAINT district_rolling_3m_median_total_price_yoy_range_check
        CHECK (
            median_total_price_yoy IS NULL
            OR median_total_price_yoy > -1
        ),

    CONSTRAINT district_rolling_3m_avg_total_price_yoy_range_check
        CHECK (
            avg_total_price_yoy IS NULL
            OR avg_total_price_yoy > -1
        ),

    CONSTRAINT district_rolling_3m_count_yoy_range_check
        CHECK (
            price_complete_transaction_count_yoy IS NULL
            OR price_complete_transaction_count_yoy >= -1
        ),

    CONSTRAINT district_rolling_3m_median_unit_price_yoy_dependency_check
        CHECK (
            median_unit_price_10k_ping IS NOT NULL
            OR median_unit_price_yoy IS NULL
        ),

    CONSTRAINT district_rolling_3m_avg_unit_price_yoy_dependency_check
        CHECK (
            avg_unit_price_10k_ping IS NOT NULL
            OR avg_unit_price_yoy IS NULL
        ),

    CONSTRAINT district_rolling_3m_median_total_price_yoy_dependency_check
        CHECK (
            median_total_price_10k IS NOT NULL
            OR median_total_price_yoy IS NULL
        ),

    CONSTRAINT district_rolling_3m_avg_total_price_yoy_dependency_check
        CHECK (
            avg_total_price_10k IS NOT NULL
            OR avg_total_price_yoy IS NULL
        )
);

-- 主鍵以 month_start 開頭，支援單月快照；第二個索引支援儀表板主要查詢：
-- 指定一個縣市與錨點月份，取得其所有鄉鎮市區。
CREATE INDEX IF NOT EXISTS district_rolling_3m_county_month_idx
    ON analytics.district_rolling_3m (county_id, month_start, town_id);

COMMENT ON TABLE analytics.district_rolling_3m IS
    'Rolling-three-month housing KPI by anchor month and Taiwan district/town for the dashboard district scatter plot.';

COMMENT ON COLUMN analytics.district_rolling_3m.month_start IS
    'First day of the anchor calendar month, which is the final month of the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.county_id IS
    'Stable five-digit county/city identifier derived from core.dim_location.';

COMMENT ON COLUMN analytics.district_rolling_3m.town_id IS
    'Stable eight-digit district/town identifier derived from core.dim_location.';

COMMENT ON COLUMN analytics.district_rolling_3m.city IS
    'Canonical county/city display name derived from core.dim_location.';

COMMENT ON COLUMN analytics.district_rolling_3m.district IS
    'Canonical district/town display name derived from core.dim_location.';

COMMENT ON COLUMN analytics.district_rolling_3m.median_unit_price_10k_ping IS
    'Median unit price, in NTD 10,000 per ping, recalculated from price-complete transactions in the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.avg_unit_price_10k_ping IS
    'Average unit price, in NTD 10,000 per ping, recalculated from price-complete transactions in the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.median_total_price_10k IS
    'Median total transaction price, in NTD 10,000, recalculated from price-complete transactions in the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.avg_total_price_10k IS
    'Average total transaction price, in NTD 10,000, recalculated from price-complete transactions in the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.price_complete_transaction_count IS
    'Number of eligible price-complete transactions in the inclusive three-month window.';

COMMENT ON COLUMN analytics.district_rolling_3m.median_unit_price_yoy IS
    'Year-over-year decimal rate for rolling-three-month median unit price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.district_rolling_3m.avg_unit_price_yoy IS
    'Year-over-year decimal rate for rolling-three-month average unit price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.district_rolling_3m.median_total_price_yoy IS
    'Year-over-year decimal rate for rolling-three-month median total price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.district_rolling_3m.avg_total_price_yoy IS
    'Year-over-year decimal rate for rolling-three-month average total price; NULL when the comparison is unavailable.';

COMMENT ON COLUMN analytics.district_rolling_3m.price_complete_transaction_count_yoy IS
    'Year-over-year decimal rate for rolling-three-month price-complete transaction count; NULL when the comparison is unavailable or the prior-year count is zero.';

COMMIT;
