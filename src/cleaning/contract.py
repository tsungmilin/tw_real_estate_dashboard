"""集中定義資料清理 v1 的固定輸入與輸出契約。"""

from __future__ import annotations

import pyarrow as pa


# 每次 run 都必須遵守同一份 spec 與預設 chunk 大小。
SPEC_VERSION = "cleaning_spec_v1"
DEFAULT_CHUNK_SIZE = 100_000

# 只有 checksum 相同的 raw source 才套用下列精確筆數 baseline。
CURRENT_SOURCE_SHA256 = (
    "1d81eb74a5c7f3607b93966732b90a7e3b8cdf5debacf905b67e1afab86b25b2"
)
CURRENT_SOURCE_BASELINE = {
    "input_rows": 4_380_208,
    "missing_source_transaction_id": 0,
    "duplicate_exact": 0,
    "duplicate_conflict": 0,
    "invalid_transaction_period": 34_519,
    "unmapped_location": 97,
    "excluded_transaction_type": 84_126,
    "excluded_rows": 118_742,
    "clean_rows": 4_261_466,
}

EXPECTED_LOCATION_ROWS = 368
EXPECTED_ALIAS_ROWS = 3

# 標準期別先以民國年月 YYYYMM 表示，方便轉成西元前篩選。
CANONICAL_PERIOD_START = 10108
CANONICAL_PERIOD_END = 11312

HOUSE_TYPE = "房地(土地+建物)"
HOUSE_PARKING_TYPE = "房地(土地+建物)+車位"
LAND_TYPE = "土地"
KEEP_TRANSACTION_TYPES = {HOUSE_TYPE, HOUSE_PARKING_TYPE, LAND_TYPE}
HOUSE_TRANSACTION_TYPES = {HOUSE_TYPE, HOUSE_PARKING_TYPE}

EXPECTED_BUILDING_TYPES = {
    "住宅大樓(11層含以上有電梯)",
    "其他",
    "透天厝",
    "華廈(10層含以下有電梯)",
    "公寓(5樓含以下無電梯)",
    "套房(1房1廳1衛)",
    "店面(店鋪)",
    "辦公商業大樓",
    "農舍",
    "廠辦",
    "工廠",
    "倉庫",
}
EXPECTED_URBAN_LAND_USE_TYPES = {"住", "商", "工", "農", "其他"}

# 正式 cleaning 只 materialize 會影響 output、排除規則或 audit 的 raw 欄位。
REQUIRED_RAW_COLUMNS = (
    "no",
    "trans_y",
    "trans_m",
    "trans_d",
    "county",
    "countycd",
    "town",
    "type",
    "trans_landsize",
    "usage_type",
    "nonurban_type",
    "building_type",
    "usage",
    "date_complete",
    "trans_size",
    "room",
    "hall",
    "bath",
    "manage",
    "housing_totprice",
    "h_price_m2",
    "parking_size",
    "parking_price",
    "note",
)
LOCATION_LOOKUP_COLUMNS = (
    "legacy_county_code",
    "legacy_town_code",
    "county_id",
    "town_id",
    "city",
    "district",
)
LOCATION_ALIAS_COLUMNS = (
    "legacy_county_code",
    "raw_town",
    "normalized_town",
)

PRIMARY_EXCLUSION_REASONS = (
    "missing_source_transaction_id",
    "duplicate_exact",
    "duplicate_conflict",
    "invalid_transaction_period",
    "unmapped_location",
    "excluded_transaction_type",
)

# Arrow schema 是 Parquet 發布契約；nullable 同時定義 NULL 是否合法。
CLEAN_SCHEMA = pa.schema(
    [
        pa.field("cleaning_run_id", pa.string(), nullable=False),
        pa.field("source_transaction_id", pa.string(), nullable=False),
        pa.field("transaction_year", pa.int16(), nullable=False),
        pa.field("transaction_month", pa.int16(), nullable=False),
        pa.field("transaction_date", pa.date32(), nullable=True),
        pa.field("transaction_date_valid", pa.bool_(), nullable=False),
        pa.field("completion_date", pa.date32(), nullable=True),
        pa.field("completion_date_status", pa.string(), nullable=False),
        pa.field("completion_after_transaction", pa.bool_(), nullable=True),
        pa.field("county_id", pa.string(), nullable=False),
        pa.field("town_id", pa.string(), nullable=False),
        pa.field("city", pa.string(), nullable=False),
        pa.field("district", pa.string(), nullable=False),
        pa.field("transaction_type", pa.string(), nullable=False),
        pa.field("urban_land_use_type", pa.string(), nullable=True),
        pa.field("nonurban_land_use_zone", pa.string(), nullable=True),
        pa.field("building_type", pa.string(), nullable=True),
        pa.field("primary_use", pa.string(), nullable=True),
        pa.field("has_management", pa.bool_(), nullable=True),
        pa.field("land_transfer_area_m2", pa.float64(), nullable=True),
        pa.field("building_transfer_area_m2", pa.float64(), nullable=True),
        pa.field("room_count", pa.int32(), nullable=True),
        pa.field("hall_count", pa.int32(), nullable=True),
        pa.field("bathroom_count", pa.int32(), nullable=True),
        pa.field("total_price_ntd", pa.int64(), nullable=True),
        pa.field("unit_price_ntd_m2", pa.float64(), nullable=True),
        pa.field("total_price_valid", pa.bool_(), nullable=False),
        pa.field("unit_price_valid", pa.bool_(), nullable=False),
        pa.field("land_transfer_area_valid", pa.bool_(), nullable=False),
        pa.field("building_transfer_area_valid", pa.bool_(), nullable=True),
        pa.field("parking_data_complete", pa.bool_(), nullable=True),
        pa.field("has_note", pa.bool_(), nullable=False),
    ]
)

EXCLUDED_SCHEMA = pa.schema(
    [
        pa.field("exclusion_record_id", pa.string(), nullable=False),
        pa.field("cleaning_run_id", pa.string(), nullable=False),
        pa.field("source_transaction_id", pa.string(), nullable=True),
        pa.field("primary_exclusion_reason", pa.string(), nullable=False),
    ]
)
