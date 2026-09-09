"""Profile price and area validity, distributions, and formula consistency."""

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


# 2. 欄位；type 用來區分房地、房地＋車位與土地交易。

COLUMNS = [
    "type",
    "housing_totprice",
    "h_price_m2",
    "h_price_pin",
    "trans_size",
    "parking_size",
    "parking_price",
]

NUMERIC_COLUMNS = [
    "housing_totprice",
    "h_price_m2",
    "h_price_pin",
    "trans_size",
    "parking_size",
    "parking_price",
]

CHUNK_SIZE = 100_000


# 3. 標準資料保留的交易類型

KEEP_TYPES = {
    "房地(土地+建物)",
    "房地(土地+建物)+車位",
    "土地",
}

HOUSE_TYPE = "房地(土地+建物)"
HOUSE_PARKING_TYPE = "房地(土地+建物)+車位"


# 4. 單價只允許每平方公尺 ±1 元的絕對誤差，不另加比例誤差。

PRICE_ATOL = 1


# 5. 統計容器

stats = {
    column: {
        "rows": 0,
        "missing": 0,
        "zero": 0,
        "negative": 0,
        "min": None,
        "max": None,
    }
    for column in NUMERIC_COLUMNS
}


# 公式驗證計數

ping_validation = {
    "comparable": 0,
    "matched": 0,
}

house_validation = {
    "comparable": 0,
    "matched": 0,
}

parking_validation = {
    "parking_type_rows": 0,
    "parking_price_valid": 0,
    "parking_size_valid": 0,
    "both_valid": 0,
    "formula_comparable": 0,
    "formula_matched": 0,
}


# 6. 每批固定抽樣最多 2,000 筆，近似計算 p1／中位數／p99；
# 固定 random_state 以便重現，並避免保留全部數值。

sample_chunks = []


# 7. 分批讀取原始 `.dta`

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

        print(
            f"Processing chunk {chunk_number:,}"
        )

        # 7.1 只保留標準資料預定納入的交易類型

        chunk = chunk[
            chunk["type"].isin(KEEP_TYPES)
        ].copy()


        # 7.2 目標欄位無法轉為數值時設為 NaN，並納入格式異常統計。

        for column in NUMERIC_COLUMNS:
            chunk[column] = pd.to_numeric(
                chunk[column],
                errors="coerce",
            )


        # 7.3 基本數值剖析

        for column in NUMERIC_COLUMNS:

            series = chunk[column]

            stats[column]["rows"] += len(series)
            stats[column]["missing"] += int(
                series.isna().sum()
            )

            valid = series.dropna()

            stats[column]["zero"] += int(
                (valid == 0).sum()
            )

            stats[column]["negative"] += int(
                (valid < 0).sum()
            )

            if not valid.empty:

                current_min = valid.min()
                current_max = valid.max()

                if stats[column]["min"] is None:
                    stats[column]["min"] = current_min
                    stats[column]["max"] = current_max

                else:
                    stats[column]["min"] = min(
                        stats[column]["min"],
                        current_min,
                    )

                    stats[column]["max"] = max(
                        stats[column]["max"],
                        current_max,
                    )


        # 7.4 抽樣估計百分位數

        if len(chunk) > 0:

            sample_n = min(
                2_000,
                len(chunk),
            )

            sample_chunks.append(
                chunk[
                    ["type"] + NUMERIC_COLUMNS
                ].sample(
                    n=sample_n,
                    random_state=chunk_number,
                )
            )


        # 8. 驗證 h_price_pin ≈ h_price_m2 × 3.305785

        ping_mask = (
            chunk["h_price_m2"].notna()
            & chunk["h_price_pin"].notna()
            & (chunk["h_price_m2"] > 0)
            & (chunk["h_price_pin"] > 0)
        )

        actual_ping = chunk.loc[
            ping_mask,
            "h_price_pin",
        ]

        expected_ping = (
            chunk.loc[
                ping_mask,
                "h_price_m2",
            ]
            * 3.305785
        )

        ping_matches = np.isclose(
            actual_ping,
            expected_ping,
            rtol=0,
            atol=PRICE_ATOL,
        )

        ping_validation["comparable"] += len(
            actual_ping
        )

        ping_validation["matched"] += int(
            ping_matches.sum()
        )


        # 9. 驗證無車位房地交易的單價公式
        # h_price_m2 ≈ housing_totprice / trans_size

        house_mask = (
            (chunk["type"] == HOUSE_TYPE)
            & chunk["housing_totprice"].notna()
            & chunk["trans_size"].notna()
            & chunk["h_price_m2"].notna()
            & (chunk["housing_totprice"] > 0)
            & (chunk["trans_size"] > 0)
            & (chunk["h_price_m2"] > 0)
        )

        expected_house_price = (
            chunk.loc[
                house_mask,
                "housing_totprice",
            ]
            /
            chunk.loc[
                house_mask,
                "trans_size",
            ]
        )

        actual_house_price = chunk.loc[
            house_mask,
            "h_price_m2",
        ]

        house_matches = np.isclose(
            actual_house_price,
            expected_house_price,
            rtol=0,
            atol=PRICE_ATOL,
        )

        house_validation["comparable"] += len(
            actual_house_price
        )

        house_validation["matched"] += int(
            house_matches.sum()
        )


        # 10. 車位欄位完整性

        parking_rows = chunk[
            chunk["type"] == HOUSE_PARKING_TYPE
        ]

        parking_validation[
            "parking_type_rows"
        ] += len(parking_rows)


        parking_price_valid = (
            parking_rows["parking_price"].notna()
            & (parking_rows["parking_price"] > 0)
        )

        parking_size_valid = (
            parking_rows["parking_size"].notna()
            & (parking_rows["parking_size"] > 0)
        )

        both_valid = (
            parking_price_valid
            & parking_size_valid
        )

        parking_validation[
            "parking_price_valid"
        ] += int(parking_price_valid.sum())

        parking_validation[
            "parking_size_valid"
        ] += int(parking_size_valid.sum())

        parking_validation[
            "both_valid"
        ] += int(both_valid.sum())


        # 11. 驗證扣除車位價格與面積後的單價

        formula_mask = (
            (chunk["type"] == HOUSE_PARKING_TYPE)
            & chunk["housing_totprice"].notna()
            & chunk["parking_price"].notna()
            & chunk["trans_size"].notna()
            & chunk["parking_size"].notna()
            & chunk["h_price_m2"].notna()

            & (
                chunk["housing_totprice"]
                > chunk["parking_price"]
            )

            & (
                chunk["trans_size"]
                > chunk["parking_size"]
            )

            & (chunk["h_price_m2"] > 0)
        )

        expected_parking_adjusted = (
            (
                chunk.loc[
                    formula_mask,
                    "housing_totprice",
                ]
                -
                chunk.loc[
                    formula_mask,
                    "parking_price",
                ]
            )
            /
            (
                chunk.loc[
                    formula_mask,
                    "trans_size",
                ]
                -
                chunk.loc[
                    formula_mask,
                    "parking_size",
                ]
            )
        )

        actual_parking_price = chunk.loc[
            formula_mask,
            "h_price_m2",
        ]

        parking_matches = np.isclose(
            actual_parking_price,
            expected_parking_adjusted,
            rtol=0,
            atol=PRICE_ATOL,
        )

        parking_validation[
            "formula_comparable"
        ] += len(actual_parking_price)

        parking_validation[
            "formula_matched"
        ] += int(
            parking_matches.sum()
        )


# 12. 建立數值摘要

summary_rows = []

for column, values in stats.items():

    rows = values["rows"]
    missing = values["missing"]

    summary_rows.append(
        {
            "column_name": column,
            "rows": rows,
            "missing_count": missing,
            "missing_rate": (
                missing / rows
                if rows
                else np.nan
            ),
            "zero_count": values["zero"],
            "negative_count": values["negative"],
            "min": values["min"],
            "max": values["max"],
        }
    )

summary_df = pd.DataFrame(summary_rows)


# 13. 近似百分位數

sample_df = pd.concat(
    sample_chunks,
    ignore_index=True,
)

percentile_rows = []

for column in NUMERIC_COLUMNS:

    valid = sample_df[column].dropna()

    valid = valid[
        valid > 0
    ]

    if valid.empty:
        continue

    percentile_rows.append(
        {
            "column_name": column,
            "sample_rows": len(valid),
            "p01": valid.quantile(0.01),
            "p50": valid.quantile(0.50),
            "p99": valid.quantile(0.99),
        }
    )

percentile_df = pd.DataFrame(
    percentile_rows
)


# 14. 公式驗證摘要

validation_rows = [
    {
        "validation": "h_price_pin_vs_h_price_m2",
        "comparable_rows": ping_validation[
            "comparable"
        ],
        "matched_rows": ping_validation[
            "matched"
        ],
    },
    {
        "validation": "house_price_formula",
        "comparable_rows": house_validation[
            "comparable"
        ],
        "matched_rows": house_validation[
            "matched"
        ],
    },
    {
        "validation": "parking_adjusted_price_formula",
        "comparable_rows": parking_validation[
            "formula_comparable"
        ],
        "matched_rows": parking_validation[
            "formula_matched"
        ],
    },
]

validation_df = pd.DataFrame(
    validation_rows
)

validation_df["match_rate"] = (
    validation_df["matched_rows"]
    /
    validation_df["comparable_rows"]
)


# 15. 車位欄位完整性摘要

parking_total = parking_validation[
    "parking_type_rows"
]

parking_df = pd.DataFrame(
    [
        {
            "parking_transaction_rows": parking_total,
            "parking_price_valid": parking_validation[
                "parking_price_valid"
            ],
            "parking_price_valid_rate": (
                parking_validation[
                    "parking_price_valid"
                ]
                / parking_total
            ),
            "parking_size_valid": parking_validation[
                "parking_size_valid"
            ],
            "parking_size_valid_rate": (
                parking_validation[
                    "parking_size_valid"
                ]
                / parking_total
            ),
            "both_valid": parking_validation[
                "both_valid"
            ],
            "both_valid_rate": (
                parking_validation[
                    "both_valid"
                ]
                / parking_total
            ),
        }
    ]
)


# 16. 儲存輸出

summary_df.to_csv(
    OUTPUT_DIR / "price_area_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

percentile_df.to_csv(
    OUTPUT_DIR / "price_area_percentiles.csv",
    index=False,
    encoding="utf-8-sig",
)

validation_df.to_csv(
    OUTPUT_DIR / "price_formula_validation.csv",
    index=False,
    encoding="utf-8-sig",
)

parking_df.to_csv(
    OUTPUT_DIR / "parking_completeness.csv",
    index=False,
    encoding="utf-8-sig",
)


# 17. 顯示結果

print("\nNumeric summary:")
print(summary_df.to_string(index=False))

print("\nApproximate percentiles:")
print(percentile_df.to_string(index=False))

print("\nFormula validation:")
print(validation_df.to_string(index=False))

print("\nParking completeness:")
print(parking_df.to_string(index=False))
