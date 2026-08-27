from collections import Counter
from pathlib import Path

import pandas as pd


# ============================================================
# 1. Project paths
# ============================================================
#
# 目前這支程式位於：
#
# dashboard/src/profiling/04_profile_selected_fields.py
#
# parents[2]：
#
# 04_profile_selected_fields.py
#        ↑ profiling
#        ↑ src
#        ↑ dashboard
#
# 因此 PROJECT_ROOT 會指向整個 dashboard project。

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

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# 2. Columns selected for profiling
# ============================================================
#
# 這次刻意只選目前 schema design 真正需要判斷的 columns。
#
# 不對 63 個 columns 全部 profiling，
# 避免為已決定 EXCLUDE 的欄位浪費 full-data scan。

COLUMNS = [
    "type",
    "trans_num",
    "story",
    "building_type",
    "usage",
]


# ============================================================
# 3. Chunk size
# ============================================================
#
# raw .dta 約 14 GB。
#
# 不把完整資料一次讀進 RAM。
#
# chunksize=100_000 表示：
#
# 一次最多 materialize 約 100,000 rows
# 到 pandas DataFrame。
#
# 但整支程式仍然會逐批掃完全部 4,380,208 rows。

CHUNK_SIZE = 100_000


# ============================================================
# 4. Output settings
# ============================================================
#
# 每個 column 最後輸出 frequency 最大的前 30 個 values。
#
# 如果某欄 unique values 本來不到 30，
# 就會全部輸出。

TOP_N = 30


# ============================================================
# 5. Counters
# ============================================================

total_rows = 0


# 每個 column 分別記錄：
#
# pandas_na
#     pandas 判斷為 NaN / missing 的數量
#
# blank_string
#     Stata string missing 很常被讀成 ""
#     而不是 NaN，因此必須另外計算
#
# value_counts
#     full-data frequency distribution

stats = {
    column: {
        "pandas_na": 0,
        "blank_string": 0,
        "value_counts": Counter(),
    }
    for column in COLUMNS
}


# ============================================================
# 6. Helper function
# ============================================================

def normalize_for_counting(value):
    """
    將 raw value 轉成適合 Counter 統計的 representation。

    注意：
    這只是 profiling 用，
    不是 canonical cleaning rule。
    """

    # pandas / NumPy missing
    if pd.isna(value):
        return "<PANDAS_NA>"

    # Stata string missing 常會變成空字串 ""
    #
    # strip() 也可以抓：
    #
    # ""
    # " "
    # "   "
    #
    # 但現在只是在 profiling，
    # 不會因此修改 raw data。
    if isinstance(value, str) and value.strip() == "":
        return "<BLANK_STRING>"

    # 統一轉成字串，方便 Counter 與 CSV 輸出。
    #
    # 例如：
    # 15     -> "15"
    # "十五層" -> "十五層"
    #
    # 原始 dtype 仍然沒有被修改，
    # 只是 frequency output 使用 string representation。
    return str(value)


# ============================================================
# 7. Read Stata file in chunks
# ============================================================
#
# pandas / Stata 相關設定：
#
#
# columns=COLUMNS
# ------------------------------------------------------------
#
# DataFrame 只建立這 5 個 columns。
#
# 主要目的：
# 降低 RAM 使用量。
#
# 注意：
# .dta 不是像 Parquet 那樣的 columnar format，
# 所以不能假定 disk I/O 也只剩原來的 5/63。
#
#
# chunksize=CHUNK_SIZE
# ------------------------------------------------------------
#
# 分批處理，避免完整 14GB .dta 同時存在 RAM。
#
#
# convert_categoricals=False
# ------------------------------------------------------------
#
# 不把 Stata value labels 自動轉成 pandas.Categorical。
#
# profiling 階段希望先看到 stored/raw representation。
#
# 此外 pandas 在 chunk / iterator 模式下，
# categorical categories 可能因 chunk 中出現的 values
# 而產生 dtype/category consistency 問題。
#
#
# convert_dates=False
# ------------------------------------------------------------
#
# 這 5 個 columns 沒有日期，
# 但仍明確指定 False，
# 讓 raw profiling script 的 Stata reader 設定保持一致。
#
#
# convert_missing=False
# ------------------------------------------------------------
#
# Stata numeric extended missing：
#
# .
# .a
# ...
# .z
#
# 會被 pandas 轉成 NaN。
#
# 所以這份 profiling 無法區分 .a 和 .b。
#
# 如果未來某個 selected field
# 真的利用 extended missing 表達不同 semantic meaning，
# 要另外用 convert_missing=True 做 targeted investigation。
#
#
# preserve_dtypes=True
# ------------------------------------------------------------
#
# 儘可能保留 Stata storage dtype。
#
# 但這個 dtype 不會直接拿來決定 PostgreSQL datatype。

with pd.read_stata(
    DTA_PATH,
    columns=COLUMNS,
    chunksize=CHUNK_SIZE,
    convert_categoricals=False,
    convert_dates=False,
    convert_missing=False,
    preserve_dtypes=True,
) as reader:

    for chunk_number, chunk in enumerate(
        reader,
        start=1,
    ):

        rows_in_chunk = len(chunk)
        total_rows += rows_in_chunk

        print(
            f"Processing chunk {chunk_number:,}"
            f" | chunk rows: {rows_in_chunk:,}"
            f" | total processed: {total_rows:,}"
        )

        # ----------------------------------------------------
        # Profile each selected column
        # ----------------------------------------------------

        for column in COLUMNS:

            series = chunk[column]


            # pandas missing
            stats[column]["pandas_na"] += int(
                series.isna().sum()
            )


            # ------------------------------------------------
            # Blank strings
            # ------------------------------------------------
            #
            # 這一段非常重要。
            #
            # Stata：
            #
            # numeric missing -> .
            # string missing  -> ""
            #
            # pandas：
            #
            # numeric . 通常會變 NaN
            # string "" 通常仍然只是 ""
            #
            # 所以：
            #
            # series.isna()
            #
            # 不一定能抓到 Stata string missing。

            if (
                pd.api.types.is_object_dtype(series)
                or pd.api.types.is_string_dtype(series)
            ):

                blank_mask = (
                    series
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .eq("")
                    & series.notna()
                )

                stats[column]["blank_string"] += int(
                    blank_mask.sum()
                )


            # ------------------------------------------------
            # Full-data frequency
            # ------------------------------------------------
            #
            # 不把 438 萬 values 全部存起來。
            #
            # 每個 chunk 先轉成 Counter，
            # 然後加到全資料 Counter。

            normalized = series.map(
                normalize_for_counting
            )

            stats[column]["value_counts"].update(
                normalized.tolist()
            )


# ============================================================
# 8. Build column-level summary
# ============================================================

summary_rows = []

for column in COLUMNS:

    pandas_na = stats[column]["pandas_na"]
    blank_string = stats[column]["blank_string"]

    # Counter keys 就是完整資料實際觀察到的
    # normalized distinct values。
    #
    # 這裡包含：
    #
    # <PANDAS_NA>
    # <BLANK_STRING>
    #
    # 所以 raw_unique_count 是 profiling-oriented count，
    # 不等於未來 canonical SQL COUNT(DISTINCT ...) 的定義。

    unique_count = len(
        stats[column]["value_counts"]
    )

    summary_rows.append(
        {
            "column_name": column,
            "total_rows": total_rows,
            "pandas_na_count": pandas_na,
            "blank_string_count": blank_string,
            "pandas_na_rate": (
                pandas_na / total_rows
            ),
            "blank_string_rate": (
                blank_string / total_rows
            ),
            "raw_unique_count": unique_count,
        }
    )


summary_df = pd.DataFrame(
    summary_rows
)


# ============================================================
# 9. Build frequency table
# ============================================================

frequency_rows = []

for column in COLUMNS:

    counter = stats[column]["value_counts"]

    # most_common(TOP_N)
    #
    # Counter 內建 method。
    #
    # 回傳：
    #
    # [
    #     (value1, count1),
    #     (value2, count2),
    #     ...
    # ]
    #
    # 並依 count 由大到小排序。

    for rank, (value, count) in enumerate(
        counter.most_common(TOP_N),
        start=1,
    ):

        frequency_rows.append(
            {
                "column_name": column,
                "rank": rank,
                "value": value,
                "row_count": count,
                "row_share": (
                    count / total_rows
                ),
            }
        )


frequency_df = pd.DataFrame(
    frequency_rows
)


# ============================================================
# 10. Save outputs
# ============================================================

SUMMARY_OUTPUT = (
    OUTPUT_DIR
    / "selected_fields_summary.csv"
)

FREQUENCY_OUTPUT = (
    OUTPUT_DIR
    / "selected_fields_top_values.csv"
)


summary_df.to_csv(
    SUMMARY_OUTPUT,
    index=False,
    encoding="utf-8-sig",
)

frequency_df.to_csv(
    FREQUENCY_OUTPUT,
    index=False,
    encoding="utf-8-sig",
)


# ============================================================
# 11. Print results
# ============================================================

print("\n")
print("=" * 70)
print("Selected fields profiling completed")
print("=" * 70)

print("\nColumn summary:")
print(
    summary_df.to_string(
        index=False
    )
)

print("\nTop values:")
print(
    frequency_df.to_string(
        index=False
    )
)

print("\nOutputs:")
print(SUMMARY_OUTPUT)
print(FREQUENCY_OUTPUT)