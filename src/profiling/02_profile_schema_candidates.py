from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# 1. Project paths
# ============================================================

# __file__ 是目前這支 .py 的位置：
# dashboard/src/profiling/02_validate_key_fields.py
#
# parents[2] 往上兩層就是 dashboard/
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


# ============================================================
# 2. Columns that we actually need
# ============================================================
#
# 這次不是完整 profiling。
# 我們只回答目前會影響 schema design 的三組問題：
#
# A. year / trans_y / trans_m / trans_d / date_trans 的關係
# B. no 是否有 missing，以及是否有明顯 duplicate 問題
# C. trans_landsize / landtotarea / landtransarea1~5 的關係
#
# 使用 columns= 可以避免把另外五十多個 columns
# 一起建立成 DataFrame，降低每個 chunk 的 RAM 使用量。

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


# ============================================================
# 3. Chunk size
# ============================================================
#
# 不可以直接把 14 GB .dta 全部讀進 RAM。
#
# 每次讀 100,000 rows。
# 因為這次只有 13 個 columns，100k 是一個合理起點。
#
# 注意：
# chunksize 只控制一次放多少資料進 RAM，
# 並不代表 pandas 不需要掃完整個 .dta。
CHUNK_SIZE = 100_000


# ============================================================
# 4. Counters
# ============================================================

total_rows = 0

# ----- no -----
no_missing = 0

# 這裡只能檢查「同一個 chunk 內」的 duplicates。
#
# 不能因此宣稱 no 在整份資料 globally unique，
# 因為：
#
# chunk 1 可能有 no = A
# chunk 2 也可能有 no = A
#
# 但兩個 chunk 各自都看不出 duplicate。
#
# 如果要做 exact global uniqueness，
# 後面應該用 PostgreSQL UNIQUE / GROUP BY，
# 或其他 disk-backed 方法，而不是把所有 no
# 塞進 Python set，避免大量 RAM 消耗。
no_duplicates_within_chunks = 0


# ----- date fields -----

year_min = None
year_max = None

trans_y_min = None
trans_y_max = None

invalid_month_rows = 0
invalid_day_rows = 0

year_trans_y_comparable = 0
year_trans_y_equal = 0


# ----- land fields -----

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


# ============================================================
# 5. Read .dta in chunks
# ============================================================
#
# Stata / pandas 有幾個重要讀取設定：
#
# convert_categoricals=False
# --------------------------------
# 不讓 pandas 自動把 Stata value labels 轉成 categorical。
#
# 原因：
# 1. 我們現在是在做 raw profiling。
# 2. pandas 官方也提醒，iterator 讀 categorical 時，
#    不同 chunk 可能得到不同 categories / dtype。
#
#
# convert_dates=False
# --------------------------------
# 不讓 pandas 自動把具有 Stata date format 的 numeric values
# 直接轉成 pandas datetime。
#
# 我們現在要先知道 raw data 是怎麼存的，
# 再決定 canonical cleaning rule。
#
#
# convert_missing=False
# --------------------------------
# Stata 有：
#
# .
# .a
# .b
# ...
# .z
#
# 這些 extended missing values。
#
# convert_missing=False 時，
# pandas 會把它們轉成 NaN。
#
# 優點：
# - RAM / datatype 比較容易控制
# - 後面的 numeric calculation 比較容易
#
# 缺點：
# - 我們會失去 . / .a / .b ... 之間的差異
#
# 如果後面發現某個欄位利用 extended missing
# 表示不同意義，我們再另外用 convert_missing=True
# 做針對性檢查。
#
#
# preserve_dtypes=True
# --------------------------------
# 儘量保留原本 Stata numeric datatype，
# 而不是全部升級成 int64 / float64。

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


        # ====================================================
        # A. Validate "no"
        # ====================================================

        no_missing += int(chunk["no"].isna().sum())

        # 注意：這只是 within-chunk duplicates。
        no_duplicates_within_chunks += int(
            chunk["no"].duplicated(keep=False).sum()
        )


        # ====================================================
        # B. Inspect date-related fields
        # ====================================================

        # ----- year -----

        current_year_min = chunk["year"].min(skipna=True)
        current_year_max = chunk["year"].max(skipna=True)

        if pd.notna(current_year_min):
            if year_min is None or current_year_min < year_min:
                year_min = current_year_min

        if pd.notna(current_year_max):
            if year_max is None or current_year_max > year_max:
                year_max = current_year_max


        # ----- trans_y -----

        current_trans_y_min = chunk["trans_y"].min(skipna=True)
        current_trans_y_max = chunk["trans_y"].max(skipna=True)

        if pd.notna(current_trans_y_min):
            if trans_y_min is None or current_trans_y_min < trans_y_min:
                trans_y_min = current_trans_y_min

        if pd.notna(current_trans_y_max):
            if trans_y_max is None or current_trans_y_max > trans_y_max:
                trans_y_max = current_trans_y_max


        # ----- month -----
        #
        # 現在只做最基本的 range validation。
        #
        # 1~12 合理。
        # missing 不算 invalid。

        invalid_month = (
            chunk["trans_m"].notna()
            & ~chunk["trans_m"].between(1, 12)
        )

        invalid_month_rows += int(invalid_month.sum())


        # ----- day -----
        #
        # 目前只檢查 1~31。
        #
        # 這還不能抓出：
        # 2/31
        # 4/31
        #
        # 因為我們現在還沒確認 year 的 calendar 定義，
        # 所以先不要擅自組成 datetime。

        invalid_day = (
            chunk["trans_d"].notna()
            & ~chunk["trans_d"].between(1, 31)
        )

        invalid_day_rows += int(invalid_day.sum())


        # ----- year vs trans_y -----
        #
        # 先檢查兩者在 raw data 中是否相等，
        # 不先假設哪一個才是真正 transaction year。

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


        # ====================================================
        # C. Land-area relationships
        # ====================================================

        # ----------------------------------------------------
        # trans_landsize vs landtotarea
        # ----------------------------------------------------

        comparable = (
            chunk["trans_landsize"].notna()
            & chunk["landtotarea"].notna()
        )

        land_pair_comparable += int(comparable.sum())

        # 浮點數不要直接用 ==
        #
        # 例如理論上都是 10.1，
        # binary floating point representation
        # 有時可能出現極小差異。
        #
        # 目前允許 0.01 平方公尺的 absolute tolerance。
        equal = np.isclose(
            chunk.loc[comparable, "trans_landsize"],
            chunk.loc[comparable, "landtotarea"],
            rtol=0,
            atol=0.01,
        )

        land_pair_equal += int(equal.sum())


        # ----------------------------------------------------
        # landtransarea1 ~ landtransarea5
        # ----------------------------------------------------

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


        # min_count=1：
        #
        # 如果 landtransarea1~5 全部 missing，
        # 結果保持 NaN。
        #
        # 不希望：
        # NaN + NaN + ... 被錯誤視為 0。
        detail_sum = chunk[detail_columns].sum(
            axis=1,
            min_count=1,
        )


        # ----------------------------------------------------
        # trans_landsize vs detail sum
        # ----------------------------------------------------

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


        # ----------------------------------------------------
        # landtotarea vs detail sum
        # ----------------------------------------------------

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


# ============================================================
# 6. Helper function
# ============================================================

def safe_rate(numerator, denominator):
    """
    denominator = 0 時避免 ZeroDivisionError。
    """
    if denominator == 0:
        return np.nan

    return numerator / denominator


# ============================================================
# 7. Build validation summary
# ============================================================

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


# ============================================================
# 8. Print results
# ============================================================

print("\n")
print("=" * 60)
print("Validation completed")
print("=" * 60)

print(result_df.to_string(index=False))

print("\nSaved to:")
print(OUTPUT_PATH)
