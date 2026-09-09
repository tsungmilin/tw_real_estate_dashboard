"""逐批套用排除順序、標準欄位轉換與品質標記。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .audit import AuditAccumulator
from .contract import (
    CANONICAL_PERIOD_END,
    CANONICAL_PERIOD_START,
    EXPECTED_BUILDING_TYPES,
    EXPECTED_URBAN_LAND_USE_TYPES,
    HOUSE_PARKING_TYPE,
    HOUSE_TYPE,
    KEEP_TRANSACTION_TYPES,
    LAND_TYPE,
)


@dataclass
class ChunkResult:
    clean: pd.DataFrame
    excluded: pd.DataFrame


def normalize_text(series: pd.Series) -> pd.Series:
    """去除前後空白、統一全形／半形字元，並將空字串轉為空值。"""

    return (
        series.astype("string")
        .str.strip()
        .str.normalize("NFKC")
        .replace("", pd.NA)
    )


def _transaction_dates(
    roc_year: pd.Series,
    month: pd.Series,
    day: pd.Series,
) -> pd.Series:
    # 年月 eligibility 已另外判斷；這裡只負責組完整日期，無效 day 會變成 NaT。
    gregorian_year = pd.Series(roc_year + 1911, dtype="float64")
    numeric_month = pd.Series(month, dtype="float64")
    numeric_day = pd.Series(day, dtype="float64")
    return pd.to_datetime(
        {
            "year": gregorian_year,
            "month": numeric_month,
            "day": numeric_day,
        },
        errors="coerce",
    )


def _completion_dates(
    raw_values: pd.Series,
    land_mask: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    # completion_status 保留無效原因；只有有效的 6／7 位民國日期才產生日期。
    normalized = normalize_text(raw_values)
    numeric = pd.to_numeric(normalized, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    integer_like = finite & np.isclose(numeric, numeric.round(), rtol=0, atol=1e-9)

    integers = pd.Series(pd.NA, index=raw_values.index, dtype="Int64")
    integers.loc[integer_like] = numeric.loc[integer_like].round().astype("int64")
    strings = integers.astype("string")
    lengths = strings.str.len()

    status = pd.Series("invalid", index=raw_values.index, dtype="string")
    status.loc[normalized.isna()] = "missing"
    status.loc[integer_like & numeric.gt(0) & lengths.eq(5)] = "ambiguous_5_digit"

    candidate = integer_like & numeric.gt(0) & lengths.isin([6, 7])
    roc_year = pd.to_numeric(strings.str[:-4], errors="coerce")
    month = pd.to_numeric(strings.str[-4:-2], errors="coerce")
    day = pd.to_numeric(strings.str[-2:], errors="coerce")
    parsed = pd.to_datetime(
        {
            "year": pd.Series(roc_year + 1911, dtype="float64"),
            "month": pd.Series(month, dtype="float64"),
            "day": pd.Series(day, dtype="float64"),
        },
        errors="coerce",
    )
    valid = candidate & parsed.notna()
    status.loc[valid] = "valid"
    parsed = parsed.where(valid)

    status.loc[land_mask] = "not_applicable"
    parsed.loc[land_mask] = pd.NaT
    return parsed, status


def _positive_float(
    values: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    # 面積與單價保留所有正值；0、負值與 parse failure 轉為 NULL。
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    output = numeric.where(valid).astype("Float64")
    return output, valid, numeric


def _positive_integer(
    values: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    # 總價輸出是 Int64，因此正值還必須能無損解析為整數。
    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    integer_like = finite & np.isclose(numeric, numeric.round(), rtol=0, atol=1e-9)
    non_integer_positive = finite & numeric.gt(0) & ~integer_like
    valid = integer_like & numeric.gt(0)
    output = numeric.round().where(valid).astype("Int64")
    return output, valid, numeric, non_integer_positive


def _nonnegative_integer(
    values: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    # 格局的 0 有實際語意；只有負值、fraction 或 parse failure 轉為 NULL。
    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.notna() & np.isfinite(numeric)
    integer_like = finite & np.isclose(numeric, numeric.round(), rtol=0, atol=1e-9)
    valid = integer_like & numeric.ge(0)
    output = numeric.round().where(valid).astype("Int32")
    parse_failure = normalize_text(values).notna() & ~valid
    return output, valid, parse_failure


def _duplicate_reasons(
    source_ids: pd.Series,
    duplicate_kinds: dict[str, str],
    exact_seen: set[str],
) -> pd.Series:
    # 衝突群組全部排除；完全相同的群組只保留整次執行最早出現的一筆。
    # exact_seen 跨 chunks 共用，因此不會漏掉跨 chunk duplicate。
    reasons = pd.Series(pd.NA, index=source_ids.index, dtype="string")
    if not duplicate_kinds:
        return reasons

    conflict_ids = {
        source_id
        for source_id, kind in duplicate_kinds.items()
        if kind == "duplicate_conflict"
    }
    exact_ids = set(duplicate_kinds) - conflict_ids
    reasons.loc[source_ids.isin(conflict_ids)] = "duplicate_conflict"

    for index, source_id in source_ids.loc[source_ids.isin(exact_ids)].items():
        if source_id in exact_seen:
            reasons.loc[index] = "duplicate_exact"
        else:
            exact_seen.add(str(source_id))
    return reasons


def _assign_reason(reasons: pd.Series, mask: pd.Series, value: str) -> None:
    # 只填尚未有 reason 的 rows，呼叫順序就是 primary exclusion precedence。
    reasons.loc[reasons.isna() & mask.fillna(False)] = value


def _has_land_building_value(values: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce")
        return numeric.notna() & numeric.ne(0)
    return normalize_text(values).notna()


def process_chunk(
    raw_chunk: pd.DataFrame,
    *,
    run_id: str,
    global_start: int,
    lookup: pd.DataFrame,
    alias_map: dict[tuple[str, str], str],
    duplicate_kinds: dict[str, str],
    exact_seen: set[str],
    audit: AuditAccumulator,
) -> ChunkResult:
    """處理一批資料，回傳可直接寫入清理後／排除 Parquet 的兩個資料框。"""

    raw = raw_chunk.reset_index(drop=True)
    index = raw.index
    audit.input_rows += len(raw)

    # 步驟 1：先標準化篩選與連接鍵，不覆寫原始欄位。
    source_id = normalize_text(raw["no"])
    county_code = normalize_text(raw["countycd"])
    raw_county = normalize_text(raw["county"])
    raw_town = normalize_text(raw["town"])
    transaction_type = normalize_text(raw["type"])

    duplicate_reason = _duplicate_reasons(source_id, duplicate_kinds, exact_seen)
    roc_year = pd.to_numeric(raw["trans_y"], errors="coerce")
    month = pd.to_numeric(raw["trans_m"], errors="coerce")
    period = roc_year * 100 + month
    valid_period = (
        roc_year.notna()
        & month.between(1, 12)
        & period.between(CANONICAL_PERIOD_START, CANONICAL_PERIOD_END)
    )

    # 步驟 2：別名只修正已確認的舊名，再以縣市代碼與鄉鎮市區對應標準地點。
    normalized_town = raw_town.copy()
    for (alias_county, alias_town), target in alias_map.items():
        alias_mask = county_code.eq(alias_county) & raw_town.eq(alias_town)
        normalized_town.loc[alias_mask] = target
        audit.independent_checks[f"location_alias:{alias_county}:{alias_town}"] += int(
            alias_mask.sum()
        )

    location_keys = pd.DataFrame(
        {
            "legacy_county_code": county_code,
            "district": normalized_town,
        }
    )
    mapped = location_keys.merge(
        lookup,
        on=["legacy_county_code", "district"],
        how="left",
        sort=False,
        validate="many_to_one",
    )
    if len(mapped) != len(raw):
        raise ValueError("location mapping changed the chunk row count")
    unmapped = mapped["town_id"].isna()

    # 步驟 3：依契約順序指派唯一主要排除原因。
    reasons = pd.Series(pd.NA, index=index, dtype="string")
    _assign_reason(reasons, source_id.isna(), "missing_source_transaction_id")
    _assign_reason(reasons, duplicate_reason.eq("duplicate_exact"), "duplicate_exact")
    _assign_reason(
        reasons,
        duplicate_reason.eq("duplicate_conflict"),
        "duplicate_conflict",
    )
    _assign_reason(reasons, ~valid_period, "invalid_transaction_period")
    _assign_reason(reasons, unmapped, "unmapped_location")
    _assign_reason(
        reasons,
        ~transaction_type.isin(KEEP_TRANSACTION_TYPES),
        "excluded_transaction_type",
    )

    audit.independent_checks["missing_source_transaction_id"] += int(
        source_id.isna().sum()
    )
    audit.independent_checks["duplicate_exact"] += int(
        duplicate_reason.eq("duplicate_exact").sum()
    )
    audit.independent_checks["duplicate_conflict"] += int(
        duplicate_reason.eq("duplicate_conflict").sum()
    )
    audit.independent_checks["invalid_transaction_period"] += int(
        (~valid_period).sum()
    )
    audit.independent_checks["unmapped_location_all_rows"] += int(unmapped.sum())
    audit.independent_checks["excluded_transaction_type_all_rows"] += int(
        (~transaction_type.isin(KEEP_TRANSACTION_TYPES)).sum()
    )
    primary_unmapped = reasons.eq("unmapped_location")
    audit.update_unmatched(county_code, raw_town, primary_unmapped)
    audit.update_primary(reasons)

    # 步驟 4：排除資料只保存追蹤與歸因需要的最小欄位。
    excluded_mask = reasons.notna()
    clean_mask = ~excluded_mask
    audit.excluded_rows += int(excluded_mask.sum())
    audit.clean_rows += int(clean_mask.sum())

    raw_positions = global_start + np.arange(len(raw), dtype=np.int64) + 1
    excluded_positions = raw_positions[excluded_mask.to_numpy()]
    excluded = pd.DataFrame(
        {
            "exclusion_record_id": [
                f"{run_id}-E{int(position):09d}" for position in excluded_positions
            ],
            "cleaning_run_id": run_id,
            "source_transaction_id": source_id.loc[excluded_mask].reset_index(drop=True),
            "primary_exclusion_reason": reasons.loc[excluded_mask].reset_index(drop=True),
        }
    )

    if not bool(clean_mask.any()):
        return ChunkResult(clean=pd.DataFrame(), excluded=excluded)

    # 步驟 5：只有符合納入條件的資料列才執行完整標準化。
    clean_raw = raw.loc[clean_mask].reset_index(drop=True)
    clean_location = mapped.loc[clean_mask].reset_index(drop=True)
    clean_source_id = source_id.loc[clean_mask].reset_index(drop=True)
    clean_type = transaction_type.loc[clean_mask].reset_index(drop=True)
    clean_roc_year = roc_year.loc[clean_mask].reset_index(drop=True)
    clean_month = month.loc[clean_mask].reset_index(drop=True)
    clean_day = pd.to_numeric(
        clean_raw["trans_d"],
        errors="coerce",
    )

    # 日期無效不一定排除：有效年月仍可支援 monthly analysis。
    transaction_date = _transaction_dates(clean_roc_year, clean_month, clean_day)
    transaction_date_valid = transaction_date.notna()
    audit.independent_checks["invalid_transaction_day_clean_rows"] += int(
        (~transaction_date_valid).sum()
    )

    land_mask = clean_type.eq(LAND_TYPE)
    completion_date, completion_status = _completion_dates(
        clean_raw["date_complete"],
        land_mask,
    )
    audit.completion_status.update(completion_status.value_counts().to_dict())
    completion_after = pd.Series(pd.NA, index=clean_raw.index, dtype="boolean")
    comparable_dates = completion_date.notna() & transaction_date.notna()
    completion_after.loc[comparable_dates] = (
        completion_date.loc[comparable_dates]
        > transaction_date.loc[comparable_dates]
    )
    audit.independent_checks["completion_after_transaction"] += int(
        completion_after.eq(True).sum()
    )

    # 文字欄位統一 trim/NFKC；土地不適用的建物欄位稍後強制設為 NULL。
    urban_use = normalize_text(clean_raw["usage_type"])
    nonurban_zone = normalize_text(clean_raw["nonurban_type"])
    building_type = normalize_text(clean_raw["building_type"])
    primary_use = normalize_text(clean_raw["usage"]).replace(
        "見其它登記事項",
        "見其他登記事項",
    )
    manage = normalize_text(clean_raw["manage"])

    has_management = pd.Series(pd.NA, index=clean_raw.index, dtype="boolean")
    has_management.loc[manage.eq("有")] = True
    has_management.loc[manage.eq("無")] = False
    unexpected_manage = ~land_mask & manage.notna() & ~manage.isin(["有", "無"])
    audit.independent_checks["unexpected_management_value"] += int(
        unexpected_manage.sum()
    )
    audit.independent_checks["unexpected_building_type"] += int(
        (
            ~land_mask
            & building_type.notna()
            & ~building_type.isin(EXPECTED_BUILDING_TYPES)
        ).sum()
    )
    audit.independent_checks["unexpected_urban_land_use_type"] += int(
        (
            urban_use.notna()
            & ~urban_use.isin(EXPECTED_URBAN_LAND_USE_TYPES)
        ).sum()
    )

    for field in [
        "building_type",
        "usage",
        "date_complete",
        "trans_size",
        "room",
        "hall",
        "bath",
        "manage",
    ]:
        audit.independent_checks[f"building_value_on_land:{field}"] += int(
            (land_mask & _has_land_building_value(clean_raw[field])).sum()
        )

    building_type.loc[land_mask] = pd.NA
    primary_use.loc[land_mask] = pd.NA
    has_management.loc[land_mask] = pd.NA

    # 數值輸出與 validity flags 同時計算，確保 flag 與 NULL state 一致。
    land_area, land_area_valid, land_area_numeric = _positive_float(
        clean_raw["trans_landsize"]
    )
    building_area, building_area_positive, building_area_numeric = _positive_float(
        clean_raw["trans_size"]
    )
    building_area.loc[land_mask] = pd.NA
    building_area_valid = pd.Series(
        building_area_positive,
        index=clean_raw.index,
        dtype="boolean",
    )
    building_area_valid.loc[land_mask] = pd.NA

    total_price, total_price_valid, total_price_numeric, total_price_non_integer = (
        _positive_integer(clean_raw["housing_totprice"])
    )
    unit_price, unit_price_valid, unit_price_numeric = _positive_float(
        clean_raw["h_price_m2"]
    )
    unit_price_non_integer = (
        unit_price_valid
        & ~np.isclose(
            unit_price_numeric,
            unit_price_numeric.round(),
            rtol=0,
            atol=1e-9,
        )
    )

    room, _, room_fail = _nonnegative_integer(clean_raw["room"])
    hall, _, hall_fail = _nonnegative_integer(clean_raw["hall"])
    bath, _, bath_fail = _nonnegative_integer(clean_raw["bath"])
    for values in [room, hall, bath]:
        values.loc[land_mask] = pd.NA
    for name, failure in {
        "room_count": room_fail,
        "hall_count": hall_fail,
        "bathroom_count": bath_fail,
    }.items():
        audit.independent_checks[f"layout_parse_failure:{name}"] += int(
            (failure & ~land_mask).sum()
        )

    # 車位旗標只表示輔助欄位是否完整，不重算或覆寫官方單價。
    parking_price = pd.to_numeric(clean_raw["parking_price"], errors="coerce")
    parking_size = pd.to_numeric(clean_raw["parking_size"], errors="coerce")
    parking_mask = clean_type.eq(HOUSE_PARKING_TYPE)
    parking_complete = pd.Series(pd.NA, index=clean_raw.index, dtype="boolean")
    parking_complete.loc[parking_mask] = (
        parking_price.loc[parking_mask].gt(0)
        & parking_size.loc[parking_mask].gt(0)
    )
    audit.independent_checks["parking_applicable_rows"] += int(parking_mask.sum())
    audit.independent_checks["parking_data_complete"] += int(
        parking_complete.eq(True).sum()
    )
    audit.independent_checks["parking_data_incomplete"] += int(
        parking_complete.eq(False).sum()
    )

    note = normalize_text(clean_raw["note"])
    has_note = note.notna()
    audit.independent_checks["has_note"] += int(has_note.sum())

    canonical_county = clean_location["city"].astype("string")
    clean_raw_county = raw_county.loc[clean_mask].reset_index(drop=True)
    audit.independent_checks["raw_county_name_differs_from_canonical"] += int(
        (clean_raw_county.notna() & clean_raw_county.ne(canonical_county)).sum()
    )

    audit.update_numeric(
        "land_transfer_area_m2",
        land_area_numeric,
        land_area_valid,
    )
    audit.update_numeric(
        "building_transfer_area_m2",
        building_area_numeric.loc[~land_mask],
        building_area_positive.loc[~land_mask],
    )
    audit.update_numeric(
        "total_price_ntd",
        total_price_numeric,
        total_price_valid,
        non_integer=total_price_non_integer,
    )
    audit.update_numeric(
        "unit_price_ntd_m2",
        unit_price_numeric,
        unit_price_valid,
        non_integer=unit_price_non_integer,
    )
    audit.update_layout("room_count", room.loc[~land_mask])
    audit.update_layout("hall_count", hall.loc[~land_mask])
    audit.update_layout("bathroom_count", bath.loc[~land_mask])

    audit.update_category("transaction_type", clean_type)
    audit.update_category("building_type", building_type)
    audit.update_category("primary_use", primary_use)
    audit.update_category("manage", manage.where(~land_mask))
    audit.update_category("urban_land_use_type", urban_use)
    audit.update_category("nonurban_land_use_zone", nonurban_zone)

    # 單價公式只做 aggregate cross-check，不新增 row-level mismatch flag 或排除資料。
    house_formula_mask = (
        clean_type.eq(HOUSE_TYPE)
        & total_price_numeric.gt(0)
        & building_area_numeric.gt(0)
        & unit_price_numeric.gt(0)
    )
    if bool(house_formula_mask.any()):
        expected = (
            total_price_numeric.loc[house_formula_mask]
            / building_area_numeric.loc[house_formula_mask]
        )
        actual = unit_price_numeric.loc[house_formula_mask]
        matched = np.isclose(actual, expected, rtol=0, atol=1)
        audit.update_formula(
            "house_unit_price",
            len(actual),
            int(matched.sum()),
        )

    parking_formula_mask = (
        parking_mask
        & total_price_numeric.gt(parking_price)
        & building_area_numeric.gt(parking_size)
        & unit_price_numeric.gt(0)
    )
    if bool(parking_formula_mask.any()):
        expected = (
            total_price_numeric.loc[parking_formula_mask]
            - parking_price.loc[parking_formula_mask]
        ) / (
            building_area_numeric.loc[parking_formula_mask]
            - parking_size.loc[parking_formula_mask]
        )
        actual = unit_price_numeric.loc[parking_formula_mask]
        matched = np.isclose(actual, expected, rtol=0, atol=1)
        audit.update_formula(
            "parking_adjusted_unit_price",
            len(actual),
            int(matched.sum()),
        )

    # 欄位順序與 dtype 最後仍會由 Arrow CLEAN_SCHEMA 再驗證一次。
    clean = pd.DataFrame(
        {
            "cleaning_run_id": run_id,
            "source_transaction_id": clean_source_id,
            "transaction_year": (clean_roc_year + 1911).astype("Int16"),
            "transaction_month": clean_month.astype("Int16"),
            "transaction_date": transaction_date,
            "transaction_date_valid": transaction_date_valid.astype("boolean"),
            "completion_date": completion_date,
            "completion_date_status": completion_status,
            "completion_after_transaction": completion_after,
            "county_id": clean_location["county_id"].astype("string"),
            "town_id": clean_location["town_id"].astype("string"),
            "city": canonical_county,
            "district": clean_location["district"].astype("string"),
            "transaction_type": clean_type,
            "urban_land_use_type": urban_use,
            "nonurban_land_use_zone": nonurban_zone,
            "building_type": building_type,
            "primary_use": primary_use,
            "has_management": has_management,
            "land_transfer_area_m2": land_area,
            "building_transfer_area_m2": building_area,
            "room_count": room,
            "hall_count": hall,
            "bathroom_count": bath,
            "total_price_ntd": total_price,
            "unit_price_ntd_m2": unit_price,
            "total_price_valid": pd.Series(total_price_valid, dtype="boolean"),
            "unit_price_valid": pd.Series(unit_price_valid, dtype="boolean"),
            "land_transfer_area_valid": pd.Series(land_area_valid, dtype="boolean"),
            "building_transfer_area_valid": building_area_valid,
            "parking_data_complete": parking_complete,
            "has_note": pd.Series(has_note, dtype="boolean"),
        }
    )
    return ChunkResult(clean=clean, excluded=excluded)
