from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# 1. Project paths
# ============================================================
#
# 目前程式位於：
#
# dashboard/src/profiling/03_investigate_anomalies.py
#
# parents[2] 會回到 project root：
#
# dashboard/
#
# 注意：
# 這種寫法依賴 __file__，
# 所以應該使用 VS Code 的 "Run Python File" 執行，
# 不要使用 Notebook / Interactive Cell 執行。

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


# 如果資料夾不存在就建立。
#
# parents=True：
# 如果 profiling_output/ 尚不存在，也一起建立。
#
# exist_ok=True：
# 如果資料夾本來就存在，不會報錯，也不會刪除內容。

SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
PRIVATE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. Columns needed for this investigation
# ============================================================
#
# 這次只處理四個問題：
#
# A. year 的 distribution
#
# B. 找出 trans_d 不在 1~31 的 rows
#
# C. landtotarea 與 landtransarea1~5 加總的 mismatch 方向
#
# D. mismatch 是否集中在 landtransarea5 有值的 rows
#
# 沒有必要讀取完整 63 columns。
#
# pandas 的 columns= 可以減少建立在 DataFrame 中的欄位，
# 因此可以顯著降低每個 chunk 的 RAM 使用量。
#
# 但是要注意：
# .dta 的實際 disk I/O / parsing 成本不一定會按照
# column 數量同比例下降。
#
# 所以：
#
# columns= 主要保證的是：
# 「不要把不需要的欄位全部 materialize 到 RAM」
#
# 不能理解成：
# 「只會從 14GB 檔案讀這幾個欄位的 bytes」。

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


# ============================================================
# 3. Chunk size
# ============================================================
#
# 這份 .dta 約 14 GB，
# 不應該一次完整載入 pandas DataFrame。
#
# 每次處理 100,000 rows。
#
# chunksize 限制的是：
# 「一次放進 RAM 的 row 數」
#
# 不是：
# 「只檢查前 100,000 rows」。
#
# 這支程式仍然會完整掃過 4,380,208 rows。

CHUNK_SIZE = 100_000


# ============================================================
# 4. Floating-point comparison tolerance
# ============================================================
#
# 土地面積是 floating-point numeric data。
#
# 不應該直接使用：
#
#     landtotarea == detail_sum
#
# 因為 binary floating-point representation
# 有可能出現非常小的 rounding difference。
#
# 目前設定：
#
# ±0.01 平方公尺視為相等。
#
# 這只是我們 profiling 階段的 technical tolerance，
# 不是內政部定義的 data quality rule。

AREA_ATOL = 0.01


# ============================================================
# 5. Counters
# ============================================================

total_rows = 0


# ------------------------------------------------------------
# year distribution
# ------------------------------------------------------------

year_counts = Counter()

year_missing = 0

# 因為 project coverage 是 2012~2024，
# 如果 year 真的是民國交易年份，
# 合理範圍應該大約是 101~113。
#
# 但目前尚未確認 year 的真正 semantic definition，
# 所以這裡只是 profiling classification，
# 不是 cleaning rule。

year_lt_101 = 0
year_101_to_113 = 0
year_gt_113 = 0


# ------------------------------------------------------------
# invalid day
# ------------------------------------------------------------

invalid_day_count = 0

# 用來判斷 invalid_day_rows.csv 是否已經開始寫入。
#
# 我們採逐 chunk append，
# 而不是把所有異常 rows 暫存在 RAM。

invalid_output_written = False


# ------------------------------------------------------------
# land mismatch
# ------------------------------------------------------------
#
# 分成兩群：
#
# landtransarea5 missing
# landtransarea5 present
#
# 這樣可以直接測試：
#
# mismatch 是否集中在「至少已經填到第 5 筆土地」的交易。
#
# 如果 land5_present 的 mismatch 特別高，
# 而且主要方向是：
#
# landtotarea > detail_sum
#
# 那會支持：
#
# 「原始土地可能超過 5 筆，但 wide structure 只保留前 5 筆」
#
# 這個 hypothesis。
#
# 注意：
# 即使結果符合，也只能說支持 hypothesis，
# 不能直接證明資料確實被截斷。

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


# ============================================================
# 6. Read Stata file
# ============================================================
#
# pandas / Stata 讀取設定非常重要。
#
#
# convert_categoricals=False
# ------------------------------------------------------------
#
# Stata value labels 可能會被 pandas 自動轉成
# pandas.Categorical。
#
# profiling 階段我們要看原始 stored values，
# 因此關閉。
#
# pandas 官方也提醒：
# chunk / iterator 模式下 categorical 的 category set
# 可能受到各 chunk 實際出現值影響。
#
#
# convert_dates=False
# ------------------------------------------------------------
#
# pandas 預設可能依 Stata date format
# 自動轉成 datetime。
#
# 目前我們還在研究：
#
# year
# trans_y
# trans_m
# trans_d
# date_trans
#
# 的原始關係，所以不要先讓 pandas 幫我們做日期 interpretation。
#
# 注意：
# convert_dates=False 並不代表 date_trans 一定變成 numeric。
#
# 如果它在 Stata 本身就是 string，
# 那 pandas 還是會讀成 string/object。
#
#
# convert_missing=False
# ------------------------------------------------------------
#
# Stata 有：
#
# .
# .a
# .b
# ...
# .z
#
# extended missing values。
#
# False 時 pandas 會將它們表示成 NaN。
#
# 優點：
# numeric operation 比較容易。
#
# 缺點：
# 無法區別 .a / .b / .c 等不同 missing code。
#
# 所以如果之後發現某一欄位需要區分
# Stata extended missing semantics，
# 必須另外針對該欄位重新檢查。
#
#
# preserve_dtypes=True
# ------------------------------------------------------------
#
# 儘量保留 Stata storage datatype，
# 例如 int16 / float32。
#
# 但：
#
# Stata storage dtype != canonical schema dtype
# Stata storage dtype != PostgreSQL dtype
#
# 所以我們現在只把 dtype 當 raw metadata，
# 不用它直接決定 database schema。
#
#
# context manager
# ------------------------------------------------------------
#
# pandas 官方建議 StataReader 使用：
#
#     with ... as reader:
#
# 而不是手動：
#
#     reader.close()
#
# 我們前一版看到的 FutureWarning
# 就是因為 close() 並不是正式 public API。

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

        # 目前 chunk 在完整 .dta 中的起始位置。
        #
        # Stata observation number 習慣從 1 開始，
        # 所以我們建立一個 1-based source_row_number。
        #
        # 注意：
        # 這只是依 pandas 讀取順序建立的 observation position，
        # 不是 transaction_id。

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


        # ====================================================
        # A. year distribution
        # ====================================================

        year_missing += int(
            chunk["year"].isna().sum()
        )

        # value_counts() 只對目前 chunk 計算。
        #
        # 再透過 Counter 累積成 full-data distribution。
        #
        # 這樣不需要把 438 萬個 year values
        # 全部存進 Python list。

        chunk_year_counts = (
            chunk["year"]
            .dropna()
            .value_counts()
        )

        for value, count in chunk_year_counts.items():

            year_counts[value] += int(count)


        # 下面只是依 project coverage 做 descriptive grouping。
        #
        # 現在不能因此直接刪掉 <101 或 >113 rows。

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


        # ====================================================
        # B. trans_d outside 1~31
        # ====================================================
        #
        # 這裡刻意沿用上一支 script 的定義：
        #
        # trans_d < 1
        # OR
        # trans_d > 31
        #
        # 所以理論上應該重新找到約 130 rows。
        #
        # 注意：
        # 這還不是完整 calendar validation。
        #
        # 它抓不到：
        #
        # 2/30
        # 2/31
        # 4/31
        # 6/31
        #
        # 這些要等日期欄位 semantic definition
        # 更清楚之後再做。

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

            # 加上原始 observation position，
            # 方便之後回頭定位 raw .dta。
            #
            # 它不是 primary key。

            invalid_rows.insert(
                0,
                "source_row_number",
                source_row_numbers[
                    invalid_day_mask.to_numpy()
                ],
            )


            # mode="a"
            # ----------------------------
            # append 到同一個 CSV。
            #
            # header 只在第一次寫入時產生。
            #
            # 因此即使異常 row 分散在很多 chunks，
            # 也不用全部先留在 RAM。

            invalid_rows.to_csv(
                INVALID_DAY_OUTPUT,
                mode="a",
                header=not invalid_output_written,
                index=False,
                encoding="utf-8",
            )

            invalid_output_written = True


        # ====================================================
        # C. landtotarea vs detail sum
        # ====================================================

        detail_columns = [
            "landtransarea1",
            "landtransarea2",
            "landtransarea3",
            "landtransarea4",
            "landtransarea5",
        ]


        # min_count=1 很重要。
        #
        # 如果五個 detail columns 全部 missing：
        #
        # 我們希望：
        #
        # detail_sum = NaN
        #
        # 而不是：
        #
        # detail_sum = 0
        #
        # 因為「沒有資料」與「土地面積為 0」
        # 是完全不同的概念。

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


        # delta > 0:
        #
        # landtotarea > detail sum
        #
        # delta < 0:
        #
        # landtotarea < detail sum

        delta = (
            chunk["landtotarea"]
            - detail_sum
        )


        # 建立與 chunk index 相同的 boolean Series。
        #
        # 預設 False，
        # 只對 comparable rows 計算 np.isclose。

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


        # 已經落在 tolerance 內的 rows
        # 不再分類成 greater / less。

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


        # ====================================================
        # D. Compare land5 missing vs present
        # ====================================================

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


# ============================================================
# 7. Helper
# ============================================================

def safe_rate(numerator, denominator):
    """
    denominator = 0 時回傳 NaN，
    避免 ZeroDivisionError。
    """

    if denominator == 0:
        return np.nan

    return numerator / denominator


# ============================================================
# 8. Export year distribution
# ============================================================

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


# ============================================================
# 9. Export land mismatch comparison
# ============================================================

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


# ============================================================
# 10. Export overall anomaly summary
# ============================================================

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


# ============================================================
# 11. Handle zero-invalid-row case
# ============================================================
#
# 如果完全沒有 invalid rows，
# 前面的 append code 就不會建立 CSV。
#
# 為了讓 pipeline output predictable，
# 我們仍然產生一個只有 header 的空 CSV。

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


# ============================================================
# 12. Print final results
# ============================================================

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