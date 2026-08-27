from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# 1. Paths
# ============================================================

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
# 2. Columns
# ============================================================

COLUMNS = [
    "type",
    "housing_totprice",
    "h_price_m2",
    "trans_size",
    "parking_size",
    "parking_price",
]

NUMERIC_COLUMNS = [
    "housing_totprice",
    "h_price_m2",
    "trans_size",
    "parking_size",
    "parking_price",
]

CHUNK_SIZE = 100_000

HOUSE_PARKING_TYPE = "房地(土地+建物)+車位"

# 官方單價為整數元 / m²，允許 rounding 差異 ±1 元。
PRICE_ATOL = 1


# ============================================================
# 3. Group counters
# ============================================================
#
# A: parking_price > 0 and parking_size > 0
# B: parking_price = 0 and parking_size = 0
# C: parking_price = 0 and parking_size > 0
# D: parking_price > 0 and parking_size = 0
#
# OTHER:
# 若未來出現 missing / negative 等異常情況，
# 不會被偷偷忽略。

group_counts = {
    "A_both_positive": 0,
    "B_both_zero": 0,
    "C_size_only": 0,
    "D_price_only": 0,
    "OTHER": 0,
}


# ============================================================
# 4. Formula validation counters
# ============================================================
#
# 每種 group 會測試一到兩種可能公式。
#
# comparable:
# 有足夠資料、可以實際計算公式的 rows
#
# matched:
# 計算結果與 h_price_m2 在 ±1 元內一致的 rows

formula_stats = {}


def add_formula_result(
    group_name,
    formula_name,
    actual,
    expected,
):
    """
    累積某個 group + formula 的 validation 結果。
    """

    key = (group_name, formula_name)

    if key not in formula_stats:
        formula_stats[key] = {
            "comparable": 0,
            "matched": 0,
        }

    # 只保留 actual / expected 都是有限 numeric value 的 rows。
    valid_mask = (
        actual.notna()
        & expected.notna()
        & np.isfinite(actual)
        & np.isfinite(expected)
    )

    actual_valid = actual[valid_mask]
    expected_valid = expected[valid_mask]

    matches = np.isclose(
        actual_valid,
        expected_valid,
        rtol=0,
        atol=PRICE_ATOL,
    )

    formula_stats[key]["comparable"] += len(
        actual_valid
    )

    formula_stats[key]["matched"] += int(
        matches.sum()
    )


# ============================================================
# 5. Read Stata file
# ============================================================

total_parking_rows = 0

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

        # ----------------------------------------------------
        # 5.1 Only 房地 + 車位
        # ----------------------------------------------------

        chunk = chunk[
            chunk["type"] == HOUSE_PARKING_TYPE
        ].copy()

        total_parking_rows += len(chunk)

        print(
            f"Processing chunk {chunk_number:,}"
            f" | parking rows processed: "
            f"{total_parking_rows:,}"
        )


        # ----------------------------------------------------
        # 5.2 Numeric conversion
        # ----------------------------------------------------

        for column in NUMERIC_COLUMNS:

            chunk[column] = pd.to_numeric(
                chunk[column],
                errors="coerce",
            )


        # ====================================================
        # 6. Define A / B / C / D
        # ====================================================

        price_positive = (
            chunk["parking_price"] > 0
        )

        price_zero = (
            chunk["parking_price"] == 0
        )

        size_positive = (
            chunk["parking_size"] > 0
        )

        size_zero = (
            chunk["parking_size"] == 0
        )


        group_a = (
            price_positive
            & size_positive
        )

        group_b = (
            price_zero
            & size_zero
        )

        group_c = (
            price_zero
            & size_positive
        )

        group_d = (
            price_positive
            & size_zero
        )


        # "|" 對 pandas Boolean Series 表示 OR。
        #
        # "~" 表示 NOT。
        #
        # 所以 other_mask 意思是：
        #
        # 不屬於 A、B、C、D 的 rows。

        known_groups = (
            group_a
            | group_b
            | group_c
            | group_d
        )

        other_mask = ~known_groups


        group_counts["A_both_positive"] += int(
            group_a.sum()
        )

        group_counts["B_both_zero"] += int(
            group_b.sum()
        )

        group_counts["C_size_only"] += int(
            group_c.sum()
        )

        group_counts["D_price_only"] += int(
            group_d.sum()
        )

        group_counts["OTHER"] += int(
            other_mask.sum()
        )


        # ====================================================
        # 7. Common valid values
        # ====================================================

        basic_valid = (
            chunk["housing_totprice"].notna()
            & chunk["trans_size"].notna()
            & chunk["h_price_m2"].notna()
            & (chunk["housing_totprice"] > 0)
            & (chunk["trans_size"] > 0)
            & (chunk["h_price_m2"] > 0)
        )


        # ====================================================
        # 8. Group A
        # parking_price > 0
        # parking_size  > 0
        # ====================================================
        #
        # Formula A1:
        #
        # 扣掉車位價格與面積
        #
        # (total - parking price)
        # -----------------------
        # (area - parking area)

        a_adjusted_mask = (
            group_a
            & basic_valid
            & (
                chunk["housing_totprice"]
                > chunk["parking_price"]
            )
            & (
                chunk["trans_size"]
                > chunk["parking_size"]
            )
        )

        actual = chunk.loc[
            a_adjusted_mask,
            "h_price_m2",
        ]

        expected = (
            (
                chunk.loc[
                    a_adjusted_mask,
                    "housing_totprice",
                ]
                -
                chunk.loc[
                    a_adjusted_mask,
                    "parking_price",
                ]
            )
            /
            (
                chunk.loc[
                    a_adjusted_mask,
                    "trans_size",
                ]
                -
                chunk.loc[
                    a_adjusted_mask,
                    "parking_size",
                ]
            )
        )

        add_formula_result(
            "A_both_positive",
            "adjust_price_and_area",
            actual,
            expected,
        )


        # ----------------------------------------------------
        # 同一批 A rows 也測試「完全不扣車位」。
        #
        # 這是 comparison benchmark。
        # ----------------------------------------------------

        a_unadjusted_mask = (
            group_a
            & basic_valid
        )

        actual = chunk.loc[
            a_unadjusted_mask,
            "h_price_m2",
        ]

        expected = (
            chunk.loc[
                a_unadjusted_mask,
                "housing_totprice",
            ]
            /
            chunk.loc[
                a_unadjusted_mask,
                "trans_size",
            ]
        )

        add_formula_result(
            "A_both_positive",
            "unadjusted",
            actual,
            expected,
        )


        # ====================================================
        # 9. Group B
        # parking_price = 0
        # parking_size  = 0
        # ====================================================
        #
        # 預期：
        #
        # total_price / total_area

        b_mask = (
            group_b
            & basic_valid
        )

        actual = chunk.loc[
            b_mask,
            "h_price_m2",
        ]

        expected = (
            chunk.loc[
                b_mask,
                "housing_totprice",
            ]
            /
            chunk.loc[
                b_mask,
                "trans_size",
            ]
        )

        add_formula_result(
            "B_both_zero",
            "unadjusted",
            actual,
            expected,
        )


        # ====================================================
        # 10. Group C
        # parking_price = 0
        # parking_size  > 0
        # ====================================================
        #
        # 有車位面積，但沒有車位價格。
        #
        # 我們不知道 raw data 實際採哪種邏輯，
        # 所以同時測兩種。


        # C1:
        # 完全不扣車位

        c_unadjusted_mask = (
            group_c
            & basic_valid
        )

        actual = chunk.loc[
            c_unadjusted_mask,
            "h_price_m2",
        ]

        expected = (
            chunk.loc[
                c_unadjusted_mask,
                "housing_totprice",
            ]
            /
            chunk.loc[
                c_unadjusted_mask,
                "trans_size",
            ]
        )

        add_formula_result(
            "C_size_only",
            "unadjusted",
            actual,
            expected,
        )


        # C2:
        # 只扣車位面積
        #
        # 這主要是 diagnostic，
        # 不是我們預設的正確官方公式。

        c_area_adjusted_mask = (
            group_c
            & basic_valid
            & (
                chunk["trans_size"]
                > chunk["parking_size"]
            )
        )

        actual = chunk.loc[
            c_area_adjusted_mask,
            "h_price_m2",
        ]

        expected = (
            chunk.loc[
                c_area_adjusted_mask,
                "housing_totprice",
            ]
            /
            (
                chunk.loc[
                    c_area_adjusted_mask,
                    "trans_size",
                ]
                -
                chunk.loc[
                    c_area_adjusted_mask,
                    "parking_size",
                ]
            )
        )

        add_formula_result(
            "C_size_only",
            "adjust_area_only",
            actual,
            expected,
        )


        # ====================================================
        # 11. Group D
        # parking_price > 0
        # parking_size  = 0
        # ====================================================
        #
        # 同樣測兩種可能。


        # D1:
        # 完全不調整

        d_unadjusted_mask = (
            group_d
            & basic_valid
        )

        actual = chunk.loc[
            d_unadjusted_mask,
            "h_price_m2",
        ]

        expected = (
            chunk.loc[
                d_unadjusted_mask,
                "housing_totprice",
            ]
            /
            chunk.loc[
                d_unadjusted_mask,
                "trans_size",
            ]
        )

        add_formula_result(
            "D_price_only",
            "unadjusted",
            actual,
            expected,
        )


        # D2:
        # 只扣車位價格

        d_price_adjusted_mask = (
            group_d
            & basic_valid
            & (
                chunk["housing_totprice"]
                > chunk["parking_price"]
            )
        )

        actual = chunk.loc[
            d_price_adjusted_mask,
            "h_price_m2",
        ]

        expected = (
            (
                chunk.loc[
                    d_price_adjusted_mask,
                    "housing_totprice",
                ]
                -
                chunk.loc[
                    d_price_adjusted_mask,
                    "parking_price",
                ]
            )
            /
            chunk.loc[
                d_price_adjusted_mask,
                "trans_size",
            ]
        )

        add_formula_result(
            "D_price_only",
            "adjust_price_only",
            actual,
            expected,
        )


# ============================================================
# 12. Group summary
# ============================================================

group_rows = []

for group_name, count in group_counts.items():

    group_rows.append(
        {
            "group": group_name,
            "row_count": count,
            "row_share": (
                count / total_parking_rows
                if total_parking_rows
                else np.nan
            ),
        }
    )

group_df = pd.DataFrame(group_rows)


# ============================================================
# 13. Formula summary
# ============================================================

formula_rows = []

for (
    group_name,
    formula_name,
), result in formula_stats.items():

    comparable = result["comparable"]
    matched = result["matched"]

    formula_rows.append(
        {
            "group": group_name,
            "formula": formula_name,
            "comparable_rows": comparable,
            "matched_rows": matched,
            "match_rate": (
                matched / comparable
                if comparable
                else np.nan
            ),
        }
    )

formula_df = pd.DataFrame(
    formula_rows
)


# ============================================================
# 14. Save
# ============================================================

group_output = (
    OUTPUT_DIR
    / "parking_groups.csv"
)

formula_output = (
    OUTPUT_DIR
    / "parking_formula_validation_detailed.csv"
)

group_df.to_csv(
    group_output,
    index=False,
    encoding="utf-8-sig",
)

formula_df.to_csv(
    formula_output,
    index=False,
    encoding="utf-8-sig",
)


# ============================================================
# 15. Print
# ============================================================

print("\n")
print("=" * 70)
print("Parking group distribution")
print("=" * 70)

print(
    group_df.to_string(
        index=False
    )
)

print("\n")
print("=" * 70)
print("Parking formula validation")
print("=" * 70)

print(
    formula_df.to_string(
        index=False
    )
)

print("\nOutputs:")
print(group_output)
print(formula_output)