"""檢查 MOI 買賣資料 ZIP，並逐批轉成既有 24 欄原始欄位契約。"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
import io
from pathlib import Path, PurePosixPath
import re
import zipfile

import pandas as pd


MAIN_FILE_PATTERN = re.compile(r"^(?P<county>[a-z])_lvr_land_a\.csv$", re.I)

# MOI 檔名前綴與現有行政區參照使用相同代碼。
COUNTY_BY_FILE_CODE = {
    "A": "臺北市",
    "B": "臺中市",
    "C": "基隆市",
    "D": "臺南市",
    "E": "高雄市",
    "F": "新北市",
    "G": "宜蘭縣",
    "H": "桃園市",
    "I": "嘉義市",
    "J": "新竹縣",
    "K": "苗栗縣",
    "M": "南投縣",
    "N": "彰化縣",
    "O": "新竹市",
    "P": "雲林縣",
    "Q": "嘉義縣",
    "T": "屏東縣",
    "U": "花蓮縣",
    "V": "臺東縣",
    "W": "金門縣",
    "X": "澎湖縣",
    "Z": "連江縣",
}

# 第一版明確沿用 DTA 結果。這是暫時性相容規則，不是地址推論。
TEMPORARY_DTA_COMPATIBILITY_TOWNS = {
    "I": "東區",
    "O": "東區",
}

# 以下是 MOI 與行政區參照之間已確認的「台／臺」字形差異。
MOI_TOWN_ALIASES = {
    ("P", "台西鄉"): "臺西鄉",
    ("V", "台東市"): "臺東市",
}

PARKING_AREA_CANONICAL = "車位移轉總面積平方公尺"
PARKING_AREA_HEADERS = {
    PARKING_AREA_CANONICAL,
    "車位移轉總面積(平方公尺)",
}

MOI_REQUIRED_COLUMNS = {
    "鄉鎮市區",
    "交易標的",
    "土地移轉總面積平方公尺",
    "都市土地使用分區",
    "非都市土地使用分區",
    "交易年月日",
    "建物型態",
    "主要用途",
    "建築完成年月",
    "建物移轉總面積平方公尺",
    "建物現況格局-房",
    "建物現況格局-廳",
    "建物現況格局-衛",
    "有無管理組織",
    "總價元",
    "單價元平方公尺",
    PARKING_AREA_CANONICAL,
    "車位總價元",
    "備註",
    "編號",
}


def _required_raw_columns() -> tuple[str, ...]:
    """延後載入清理契約，避免轉接模組與清理模組循環匯入。"""

    from src.cleaning.contract import REQUIRED_RAW_COLUMNS

    return REQUIRED_RAW_COLUMNS


@dataclass(frozen=True)
class MoiZipInspection:
    """通過結構檢查的 MOI ZIP 清單與轉接契約。"""

    path: Path
    transaction_files: tuple[str, ...]
    normalized_columns: tuple[str, ...]
    temporary_location_rules: tuple[str, ...]


def _normalize_header(value: str) -> str:
    return value.removeprefix("\ufeff").strip()


def _canonicalize_headers(headers: list[str], member: str) -> dict[str, str]:
    normalized = [_normalize_header(header) for header in headers]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"MOI CSV contains duplicate headers: {member}")

    parking_headers = PARKING_AREA_HEADERS.intersection(normalized)
    if len(parking_headers) != 1:
        raise ValueError(
            f"MOI CSV must contain exactly one supported parking-area header: {member}"
        )

    rename = {next(iter(parking_headers)): PARKING_AREA_CANONICAL}
    canonical = {rename.get(header, header) for header in normalized}
    missing = sorted(MOI_REQUIRED_COLUMNS - canonical)
    if missing:
        raise ValueError(f"MOI CSV missing required columns in {member}: {missing}")
    return rename


def _main_files(archive: zipfile.ZipFile) -> tuple[tuple[str, str], ...]:
    selected: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    for member in archive.namelist():
        filename = PurePosixPath(member).name
        match = MAIN_FILE_PATTERN.fullmatch(filename)
        if not match:
            continue
        code = match.group("county").upper()
        if code not in COUNTY_BY_FILE_CODE:
            raise ValueError(f"unknown MOI county file code {code}: {member}")
        if code in seen_codes:
            raise ValueError(f"duplicate MOI main transaction file for county {code}")
        seen_codes.add(code)
        selected.append((code, member))

    if not selected:
        raise ValueError("MOI ZIP contains no *_lvr_land_a.csv transaction files")
    return tuple(sorted(selected))


def inspect_moi_zip(path: Path) -> MoiZipInspection:
    """只讀檔案清單與欄名；結構不符時在清理前停止。"""

    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"MOI ZIP not found: {path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"MOI source is not a valid ZIP: {path}")

    transaction_files: list[str] = []
    temporary_rules: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for code, member in _main_files(archive):
            with archive.open(member) as raw:
                wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                try:
                    headers = next(csv.reader(wrapper))
                except StopIteration as error:
                    raise ValueError(f"MOI CSV is empty: {member}") from error
            _canonicalize_headers(headers, member)
            transaction_files.append(member)
            if code in TEMPORARY_DTA_COMPATIBILITY_TOWNS:
                temporary_rules.append(
                    f"{code}:{COUNTY_BY_FILE_CODE[code]}:"
                    f"{TEMPORARY_DTA_COMPATIBILITY_TOWNS[code]}"
                )

    return MoiZipInspection(
        path=path,
        transaction_files=tuple(transaction_files),
        normalized_columns=_required_raw_columns(),
        temporary_location_rules=tuple(temporary_rules),
    )


def _normalize_text(values: pd.Series) -> pd.Series:
    return (
        values.astype("string")
        .str.strip()
        .str.normalize("NFKC")
        .replace("", pd.NA)
    )


def _urban_land_use(values: pd.Series) -> pd.Series:
    """將 MOI 詳細文字縮成清理契約接受的五種分類。"""

    source = _normalize_text(values)
    result = pd.Series(pd.NA, index=source.index, dtype="string")
    for marker, category in (
        ("住", "住"),
        ("商", "商"),
        ("工", "工"),
        ("農", "農"),
    ):
        result.loc[source.str.contains(marker, na=False)] = category
    result.loc[source.notna() & result.isna()] = "其他"
    return result


def _transaction_date_parts(values: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    normalized = _normalize_text(values).str.replace(r"\.0$", "", regex=True)
    valid = normalized.str.fullmatch(r"\d{6,7}", na=False)
    roc_year = pd.to_numeric(normalized.str[:-4].where(valid), errors="coerce")
    month = pd.to_numeric(normalized.str[-4:-2].where(valid), errors="coerce")
    day = pd.to_numeric(normalized.str[-2:].where(valid), errors="coerce")
    return roc_year, month, day


def _adapt_chunk(source: pd.DataFrame, county_code: str) -> pd.DataFrame:
    source = source.rename(
        columns={header: _normalize_header(header) for header in source.columns}
    )
    parking_header = next(
        header for header in PARKING_AREA_HEADERS if header in source.columns
    )
    source = source.rename(columns={parking_header: PARKING_AREA_CANONICAL})

    roc_year, month, day = _transaction_date_parts(source["交易年月日"])
    town = _normalize_text(source["鄉鎮市區"])
    if county_code in TEMPORARY_DTA_COMPATIBILITY_TOWNS:
        town = pd.Series(
            TEMPORARY_DTA_COMPATIBILITY_TOWNS[county_code],
            index=source.index,
            dtype="string",
        )
    else:
        for (alias_county, raw_town), normalized_town in MOI_TOWN_ALIASES.items():
            if county_code == alias_county:
                town = town.replace(raw_town, normalized_town)

    adapted = pd.DataFrame(
        {
            "no": source["編號"],
            "trans_y": roc_year,
            "trans_m": month,
            "trans_d": day,
            "county": COUNTY_BY_FILE_CODE[county_code],
            "countycd": county_code,
            "town": town,
            "type": source["交易標的"],
            "trans_landsize": source["土地移轉總面積平方公尺"],
            "usage_type": _urban_land_use(source["都市土地使用分區"]),
            "nonurban_type": source["非都市土地使用分區"],
            "building_type": source["建物型態"],
            "usage": source["主要用途"],
            "date_complete": source["建築完成年月"],
            "trans_size": source["建物移轉總面積平方公尺"],
            "room": source["建物現況格局-房"],
            "hall": source["建物現況格局-廳"],
            "bath": source["建物現況格局-衛"],
            "manage": source["有無管理組織"],
            "housing_totprice": source["總價元"],
            "h_price_m2": source["單價元平方公尺"],
            "parking_size": source[PARKING_AREA_CANONICAL],
            "parking_price": source["車位總價元"],
            "note": source["備註"],
        }
    )
    return adapted.loc[:, list(_required_raw_columns())]


def iter_moi_transaction_chunks(
    path: Path,
    *,
    chunk_size: int,
) -> Iterator[pd.DataFrame]:
    """逐縣市讀取買賣主檔並回傳 DTA 相容欄位，不展開附檔。"""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    inspection = inspect_moi_zip(path)

    with zipfile.ZipFile(inspection.path) as archive:
        for member in inspection.transaction_files:
            filename = PurePosixPath(member).name
            match = MAIN_FILE_PATTERN.fullmatch(filename)
            if match is None:
                raise ValueError(f"unexpected inspected MOI filename: {member}")
            county_code = match.group("county").upper()

            with archive.open(member) as raw:
                with pd.read_csv(
                    raw,
                    encoding="utf-8-sig",
                    dtype="string",
                    skiprows=[1],
                    chunksize=chunk_size,
                    keep_default_na=False,
                    on_bad_lines="error",
                ) as reader:
                    for chunk in reader:
                        yield _adapt_chunk(chunk, county_code)
