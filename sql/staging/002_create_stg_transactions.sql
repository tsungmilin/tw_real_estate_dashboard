-- Create the persistent staging transaction table and its data-quality constraints.
-- Requires the staging schema; preserves existing rows and returns no result set.
BEGIN;

CREATE TABLE IF NOT EXISTS staging.stg_transactions (
    cleaning_run_id TEXT NOT NULL,
    source_transaction_id TEXT PRIMARY KEY,
    transaction_year SMALLINT NOT NULL,
    transaction_month SMALLINT NOT NULL,
    transaction_date DATE,
    transaction_date_valid BOOLEAN NOT NULL,
    completion_date DATE,
    completion_date_status TEXT NOT NULL,
    completion_after_transaction BOOLEAN,
    county_id TEXT NOT NULL,
    town_id TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    urban_land_use_type TEXT,
    nonurban_land_use_zone TEXT,
    building_type TEXT,
    primary_use TEXT,
    has_management BOOLEAN,
    land_transfer_area_m2 DOUBLE PRECISION,
    building_transfer_area_m2 DOUBLE PRECISION,
    room_count INTEGER,
    hall_count INTEGER,
    bathroom_count INTEGER,
    total_price_ntd BIGINT,
    unit_price_ntd_m2 DOUBLE PRECISION,
    total_price_valid BOOLEAN NOT NULL,
    unit_price_valid BOOLEAN NOT NULL,
    land_transfer_area_valid BOOLEAN NOT NULL,
    building_transfer_area_valid BOOLEAN,
    parking_data_complete BOOLEAN,
    has_note BOOLEAN NOT NULL,
    load_batch_id TEXT NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT stg_transactions_year_check
        CHECK (transaction_year BETWEEN 2012 AND 2024),
    CONSTRAINT stg_transactions_month_check
        CHECK (transaction_month BETWEEN 1 AND 12),
    CONSTRAINT stg_transactions_completion_status_check
        CHECK (completion_date_status IN (
            'valid', 'missing', 'invalid', 'ambiguous_5_digit', 'not_applicable'
        )),
    CONSTRAINT stg_transactions_location_id_check
        CHECK (length(county_id) = 5 AND length(town_id) = 8),
    CONSTRAINT stg_transactions_type_check
        CHECK (transaction_type IN (
            '房地(土地+建物)', '房地(土地+建物)+車位', '土地'
        )),
    CONSTRAINT stg_transactions_area_check
        CHECK (
            (land_transfer_area_m2 IS NULL OR land_transfer_area_m2 > 0)
            AND (building_transfer_area_m2 IS NULL OR building_transfer_area_m2 > 0)
        ),
    CONSTRAINT stg_transactions_price_check
        CHECK (
            (total_price_ntd IS NULL OR total_price_ntd > 0)
            AND (unit_price_ntd_m2 IS NULL OR unit_price_ntd_m2 > 0)
        ),
    CONSTRAINT stg_transactions_layout_check
        CHECK (
            (room_count IS NULL OR room_count >= 0)
            AND (hall_count IS NULL OR hall_count >= 0)
            AND (bathroom_count IS NULL OR bathroom_count >= 0)
        )
);

CREATE INDEX IF NOT EXISTS idx_stg_transactions_cleaning_run
    ON staging.stg_transactions (cleaning_run_id);

CREATE INDEX IF NOT EXISTS idx_stg_transactions_load_batch
    ON staging.stg_transactions (load_batch_id);

COMMENT ON TABLE staging.stg_transactions IS
    'Current active validated transactions accumulated through UPSERT-only clean Parquet loads.';
COMMENT ON COLUMN staging.stg_transactions.source_transaction_id IS
    'Stable natural key from the source; used for idempotent UPSERT.';
COMMENT ON COLUMN staging.stg_transactions.load_batch_id IS
    'PostgreSQL load batch that last changed this row.';

COMMIT;
