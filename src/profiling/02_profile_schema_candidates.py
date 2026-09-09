"""Profile candidate key, date, and land-area fields into a public summary."""

from pathlib import Path

import numpy as np
import pandas as pd


# 專案路徑

# 從程式位置解析專案根目錄，不依賴執行時的工作目錄。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DTA_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "house_preowned_data_2.0.dta"
)

OUTPUT_DIR = (
    PROJECT_ROOT
    / "profiling_output"
    / "summary"
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_PATH = OUTPUT_DIR / "key_fields_validation.csv"


# 只讀取日期、來源 ID 與土地面積欄位，降低每批記憶體用量。

DATE_COLUMNS = [
    "year",
    "trans_y",
    "trans_m",
    "trans_d",
    "date_trans",
]

ID_COLUMNS = [
    "no",
]

LAND_COLUMNS = [
    "trans_landsize",
    "landtotarea",
    "landtransarea1",
    "landtransarea2",
    "landtransarea3",
    "landtransarea4",
    "landtransarea5",
]

COLUMNS = DATE_COLUMNS + ID_COLUMNS + LAND_COLUMNS


# 分批限制記憶體用量，但仍會掃描完整 `.dta`。
CHUNK_SIZE = 100_000


# 彙總計數

total_rows = 0

# 來源 ID
no_missing = 0

# 這裡只檢查批內重複；全域唯一性由磁碟型流程另行驗證。
no_duplicates_within_chunks = 0


# 日期欄位

year_min = None
year_max = None

trans_y_min = None
trans_y_max = None

invalid_month_rows = 0
invalid_day_rows = 0

year_trans_y_comparable = 0
year_trans_y_equal = 0


# 土地欄位

land_pair_comparable = 0
land_pair_equal = 0

trans_vs_detail_comparable = 0
trans_vs_detail_equal = 0

landtot_vs_detail_comparable = 0
landtot_vs_detail_equal = 0

land_detail_non_null = {
    f"landtransarea{i}": 0
    for i in range(1, 6)
}


# 保留原始儲存型別，不預先轉換標籤與日期；Stata 延伸缺失值統一視為 NaN。

with pd.read_stata(
    DTA_PATH,
    columns=COLUMNS,
    chunksize=CHUNK_SIZE,
    convert_categoricals=False,
    convert_dates=False,
    convert_missing=False,
    preserve_dtypes=True,
) as reader:

    for chunk_number, chunk in enumerate(reader, start=1):

        rows = len(chunk)
        total_rows += rows

        print(
            f"Processing chunk {chunk_number:,} "
            f"| rows in chunk: {rows:,} "
            f"| total rows processed: {total_rows:,}"
        )


        # A. 驗證來源 ID

        no_missing += int(chunk["no"].isna().sum())

        # 此處只檢查批次內重複；跨批次重複另由全域檢查處理。
        no_duplicates_within_chunks += int(
            chunk["no"].duplicated(keep=False).sum()
        )


        # B. 檢查日期相關欄位

        # year

        current_year_min = chunk["year"].min(skipna=True)
        current_year_max = chunk["year"].max(skipna=True)

        if pd.notna(current_year_min):
            if year_min is None or current_year_min < year_min:
                year_min = current_year_min

        if pd.notna(current_year_max):
            if year_max is None or current_year_max > year_max:
                year_max = current_year_max


        # trans_y

        current_trans_y_min = chunk["trans_y"].min(skipna=True)
        current_trans_y_max = chunk["trans_y"].max(skipna=True)

        if pd.notna(current_trans_y_min):
            if trans_y_min is None or current_trans_y_min < trans_y_min:
                trans_y_min = current_trans_y_min

        if pd.notna(current_trans_y_max):
            if trans_y_max is None or current_trans_y_max > trans_y_max:
                trans_y_max = current_trans_y_max


        # month：只檢查 1–12；缺值另計，不視為超出範圍。

        invalid_month = (
            chunk["trans_m"].notna()
            & ~chunk["trans_m"].between(1, 12)
        )

        invalid_month_rows += int(invalid_month.sum())


        # 本剖析步驟只檢查 day 是否介於 1–31；完整曆法日期由後續驗證處理。

        invalid_day = (
            chunk["trans_d"].notna()
            & ~chunk["trans_d"].between(1, 31)
        )

        invalid_day_rows += int(invalid_day.sum())


        # 比較 year 與 trans_y，不預設哪個才是交易年。

        comparable = (
            chunk["year"].notna()
            & chunk["trans_y"].notna()
        )

        year_trans_y_comparable += int(comparable.sum())

        year_trans_y_equal += int(
            (
                chunk.loc[comparable, "year"]
                == chunk.loc[comparable, "trans_y"]
            ).sum()
        )


        # C. 土地面積關係

        # trans_landsize vs landtotarea

        comparable = (
            chunk["trans_landsize"].notna()
            & chunk["landtotarea"].notna()
        )

        land_pair_comparable += int(comparable.sum())

        # 浮點比較允許 0.01 平方公尺誤差。
        equal = np.isclose(
            chunk.loc[comparable, "trans_landsize"],
            chunk.loc[comparable, "landtotarea"],
            rtol=0,
            atol=0.01,
        )

        land_pair_equal += int(equal.sum())


        # landtransarea1 ~ landtransarea5

        detail_columns = [
            "landtransarea1",
            "landtransarea2",
            "landtransarea3",
            "landtransarea4",
            "landtransarea5",
        ]

        for column in detail_columns:
            land_detail_non_null[column] += int(
                chunk[column].notna().sum()
            )


        # 明細全缺時維持 NaN，避免誤判為面積 0。
        detail_sum = chunk[detail_columns].sum(
            axis=1,
            min_count=1,
        )


        # trans_landsize vs detail sum

        comparable = (
            chunk["trans_landsize"].notna()
            & detail_sum.notna()
        )

        trans_vs_detail_comparable += int(comparable.sum())

        equal = np.isclose(
            chunk.loc[comparable, "trans_landsize"],
            detail_sum.loc[comparable],
            rtol=0,
            atol=0.01,
        )

        trans_vs_detail_equal += int(equal.sum())


        # landtotarea vs detail sum

        comparable = (
            chunk["landtotarea"].notna()
            & detail_sum.notna()
        )

        landtot_vs_detail_comparable += int(comparable.sum())

        equal = np.isclose(
            chunk.loc[comparable, "landtotarea"],
            detail_sum.loc[comparable],
            rtol=0,
            atol=0.01,
        )

        landtot_vs_detail_equal += int(equal.sum())


# 6. 輔助函式

def safe_rate(numerator, denominator):
    """
    denominator = 0 時避免 ZeroDivisionError。
    """
    if denominator == 0:
        return np.nan

    return numerator / denominator


# 7. 建立驗證摘要

results = [
    {
        "metric": "total_rows",
        "value": total_rows,
    },
    {
        "metric": "no_missing",
        "value": no_missing,
    },
    {
        "metric": "no_duplicates_within_chunks",
        "value": no_duplicates_within_chunks,
    },
    {
        "metric": "year_min",
        "value": year_min,
    },
    {
        "metric": "year_max",
        "value": year_max,
    },
    {
        "metric": "trans_y_min",
        "value": trans_y_min,
    },
    {
        "metric": "trans_y_max",
        "value": trans_y_max,
    },
    {
        "metric": "invalid_month_rows",
        "value": invalid_month_rows,
    },
    {
        "metric": "invalid_day_rows",
        "value": invalid_day_rows,
    },
    {
        "metric": "year_trans_y_equal_rate",
        "value": safe_rate(
            year_trans_y_equal,
            year_trans_y_comparable,
        ),
    },
    {
        "metric": "trans_landsize_vs_landtotarea_equal_rate",
        "value": safe_rate(
            land_pair_equal,
            land_pair_comparable,
        ),
    },
    {
        "metric": "trans_landsize_vs_land_detail_sum_equal_rate",
        "value": safe_rate(
            trans_vs_detail_equal,
            trans_vs_detail_comparable,
        ),
    },
    {
        "metric": "landtotarea_vs_land_detail_sum_equal_rate",
        "value": safe_rate(
            landtot_vs_detail_equal,
            landtot_vs_detail_comparable,
        ),
    },
]

for column, count in land_detail_non_null.items():
    results.append(
        {
            "metric": f"{column}_non_null",
            "value": count,
        }
    )


result_df = pd.DataFrame(results)

result_df.to_csv(
    OUTPUT_PATH,
    index=False,
    encoding="utf-8-sig",
)


# 8. 顯示結果

print("\n")
print("=" * 60)
print("Validation completed")
print("=" * 60)

print(result_df.to_string(index=False))

print("\nSaved to:")
print(OUTPUT_PATH)
