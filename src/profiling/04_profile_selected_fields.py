"""Summarize selected categorical fields used to define the cleaning contract."""

from collections import Counter
from pathlib import Path

import pandas as pd


# 1. 從程式位置取得專案根目錄。

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


# 2. 只剖析欄位設計需要判斷的欄位，略過已確定排除者。

COLUMNS = [
    "type",
    "trans_num",
    "story",
    "building_type",
    "usage",
]


# 分批限制記憶體用量，但仍會掃描完整 `.dta`。

CHUNK_SIZE = 100_000


# 每個欄位最多輸出 30 個最常見值。

TOP_N = 30


# 5. 統計容器

total_rows = 0


# Stata 字串缺失常表示為空字串，必須與 pandas 空值分開統計。

stats = {
    column: {
        "pandas_na": 0,
        "blank_string": 0,
        "value_counts": Counter(),
    }
    for column in COLUMNS
}


# 6. 輔助函式

def normalize_for_counting(value):
    """
    將 raw value 轉成適合 Counter 統計的 representation。

    注意：
    這只是 profiling 用，
    不是 canonical cleaning rule。
    """

    # pandas／NumPy 缺值
    if pd.isna(value):
        return "<PANDAS_NA>"

    # 空白字串是另一種缺失表示；資料剖析不修改原始值。
    if isinstance(value, str) and value.strip() == "":
        return "<BLANK_STRING>"

    # 統一計數鍵與 CSV 顯示格式，不修改原始欄位型別。
    return str(value)


# 只讀必要欄位並保留原始型別；不預先轉換標籤、日期或延伸缺失碼。

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

        # 逐欄累計缺值、不重複值與出現次數。

        for column in COLUMNS:

            series = chunk[column]


            # pandas 缺值
            stats[column]["pandas_na"] += int(
                series.isna().sum()
            )


            # `isna()` 不會捕捉 Stata 的空白字串缺失。

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


            # 逐批累加頻率，不保存全部原始值。

            normalized = series.map(
                normalize_for_counting
            )

            stats[column]["value_counts"].update(
                normalized.tolist()
            )


# 8. 建立欄位摘要

summary_rows = []

for column in COLUMNS:

    pandas_na = stats[column]["pandas_na"]
    blank_string = stats[column]["blank_string"]

    # 不重複值包含 <PANDAS_NA> 與 <BLANK_STRING>，因此 raw_unique_count
    # 是剖析用計數，不等同標準 SQL COUNT(DISTINCT ...) 定義。

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


# 9. 建立頻率表

frequency_rows = []

for column in COLUMNS:

    counter = stats[column]["value_counts"]

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


# 10. 儲存輸出

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


# 11. 顯示結果

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
