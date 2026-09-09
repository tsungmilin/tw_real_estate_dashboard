-- Create the persistent transaction fact table used by analytics refreshes.
-- Requires both core dimensions; preserves existing rows and returns no result set.
CREATE TABLE IF NOT EXISTS core.fact_transactions (
    source_transaction_id TEXT PRIMARY KEY,
    transaction_month DATE NOT NULL,

    location_id SMALLINT NOT NULL
        REFERENCES core.dim_location (location_id),

    building_type_id SMALLINT NOT NULL
        REFERENCES core.dim_building_type (building_type_id),

    transaction_type TEXT NOT NULL,
    total_price_ntd BIGINT,
    unit_price_ntd_m2 DOUBLE PRECISION,

    CONSTRAINT fact_transactions_month_check
        CHECK (
            EXTRACT(DAY FROM transaction_month) = 1
            AND transaction_month BETWEEN
                DATE '2012-08-01' AND DATE '2024-12-01'
        ),

    CONSTRAINT fact_transactions_type_check
        CHECK (
            transaction_type IN (
                '土地',
                '房地(土地+建物)',
                '房地(土地+建物)+車位'
            )
        ),

    CONSTRAINT fact_transactions_total_price_check
        CHECK (
            total_price_ntd IS NULL
            OR total_price_ntd > 0
        ),

    CONSTRAINT fact_transactions_unit_price_check
        CHECK (
            unit_price_ntd_m2 IS NULL
            OR unit_price_ntd_m2 > 0
        ),

    CONSTRAINT fact_transactions_building_type_check
        CHECK (
            (
                transaction_type = '土地'
                AND building_type_id = 0
            )
            OR (
                transaction_type IN (
                    '房地(土地+建物)',
                    '房地(土地+建物)+車位'
                )
                AND building_type_id <> 0
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_fact_transactions_month_location
    ON core.fact_transactions (transaction_month, location_id);

COMMENT ON TABLE core.fact_transactions IS
    '每列代表一筆清理後實價登錄交易。';

COMMENT ON COLUMN core.fact_transactions.source_transaction_id IS
    '來源 no 欄位；核心層的交易自然鍵。';

COMMENT ON COLUMN core.fact_transactions.transaction_month IS
    '交易月份，固定使用該月第一天。';
