from collections import Counter
from pathlib import Path

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

SUMMARY_DIR = (
    PROJECT_ROOT
    / "profiling_output"
    / "summary"
)

SUMMARY_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# 2. Columns
# ============================================================

COLUMNS = [
    "type",
    "trans_y",
    "trans_m",
    "date_complete",
    "county",
    "town",
    "building_type",
    "usage",
    "note_yn",
]

HOUSE_TYPES = {
    "房地(土地+建物)",
    "房地(土地+建物)+車位",
}

CHUNK_SIZE = 100_000


# ============================================================
# 3. Containers
# ============================================================

five_digit_years = Counter()
five_digit_building_types = Counter()
five_digit_transaction_years = Counter()

future_gap_groups = Counter()
future_by_building_type = Counter()
future_by_transaction_year = Counter()
future_by_type = Counter()
future_note_flag = Counter()

total_future_rows = 0

five_digit_examples = []

MAX_EXAMPLES = 50


# ============================================================
# 4. Read data
# ============================================================

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

        print(
            f"Processing chunk {chunk_number:,}"
        )

        # 房屋 completion date
        # 對純土地沒有分析意義。
        house = chunk[
            chunk["type"].isin(HOUSE_TYPES)
        ].copy()

        raw_date = pd.to_numeric(
            house["date_complete"],
            errors="coerce",
        )

        # 只看 positive integer-like values
        valid = raw_date[
            raw_date > 0
        ].round().astype("Int64")

        date_string = (
            valid
            .astype(str)
        )

        digit_length = (
            date_string
            .str.len()
        )


        # ====================================================
        # 5. Five-digit values
        # ============================================================
        #
        # 假設：
        #
        # 60101
        #
        # 前面：
        # 6 = ROC year
        #
        # 後四碼：
        # 0101 = Jan 1
        #
        # 這次不是直接相信它，
        # 而是看這些 rows 的 context 是否合理。

        five_mask = (
            digit_length == 5
        )

        five_strings = date_string[
            five_mask
        ]

        five_index = (
            five_strings.index
        )

        if len(five_index) > 0:

            roc_year = (
                five_strings
                .str[:-4]
                .astype(int)
            )

            for value, count in (
                roc_year
                .value_counts()
                .items()
            ):

                five_digit_years[
                    int(value)
                ] += int(count)


            building_types = (
                house.loc[
                    five_index,
                    "building_type",
                ]
                .fillna("<NA>")
                .astype(str)
            )

            five_digit_building_types.update(
                building_types.tolist()
            )


            transaction_year = (
                pd.to_numeric(
                    house.loc[
                        five_index,
                        "trans_y",
                    ],
                    errors="coerce",
                )
            )

            five_digit_transaction_years.update(
                transaction_year
                .dropna()
                .astype(int)
                .tolist()
            )


            # -----------------------------------------------
            # 保留少量 aggregate-safe examples。
            #
            # address 沒有讀進來，
            # 所以不會輸出具體門牌。
            # -----------------------------------------------

            if (
                len(five_digit_examples)
                < MAX_EXAMPLES
            ):

                remaining = (
                    MAX_EXAMPLES
                    - len(
                        five_digit_examples
                    )
                )

                example_index = (
                    five_index[:remaining]
                )

                for idx in example_index:

                    five_digit_examples.append(
                        {
                            "raw_date_complete": (
                                int(valid.loc[idx])
                            ),
                            "transaction_year": (
                                house.loc[
                                    idx,
                                    "trans_y",
                                ]
                            ),
                            "county": (
                                house.loc[
                                    idx,
                                    "county",
                                ]
                            ),
                            "town": (
                                house.loc[
                                    idx,
                                    "town",
                                ]
                            ),
                            "building_type": (
                                house.loc[
                                    idx,
                                    "building_type",
                                ]
                            ),
                            "usage": (
                                house.loc[
                                    idx,
                                    "usage",
                                ]
                            ),
                        }
                    )


        # ====================================================
        # 6. Parse completion year/month
        # ============================================================
        #
        # 只處理至少 5 digits。
        #
        # 前面 digits = ROC year
        # 最後四碼 = MMDD

        parse_mask = (
            digit_length >= 5
        )

        parse_strings = (
            date_string[
                parse_mask
            ]
        )

        parse_index = (
            parse_strings.index
        )

        roc_year = (
            parse_strings
            .str[:-4]
            .astype(int)
        )

        completion_month = (
            parse_strings
            .str[-4:-2]
            .astype(int)
        )

        completion_day = (
            parse_strings
            .str[-2:]
            .astype(int)
        )


        # ====================================================
        # 7. Calendar validation
        # ============================================================

        gregorian_year = (
            roc_year + 1911
        )

        iso_date = (
            gregorian_year
            .astype(str)
            .str.zfill(4)
            + "-"
            + completion_month
            .astype(str)
            .str.zfill(2)
            + "-"
            + completion_day
            .astype(str)
            .str.zfill(2)
        )

        completion_date = pd.to_datetime(
            iso_date,
            errors="coerce",
        )

        calendar_valid = (
            completion_date.notna()
        )

        valid_index = (
            completion_date[
                calendar_valid
            ].index
        )


        # ====================================================
        # 8. Transaction year/month
        # ============================================================

        trans_year = pd.to_numeric(
            house.loc[
                valid_index,
                "trans_y",
            ],
            errors="coerce",
        )

        trans_month = pd.to_numeric(
            house.loc[
                valid_index,
                "trans_m",
            ],
            errors="coerce",
        )

        transaction_valid = (
            trans_year.notna()
            & trans_month.between(
                1,
                12,
            )
        )

        compare_index = (
            trans_year[
                transaction_valid
            ].index
        )


        # ====================================================
        # 9. Calculate month difference
        # ============================================================
        #
        # 不需要完整 transaction day。
        #
        # 因為我們只想知道：
        #
        # completion month
        # 比 transaction month
        # 晚多少個月。
        #
        # 公式：
        #
        # (year difference × 12)
        # +
        # month difference

        completion_year_compare = (
            roc_year.loc[
                compare_index
            ]
        )

        completion_month_compare = (
            completion_month.loc[
                compare_index
            ]
        )

        transaction_year_compare = (
            trans_year.loc[
                compare_index
            ].astype(int)
        )

        transaction_month_compare = (
            trans_month.loc[
                compare_index
            ].astype(int)
        )

        month_gap = (
            (
                completion_year_compare
                - transaction_year_compare
            )
            * 12
            +
            (
                completion_month_compare
                - transaction_month_compare
            )
        )


        # completion after transaction
        future_mask = (
            month_gap > 0
        )

        future_gap = (
            month_gap[
                future_mask
            ]
        )

        future_index = (
            future_gap.index
        )

        total_future_rows += len(
            future_gap
        )


        # ====================================================
        # 10. Gap distribution
        # ============================================================

        future_gap_groups[
            "1_3_months"
        ] += int(
            future_gap.between(
                1,
                3,
            ).sum()
        )

        future_gap_groups[
            "4_6_months"
        ] += int(
            future_gap.between(
                4,
                6,
            ).sum()
        )

        future_gap_groups[
            "7_12_months"
        ] += int(
            future_gap.between(
                7,
                12,
            ).sum()
        )

        future_gap_groups[
            "13_24_months"
        ] += int(
            future_gap.between(
                13,
                24,
            ).sum()
        )

        future_gap_groups[
            "25_60_months"
        ] += int(
            future_gap.between(
                25,
                60,
            ).sum()
        )

        future_gap_groups[
            "over_60_months"
        ] += int(
            (future_gap > 60).sum()
        )


        # ====================================================
        # 11. Future completion characteristics
        # ============================================================

        future_by_building_type.update(
            house.loc[
                future_index,
                "building_type",
            ]
            .fillna("<NA>")
            .astype(str)
            .tolist()
        )


        future_by_transaction_year.update(
            pd.to_numeric(
                house.loc[
                    future_index,
                    "trans_y",
                ],
                errors="coerce",
            )
            .dropna()
            .astype(int)
            .tolist()
        )


        future_by_type.update(
            house.loc[
                future_index,
                "type",
            ]
            .fillna("<NA>")
            .astype(str)
            .tolist()
        )


        future_note_flag.update(
            house.loc[
                future_index,
                "note_yn",
            ]
            .fillna("<NA>")
            .astype(str)
            .tolist()
        )


# ============================================================
# 12. Output helpers
# ============================================================

def counter_to_df(
    counter,
    key_name,
    total=None,
):
    rows = []

    for value, count in (
        counter.most_common()
    ):

        row = {
            key_name: value,
            "row_count": count,
        }

        if total:
            row["row_share"] = (
                count / total
            )

        rows.append(row)

    return pd.DataFrame(rows)


# ============================================================
# 13. Build outputs
# ============================================================

five_year_df = counter_to_df(
    five_digit_years,
    "roc_completion_year",
)

five_type_df = counter_to_df(
    five_digit_building_types,
    "building_type",
)

five_trans_year_df = counter_to_df(
    five_digit_transaction_years,
    "transaction_year",
)

five_examples_df = pd.DataFrame(
    five_digit_examples
)


future_gap_df = counter_to_df(
    future_gap_groups,
    "gap_group",
    total_future_rows,
)

future_building_df = counter_to_df(
    future_by_building_type,
    "building_type",
    total_future_rows,
)

future_year_df = counter_to_df(
    future_by_transaction_year,
    "transaction_year",
    total_future_rows,
)

future_type_df = counter_to_df(
    future_by_type,
    "transaction_type",
    total_future_rows,
)

future_note_df = counter_to_df(
    future_note_flag,
    "note_flag",
    total_future_rows,
)


# ============================================================
# 14. Save
# ============================================================

five_year_df.to_csv(
    SUMMARY_DIR
    / "completion_5digit_year_distribution.csv",
    index=False,
    encoding="utf-8-sig",
)

five_type_df.to_csv(
    SUMMARY_DIR
    / "completion_5digit_building_type.csv",
    index=False,
    encoding="utf-8-sig",
)

five_trans_year_df.to_csv(
    SUMMARY_DIR
    / "completion_5digit_transaction_year.csv",
    index=False,
    encoding="utf-8-sig",
)

five_examples_df.to_csv(
    SUMMARY_DIR
    / "completion_5digit_examples.csv",
    index=False,
    encoding="utf-8-sig",
)

future_gap_df.to_csv(
    SUMMARY_DIR
    / "completion_after_transaction_gap.csv",
    index=False,
    encoding="utf-8-sig",
)

future_building_df.to_csv(
    SUMMARY_DIR
    / "completion_after_transaction_building_type.csv",
    index=False,
    encoding="utf-8-sig",
)

future_year_df.to_csv(
    SUMMARY_DIR
    / "completion_after_transaction_year.csv",
    index=False,
    encoding="utf-8-sig",
)

future_type_df.to_csv(
    SUMMARY_DIR
    / "completion_after_transaction_type.csv",
    index=False,
    encoding="utf-8-sig",
)

future_note_df.to_csv(
    SUMMARY_DIR
    / "completion_after_transaction_note.csv",
    index=False,
    encoding="utf-8-sig",
)


# ============================================================
# 15. Print
# ============================================================

print("\n5-digit ROC completion years:")
print(five_year_df.to_string(index=False))

print("\n5-digit building types:")
print(five_type_df.to_string(index=False))

print("\n5-digit transaction years:")
print(five_trans_year_df.to_string(index=False))

print("\n5-digit examples:")
print(five_examples_df.to_string(index=False))

print("\nCompletion after transaction — gap:")
print(future_gap_df.to_string(index=False))

print("\nCompletion after transaction — building type:")
print(future_building_df.to_string(index=False))

print("\nCompletion after transaction — transaction year:")
print(future_year_df.to_string(index=False))

print("\nCompletion after transaction — transaction type:")
print(future_type_df.to_string(index=False))

print("\nCompletion after transaction — note flag:")
print(future_note_df.to_string(index=False))