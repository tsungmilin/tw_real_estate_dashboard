"""Compare parking price and area adjustment formulas across completeness groups."""

from pathlib import Path

import numpy as np
import pandas as pd


# 1. 路徑

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


# 2. 欄位

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

# 官方單價為整數元／m²，允許四捨五入差異 ±1 元。
PRICE_ATOL = 1


# 3. 車位價格與面積組合計數
# A：價格、面積均大於 0；B：均為 0；C：價格為 0；D：面積為 0。
# OTHER 保留缺值、負值或新的異常組合，避免靜默忽略。

group_counts = {
    "A_both_positive": 0,
    "B_both_zero": 0,
    "C_size_only": 0,
    "D_price_only": 0,
    "OTHER": 0,
}


# 4. 公式驗證計數；每組測試一至兩種可能公式。
#
# comparable 表示資料足以計算；matched 表示與 h_price_m2 相差不超過 ±1 元。

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

    # 只比較實際值與預期值皆為有限數值的資料列。
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


# 5. 讀取 Stata 檔

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

        # 5.1 只保留房地＋車位交易

        chunk = chunk[
            chunk["type"] == HOUSE_PARKING_TYPE
        ].copy()

        total_parking_rows += len(chunk)

        print(
            f"Processing chunk {chunk_number:,}"
            f" | parking rows processed: "
            f"{total_parking_rows:,}"
        )


        # 5.2 數值轉換

        for column in NUMERIC_COLUMNS:

            chunk[column] = pd.to_numeric(
                chunk[column],
                errors="coerce",
            )


        # 6. 定義 A／B／C／D 組

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


        # other_mask 收納不屬於 A、B、C、D 的資料列。

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


        # 7. 各公式共用的有效值條件

        basic_valid = (
            chunk["housing_totprice"].notna()
            & chunk["trans_size"].notna()
            & chunk["h_price_m2"].notna()
            & (chunk["housing_totprice"] > 0)
            & (chunk["trans_size"] > 0)
            & (chunk["h_price_m2"] > 0)
        )


        # 8. A 組：價格、面積均大於 0，測試同時扣除兩者的公式。

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


        # 同一批 A 組資料也測試「完全不扣車位」作為比較基準。

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


        # 9. B 組：車位價格與面積均為 0，預期不扣除車位。

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


        # 10. C 組：有車位面積但無車位價格，同時測試兩種可能公式。


        # C1：完全不扣車位。

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


        # C2：只扣車位面積；這是診斷假設，不代表官方公式。

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


        # 11. D 組：有車位價格但無車位面積，同時測試兩種可能公式。


        # D1：完全不調整。

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


        # D2：只扣車位價格。

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


# 12. 分組摘要

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


# 13. 公式摘要

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


# 14. 儲存輸出

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


# 15. 顯示結果

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
