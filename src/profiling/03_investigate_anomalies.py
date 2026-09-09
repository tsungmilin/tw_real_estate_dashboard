"""Measure date and land-area anomalies, separating summaries from row-level output."""

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


# 1. 專案路徑；從程式位置解析，不依賴工作目錄。

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DTA_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "house_preowned_data_2.0.dta"
)

SUMMARY_DIR = PROJECT_ROOT / "profiling_output" / "summary"
PRIVATE_DIR = PROJECT_ROOT / "profiling_output" / "private"

YEAR_OUTPUT = SUMMARY_DIR / "year_distribution.csv"

SUMMARY_OUTPUT = (
    SUMMARY_DIR
    / "anomaly_summary.csv"
)

LAND_OUTPUT = (
    SUMMARY_DIR
    / "land_mismatch_by_land5.csv"
)

INVALID_DAY_OUTPUT = (
    PRIVATE_DIR
    / "invalid_day_rows.csv"
)


SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
PRIVATE_DIR.mkdir(parents=True, exist_ok=True)


# 只載入日期與土地面積欄位以限制記憶體；`.dta` 仍可能需要完整掃描。

DATE_COLUMNS = [
    "year",
    "trans_y",
    "trans_m",
    "trans_d",
    "date_trans",
]

LAND_COLUMNS = [
    "landtotarea",
    "landtransarea1",
    "landtransarea2",
    "landtransarea3",
    "landtransarea4",
    "landtransarea5",
]

COLUMNS = [
    "no",
    *DATE_COLUMNS,
    *LAND_COLUMNS,
]


# 3. 每批處理 100,000 筆以限制記憶體，但仍掃描完整資料。

CHUNK_SIZE = 100_000


# 4. 土地面積浮點比較允許 ±0.01 平方公尺；這是剖析容差，不是官方品質規則。

AREA_ATOL = 0.01


# 5. 統計容器

total_rows = 0


# year 分布

year_counts = Counter()

year_missing = 0

# 若 year 是民國交易年，專案期間應約為 101–113；此處只做剖析分類，
# 不作為清理規則。

year_lt_101 = 0
year_101_to_113 = 0
year_gt_113 = 0


# 無效日期

invalid_day_count = 0

# 異常資料逐批附加至 CSV，不全數留在記憶體。

invalid_output_written = False


# 依 landtransarea5 是否有值分組，檢查面積差異是否集中於至少填到
# 第 5 筆土地的交易。結果只能支持「寬表可能截斷更多筆土地」的假設，不能證明。

land_stats = {
    "land5_missing": {
        "comparable": 0,
        "equal": 0,
        "landtot_gt_detail_sum": 0,
        "landtot_lt_detail_sum": 0,
    },
    "land5_present": {
        "comparable": 0,
        "equal": 0,
        "landtot_gt_detail_sum": 0,
        "landtot_lt_detail_sum": 0,
    },
}


# 保留原始儲存型別，不預先轉換標籤與日期；延伸缺失值統一視為 NaN。

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

        rows_in_chunk = len(chunk)

        # 依 Stata 慣例建立從 1 起算的來源列號；它不是交易 ID。

        source_row_numbers = np.arange(
            total_rows + 1,
            total_rows + rows_in_chunk + 1,
        )

        total_rows += rows_in_chunk

        print(
            f"Processing chunk {chunk_number:,}"
            f" | chunk rows: {rows_in_chunk:,}"
            f" | total processed: {total_rows:,}"
        )


        # A. year 分布

        year_missing += int(
            chunk["year"].isna().sum()
        )

        # 逐批計數後累加，不保存 438 萬個 year 值。

        chunk_year_counts = (
            chunk["year"]
            .dropna()
            .value_counts()
        )

        for value, count in chunk_year_counts.items():

            year_counts[value] += int(count)


        # 只依專案期間描述性分組，不據此刪除範圍外資料。

        year_valid = chunk["year"].notna()

        year_lt_101 += int(
            (
                year_valid
                & (chunk["year"] < 101)
            ).sum()
        )

        year_101_to_113 += int(
            (
                year_valid
                & chunk["year"].between(101, 113)
            ).sum()
        )

        year_gt_113 += int(
            (
                year_valid
                & (chunk["year"] > 113)
            ).sum()
        )


        # B. trans_d 超出 1–31；這不是完整曆法驗證，無法辨識 2/30 等日期。

        invalid_day_mask = (
            chunk["trans_d"].notna()
            & ~chunk["trans_d"].between(1, 31)
        )

        invalid_day_count += int(
            invalid_day_mask.sum()
        )


        if invalid_day_mask.any():

            invalid_rows = chunk.loc[
                invalid_day_mask,
                [
                    "no",
                    "year",
                    "trans_y",
                    "trans_m",
                    "trans_d",
                    "date_trans",
                ],
            ].copy()

            # 加上來源列號以定位原始 `.dta`；它不是主鍵。

            invalid_rows.insert(
                0,
                "source_row_number",
                source_row_numbers[
                    invalid_day_mask.to_numpy()
                ],
            )


            # 分批附加寫入，避免將所有異常列留在記憶體。

            invalid_rows.to_csv(
                INVALID_DAY_OUTPUT,
                mode="a",
                header=not invalid_output_written,
                index=False,
                encoding="utf-8",
            )

            invalid_output_written = True


        # C. 比較 landtotarea 與明細合計

        detail_columns = [
            "landtransarea1",
            "landtransarea2",
            "landtransarea3",
            "landtransarea4",
            "landtransarea5",
        ]


        # 全部明細缺失時維持 NaN，避免把「沒有資料」誤判為面積 0。

        detail_sum = chunk[
            detail_columns
        ].sum(
            axis=1,
            min_count=1,
        )


        # 只有兩邊都有資料才進行比較。

        comparable = (
            chunk["landtotarea"].notna()
            & detail_sum.notna()
        )


        # 正值表示總面積較大，負值表示明細合計較大。

        delta = (
            chunk["landtotarea"]
            - detail_sum
        )


        # 只對兩邊都有值的資料列計算浮點近似比較。

        equal_mask = pd.Series(
            False,
            index=chunk.index,
        )

        equal_mask.loc[comparable] = np.isclose(
            chunk.loc[
                comparable,
                "landtotarea",
            ],
            detail_sum.loc[comparable],
            rtol=0,
            atol=AREA_ATOL,
        )


        # 容差內的資料列不再分類為總面積較大或明細較大。

        greater_mask = (
            comparable
            & ~equal_mask
            & (delta > 0)
        )

        less_mask = (
            comparable
            & ~equal_mask
            & (delta < 0)
        )


        # D. 比較 landtransarea5 缺值與有值兩組

        land5_present = (
            chunk["landtransarea5"].notna()
        )


        group_masks = {
            "land5_missing": ~land5_present,
            "land5_present": land5_present,
        }


        for group_name, group_mask in group_masks.items():

            group_comparable = (
                comparable
                & group_mask
            )

            land_stats[
                group_name
            ]["comparable"] += int(
                group_comparable.sum()
            )

            land_stats[
                group_name
            ]["equal"] += int(
                (
                    equal_mask
                    & group_mask
                ).sum()
            )

            land_stats[
                group_name
            ]["landtot_gt_detail_sum"] += int(
                (
                    greater_mask
                    & group_mask
                ).sum()
            )

            land_stats[
                group_name
            ]["landtot_lt_detail_sum"] += int(
                (
                    less_mask
                    & group_mask
                ).sum()
            )


# 7. 輔助函式

def safe_rate(numerator, denominator):
    """
    denominator = 0 時回傳 NaN，
    避免 ZeroDivisionError。
    """

    if denominator == 0:
        return np.nan

    return numerator / denominator


# 8. 匯出 year 分布

year_distribution = pd.DataFrame(
    [
        {
            "year_raw": year_value,
            "row_count": row_count,
            "row_share": safe_rate(
                row_count,
                total_rows,
            ),
        }
        for year_value, row_count
        in sorted(year_counts.items())
    ]
)

year_distribution.to_csv(
    YEAR_OUTPUT,
    index=False,
    encoding="utf-8-sig",
)


# 9. 匯出土地面積差異比較

land_results = []

for group_name, stats in land_stats.items():

    comparable_count = stats["comparable"]

    mismatch_count = (
        stats["landtot_gt_detail_sum"]
        + stats["landtot_lt_detail_sum"]
    )

    land_results.append(
        {
            "group": group_name,

            "comparable_rows":
                comparable_count,

            "equal_rows":
                stats["equal"],

            "landtot_gt_detail_sum_rows":
                stats["landtot_gt_detail_sum"],

            "landtot_lt_detail_sum_rows":
                stats["landtot_lt_detail_sum"],

            "mismatch_rows":
                mismatch_count,

            "equal_rate":
                safe_rate(
                    stats["equal"],
                    comparable_count,
                ),

            "mismatch_rate":
                safe_rate(
                    mismatch_count,
                    comparable_count,
                ),

            "landtot_gt_detail_sum_rate":
                safe_rate(
                    stats["landtot_gt_detail_sum"],
                    comparable_count,
                ),

            "landtot_lt_detail_sum_rate":
                safe_rate(
                    stats["landtot_lt_detail_sum"],
                    comparable_count,
                ),
        }
    )


land_result_df = pd.DataFrame(
    land_results
)

land_result_df.to_csv(
    LAND_OUTPUT,
    index=False,
    encoding="utf-8-sig",
)


# 10. 匯出整體異常摘要

summary = pd.DataFrame(
    [
        {
            "metric": "total_rows",
            "value": total_rows,
        },
        {
            "metric": "year_missing",
            "value": year_missing,
        },
        {
            "metric": "year_raw_lt_101",
            "value": year_lt_101,
        },
        {
            "metric": "year_raw_101_to_113",
            "value": year_101_to_113,
        },
        {
            "metric": "year_raw_gt_113",
            "value": year_gt_113,
        },
        {
            "metric": "invalid_day_outside_1_31",
            "value": invalid_day_count,
        },
    ]
)

summary.to_csv(
    SUMMARY_OUTPUT,
    index=False,
    encoding="utf-8-sig",
)


# 11. 沒有異常資料時仍建立只有標題列的 CSV，使輸出結構固定。

if not invalid_output_written:

    pd.DataFrame(
        columns=[
            "source_row_number",
            "no",
            "year",
            "trans_y",
            "trans_m",
            "trans_d",
            "date_trans",
        ]
    ).to_csv(
        INVALID_DAY_OUTPUT,
        index=False,
        encoding="utf-8",
    )


# 12. 顯示結果

print("\n")
print("=" * 70)
print("Anomaly investigation completed")
print("=" * 70)

print("\nSummary:")
print(
    summary.to_string(
        index=False
    )
)

print("\nYear distribution:")
print(
    year_distribution.to_string(
        index=False
    )
)

print("\nLand mismatch by land5 status:")
print(
    land_result_df.to_string(
        index=False
    )
)

print("\nOutputs:")
print(YEAR_OUTPUT)
print(SUMMARY_OUTPUT)
print(LAND_OUTPUT)
print(INVALID_DAY_OUTPUT)
