from collections import Counter, defaultdict
from pathlib import Path
import sqlite3

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

SUMMARY_DIR = (
    PROJECT_ROOT
    / "profiling_output"
    / "summary"
)

PRIVATE_DIR = (
    PROJECT_ROOT
    / "profiling_output"
    / "private"
)

SUMMARY_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

PRIVATE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# 2. Columns
# ============================================================

COLUMNS = [
    "no",
    "type",

    "trans_y",
    "trans_m",
    "date_complete",

    "note_yn",
    "note",

    "trans_size",
    "mainbuilding_area",
    "affbuilding_area",
    "balcony_area",
    "trans_landsize",

    # 尚未正式收尾的 low-cardinality fields
    "story",
    "manage",
    "parking_type",
    "usage_type",
    "nonurban_type",
    "nonurban_categ",
]


HOUSE_TYPES = {
    "房地(土地+建物)",
    "房地(土地+建物)+車位",
}

HOUSE_PARKING_TYPE = "房地(土地+建物)+車位"

KEEP_TYPES = HOUSE_TYPES | {"土地"}

CHUNK_SIZE = 100_000

TOP_N = 30


# ============================================================
# 3. Exact global uniqueness check for no
# ============================================================
#
# 之前我們只知道：
#
# no_missing = 0
# no_duplicates_within_chunks = 0
#
# 但「每個 chunk 內沒有 duplicate」
# 不代表不同 chunks 之間沒有相同 no。
#
# 因此現在做 exact global uniqueness。
#
#
# 為什麼使用 SQLite？
#
# 如果直接建立：
#
# seen = set()
#
# 並塞入 438 萬個 no，
# Python set 可能占掉大量 RAM。
#
# SQLite 則把 unique values 放在 disk-backed database，
# 可以做 exact uniqueness check，而不必全部留在 RAM。
#
# 這只是一個 profiling temporary database，
# 不是正式 PostgreSQL architecture。

TEMP_DB_PATH = (
    PRIVATE_DIR
    / "no_uniqueness_temp.sqlite"
)

# 如果上次程式中途停止留下 temp file，
# 重新執行前先刪除。
TEMP_DB_PATH.unlink(
    missing_ok=True
)

conn = sqlite3.connect(
    TEMP_DB_PATH
)

conn.execute(
    """
    CREATE TABLE seen_no (
        no TEXT PRIMARY KEY
    )
    """
)

no_total_rows = 0
no_missing = 0
no_blank = 0


# ============================================================
# 4. Date profiling containers
# ============================================================

date_stats = Counter()

date_digit_counts = Counter()

date_valid_by_length = Counter()

date_examples = defaultdict(list)

date_example_seen = defaultdict(set)

MAX_DATE_EXAMPLES = 15


# ============================================================
# 5. Note consistency
# ============================================================

note_consistency = Counter()


# ============================================================
# 6. Building-area component statistics
# ============================================================

AREA_COLUMNS = [
    "trans_size",
    "mainbuilding_area",
    "affbuilding_area",
    "balcony_area",
]

area_stats = {
    column: {
        "rows": 0,
        "missing": 0,
        "zero": 0,
        "negative": 0,
        "min": None,
        "max": None,
    }
    for column in AREA_COLUMNS
}

area_relationships = Counter()


# ============================================================
# 7. Land-area statistics by transaction type
# ============================================================

land_area_stats = {
    transaction_type: {
        "rows": 0,
        "missing": 0,
        "zero": 0,
        "negative": 0,
        "min": None,
        "max": None,
    }
    for transaction_type in KEEP_TYPES
}


# ============================================================
# 8. Remaining category fields
# ============================================================

category_stats = {
    "story": Counter(),
    "manage": Counter(),
    "parking_type": Counter(),
    "usage_type": Counter(),
    "nonurban_type": Counter(),
    "nonurban_categ": Counter(),
}

category_rows = Counter()
category_missing = Counter()


def normalize_category(value):
    """
    將 missing / blank 統一成 profiling label。
    """

    if pd.isna(value):
        return "<PANDAS_NA>"

    value = str(value).strip()

    if value == "":
        return "<BLANK_STRING>"

    return value


# ============================================================
# 9. Read .dta
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


        # ====================================================
        # 10. no — exact global uniqueness
        # ====================================================

        no_total_rows += len(chunk)

        no_series = chunk["no"]

        no_missing += int(
            no_series.isna().sum()
        )

        valid_no = (
            no_series
            .dropna()
            .astype(str)
            .str.strip()
        )

        blank_mask = valid_no.eq("")

        no_blank += int(
            blank_mask.sum()
        )

        valid_no = valid_no[
            ~blank_mask
        ]


        # executemany：
        #
        # 一次把很多 parameters 交給 SQLite。
        #
        # INSERT OR IGNORE：
        #
        # 第一次遇到 no → insert
        # 再次遇到相同 no → 因 PRIMARY KEY 衝突而 ignore
        #
        # 因此最後 table row count
        # 就是 exact distinct no count。

        conn.executemany(
            """
            INSERT OR IGNORE
            INTO seen_no (no)
            VALUES (?)
            """,
            ((value,) for value in valid_no),
        )

        conn.commit()


        # ====================================================
        # 11. House subset
        # ====================================================

        house = chunk[
            chunk["type"].isin(HOUSE_TYPES)
        ].copy()


        # ====================================================
        # 12. date_complete
        # ====================================================

        raw_date = pd.to_numeric(
            house["date_complete"],
            errors="coerce",
        )


        date_stats["rows"] += len(raw_date)

        date_stats["missing"] += int(
            raw_date.isna().sum()
        )

        date_stats["zero"] += int(
            (raw_date == 0).sum()
        )

        date_stats["negative"] += int(
            (raw_date < 0).sum()
        )


        positive_date = raw_date[
            raw_date > 0
        ]


        # date_complete 看起來是 integer-like numeric，
        # 但 raw dtype 是 float。
        #
        # round() 後檢查是否真的接近 integer。

        rounded_date = (
            positive_date
            .round()
        )

        integer_like = np.isclose(
            positive_date,
            rounded_date,
            rtol=0,
            atol=1e-9,
        )

        date_stats[
            "non_integer_positive"
        ] += int(
            (~integer_like).sum()
        )


        integer_date = (
            rounded_date[
                integer_like
            ]
            .astype("int64")
        )

        date_string = (
            integer_date
            .astype(str)
        )

        digit_length = (
            date_string
            .str.len()
        )


        # ----------------------------------------------------
        # 12.1 Digit distribution + examples
        # ----------------------------------------------------

        for length, count in (
            digit_length
            .value_counts()
            .items()
        ):

            length = int(length)

            date_digit_counts[length] += int(
                count
            )


            examples = (
                date_string[
                    digit_length == length
                ]
                .drop_duplicates()
            )

            for value in examples:

                if (
                    len(
                        date_examples[length]
                    )
                    >= MAX_DATE_EXAMPLES
                ):
                    break

                if (
                    value
                    not in date_example_seen[length]
                ):

                    date_examples[
                        length
                    ].append(
                        value
                    )

                    date_example_seen[
                        length
                    ].add(
                        value
                    )


        # ----------------------------------------------------
        # 12.2 Test ROC variable-length hypothesis
        # ----------------------------------------------------
        #
        # Hypothesis:
        #
        # 最後四碼：
        # MMDD
        #
        # 前面所有 digits：
        # ROC year
        #
        # Examples:
        #
        # 990101
        # → 99 / 01 / 01
        #
        # 1010101
        # → 101 / 01 / 01
        #
        # 90101
        # → 9 / 01 / 01

        parseable_length = (
            digit_length >= 5
        )

        parse_string = date_string[
            parseable_length
        ]

        roc_year = (
            parse_string
            .str[:-4]
            .astype(int)
        )

        month = (
            parse_string
            .str[-4:-2]
            .astype(int)
        )

        day = (
            parse_string
            .str[-2:]
            .astype(int)
        )


        gregorian_year = (
            roc_year + 1911
        )


        # 建立 ISO format：
        #
        # YYYY-MM-DD
        #
        # errors="coerce"：
        # 不合法日期，例如 2020-13-45，
        # 會變成 NaT。

        iso_date = (
            gregorian_year
            .astype(str)
            .str.zfill(4)
            + "-"
            + month
            .astype(str)
            .str.zfill(2)
            + "-"
            + day
            .astype(str)
            .str.zfill(2)
        )

        parsed_date = pd.to_datetime(
            iso_date,
            errors="coerce",
        )

        valid_calendar = (
            parsed_date.notna()
        )

        date_stats[
            "roc_pattern_comparable"
        ] += len(parsed_date)

        date_stats[
            "roc_pattern_valid_calendar"
        ] += int(
            valid_calendar.sum()
        )


        for length in (
            digit_length[
                parseable_length
            ]
            .unique()
        ):

            mask = (
                digit_length[
                    parseable_length
                ]
                == length
            )

            date_valid_by_length[
                int(length)
            ] += int(
                valid_calendar[
                    mask
                ].sum()
            )


        # ----------------------------------------------------
        # 12.3 Completion date after transaction date?
        # ----------------------------------------------------

        valid_idx = (
            parsed_date[
                valid_calendar
            ]
            .index
        )

        transaction_year = pd.to_numeric(
            house.loc[
                valid_idx,
                "trans_y",
            ],
            errors="coerce",
        )

        transaction_month = pd.to_numeric(
            house.loc[
                valid_idx,
                "trans_m",
            ],
            errors="coerce",
        )


        completion_year = (
            roc_year.loc[
                valid_idx
            ]
        )

        completion_month = (
            month.loc[
                valid_idx
            ]
        )


        comparison_valid = (
            transaction_year.notna()
            & transaction_month.notna()
            & transaction_month.between(
                1,
                12,
            )
        )


        completion_yyyymm = (
            completion_year[
                comparison_valid
            ]
            * 100
            +
            completion_month[
                comparison_valid
            ]
        )

        transaction_yyyymm = (
            transaction_year[
                comparison_valid
            ]
            * 100
            +
            transaction_month[
                comparison_valid
            ]
        )


        future_completion = (
            completion_yyyymm
            >
            transaction_yyyymm
        )


        date_stats[
            "transaction_comparable"
        ] += len(
            future_completion
        )

        date_stats[
            "completion_after_transaction"
        ] += int(
            future_completion.sum()
        )


        # ====================================================
        # 13. note_yn vs note
        # ====================================================

        note_subset = chunk[
            chunk["type"].isin(
                KEEP_TYPES
            )
        ][
            [
                "note_yn",
                "note",
            ]
        ].copy()


        note_flag = (
            note_subset["note_yn"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        note_text = (
            note_subset["note"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        note_nonblank = (
            note_text.ne("")
        )


        note_consistency[
            "Y_and_nonblank"
        ] += int(
            (
                note_flag.eq("Y")
                & note_nonblank
            ).sum()
        )

        note_consistency[
            "Y_and_blank"
        ] += int(
            (
                note_flag.eq("Y")
                & ~note_nonblank
            ).sum()
        )

        note_consistency[
            "N_and_nonblank"
        ] += int(
            (
                note_flag.eq("N")
                & note_nonblank
            ).sum()
        )

        note_consistency[
            "N_and_blank"
        ] += int(
            (
                note_flag.eq("N")
                & ~note_nonblank
            ).sum()
        )

        note_consistency[
            "other_flag"
        ] += int(
            (~note_flag.isin(["Y", "N"]))
            .sum()
        )


        # ====================================================
        # 14. Building-area components
        # ====================================================

        for column in AREA_COLUMNS:

            series = pd.to_numeric(
                house[column],
                errors="coerce",
            )

            stat = area_stats[
                column
            ]

            stat["rows"] += len(series)

            stat["missing"] += int(
                series.isna().sum()
            )

            valid = series.dropna()

            stat["zero"] += int(
                (valid == 0).sum()
            )

            stat["negative"] += int(
                (valid < 0).sum()
            )

            if not valid.empty:

                current_min = valid.min()
                current_max = valid.max()

                if stat["min"] is None:

                    stat["min"] = (
                        current_min
                    )

                    stat["max"] = (
                        current_max
                    )

                else:

                    stat["min"] = min(
                        stat["min"],
                        current_min,
                    )

                    stat["max"] = max(
                        stat["max"],
                        current_max,
                    )


        area = house[
            AREA_COLUMNS
        ].apply(
            pd.to_numeric,
            errors="coerce",
        )


        # ----------------------------------------------------
        # Hypothesis 1:
        #
        # balcony_area 是否 <= affbuilding_area
        # ----------------------------------------------------

        mask = (
            area[
                "balcony_area"
            ].notna()
            &
            area[
                "affbuilding_area"
            ].notna()
        )

        area_relationships[
            "balcony_vs_aff_comparable"
        ] += int(
            mask.sum()
        )

        area_relationships[
            "balcony_le_aff"
        ] += int(
            (
                area.loc[
                    mask,
                    "balcony_area",
                ]
                <=
                area.loc[
                    mask,
                    "affbuilding_area",
                ]
                + 0.01
            ).sum()
        )


        # ----------------------------------------------------
        # Hypothesis 2:
        #
        # main + aff <= total building area
        #
        # 其差額通常可能包含共有部分。
        # ----------------------------------------------------

        mask = (
            area[
                "mainbuilding_area"
            ].notna()
            &
            area[
                "affbuilding_area"
            ].notna()
            &
            area[
                "trans_size"
            ].notna()
        )

        area_relationships[
            "main_plus_aff_comparable"
        ] += int(
            mask.sum()
        )

        area_relationships[
            "main_plus_aff_le_total"
        ] += int(
            (
                area.loc[
                    mask,
                    "mainbuilding_area",
                ]
                +
                area.loc[
                    mask,
                    "affbuilding_area",
                ]
                <=
                area.loc[
                    mask,
                    "trans_size",
                ]
                + 0.01
            ).sum()
        )


        # ----------------------------------------------------
        # Diagnostic only:
        #
        # main + aff + balcony <= total
        #
        # 用這個結果協助判斷 balcony
        # 是否已包含在 affbuilding_area 中。
        # ----------------------------------------------------

        mask = area.notna().all(
            axis=1
        )

        area_relationships[
            "all_components_comparable"
        ] += int(
            mask.sum()
        )

        area_relationships[
            "main_aff_balcony_le_total"
        ] += int(
            (
                area.loc[
                    mask,
                    "mainbuilding_area",
                ]
                +
                area.loc[
                    mask,
                    "affbuilding_area",
                ]
                +
                area.loc[
                    mask,
                    "balcony_area",
                ]
                <=
                area.loc[
                    mask,
                    "trans_size",
                ]
                + 0.01
            ).sum()
        )


        # ====================================================
        # 15. trans_landsize by transaction type
        # ====================================================

        keep_subset = chunk[
            chunk["type"].isin(
                KEEP_TYPES
            )
        ]

        for transaction_type in KEEP_TYPES:

            series = pd.to_numeric(
                keep_subset.loc[
                    keep_subset["type"]
                    == transaction_type,
                    "trans_landsize",
                ],
                errors="coerce",
            )

            stat = land_area_stats[
                transaction_type
            ]

            stat["rows"] += len(series)

            stat["missing"] += int(
                series.isna().sum()
            )

            valid = series.dropna()

            stat["zero"] += int(
                (valid == 0).sum()
            )

            stat["negative"] += int(
                (valid < 0).sum()
            )

            if not valid.empty:

                current_min = valid.min()
                current_max = valid.max()

                if stat["min"] is None:

                    stat["min"] = current_min
                    stat["max"] = current_max

                else:

                    stat["min"] = min(
                        stat["min"],
                        current_min,
                    )

                    stat["max"] = max(
                        stat["max"],
                        current_max,
                    )


        # ====================================================
        # 16. Remaining categorical fields
        # ====================================================

        # story / manage：房地交易
        for column in [
            "story",
            "manage",
        ]:

            series = house[column]

            category_rows[
                column
            ] += len(series)

            normalized = series.map(
                normalize_category
            )

            category_missing[
                column
            ] += int(
                normalized.isin(
                    [
                        "<PANDAS_NA>",
                        "<BLANK_STRING>",
                    ]
                ).sum()
            )

            category_stats[
                column
            ].update(
                normalized.tolist()
            )


        # parking_type：
        # 只看房地+車位

        parking_subset = chunk[
            chunk["type"]
            == HOUSE_PARKING_TYPE
        ]["parking_type"]

        category_rows[
            "parking_type"
        ] += len(
            parking_subset
        )

        normalized = parking_subset.map(
            normalize_category
        )

        category_missing[
            "parking_type"
        ] += int(
            normalized.isin(
                [
                    "<PANDAS_NA>",
                    "<BLANK_STRING>",
                ]
            ).sum()
        )

        category_stats[
            "parking_type"
        ].update(
            normalized.tolist()
        )


        # 土地使用分區：
        # canonical 保留的三類交易都看

        for column in [
            "usage_type",
            "nonurban_type",
            "nonurban_categ",
        ]:

            series = keep_subset[
                column
            ]

            category_rows[
                column
            ] += len(series)

            normalized = series.map(
                normalize_category
            )

            category_missing[
                column
            ] += int(
                normalized.isin(
                    [
                        "<PANDAS_NA>",
                        "<BLANK_STRING>",
                    ]
                ).sum()
            )

            category_stats[
                column
            ].update(
                normalized.tolist()
            )


# ============================================================
# 17. Finish no uniqueness
# ============================================================

distinct_no = conn.execute(
    """
    SELECT COUNT(*)
    FROM seen_no
    """
).fetchone()[0]

conn.close()


valid_no_count = (
    no_total_rows
    - no_missing
    - no_blank
)

duplicate_occurrences = (
    valid_no_count
    - distinct_no
)


no_df = pd.DataFrame(
    [
        {
            "total_rows": no_total_rows,
            "missing_count": no_missing,
            "blank_count": no_blank,
            "valid_no_count": valid_no_count,
            "distinct_no_count": distinct_no,
            "duplicate_occurrences": (
                duplicate_occurrences
            ),
            "globally_unique": (
                duplicate_occurrences == 0
            ),
        }
    ]
)


# Temp SQLite 已經完成使命，
# 刪除以避免留下大型 temporary file。

TEMP_DB_PATH.unlink(
    missing_ok=True
)


# ============================================================
# 18. Date outputs
# ============================================================

date_df = pd.DataFrame(
    [
        dict(date_stats)
    ]
)


date_detail_rows = []

for length, count in sorted(
    date_digit_counts.items()
):

    valid_count = (
        date_valid_by_length[length]
    )

    date_detail_rows.append(
        {
            "digit_length": length,
            "row_count": count,
            "valid_roc_calendar_count": (
                valid_count
            ),
            "valid_rate": (
                valid_count / count
                if count
                else None
            ),
            "examples": " | ".join(
                date_examples[length]
            ),
        }
    )

date_detail_df = pd.DataFrame(
    date_detail_rows
)


# ============================================================
# 19. Note output
# ============================================================

note_df = pd.DataFrame(
    [
        dict(note_consistency)
    ]
)


# ============================================================
# 20. Area outputs
# ============================================================

area_rows = []

for column, stat in area_stats.items():

    area_rows.append(
        {
            "column_name": column,
            **stat,
        }
    )

area_df = pd.DataFrame(
    area_rows
)


relationship_rows = []

for key, value in (
    area_relationships.items()
):

    relationship_rows.append(
        {
            "metric": key,
            "count": value,
        }
    )

relationship_df = pd.DataFrame(
    relationship_rows
)


# ============================================================
# 21. Land area output
# ============================================================

land_rows = []

for transaction_type, stat in (
    land_area_stats.items()
):

    land_rows.append(
        {
            "transaction_type": (
                transaction_type
            ),
            **stat,
        }
    )

land_df = pd.DataFrame(
    land_rows
)


# ============================================================
# 22. Category outputs
# ============================================================

category_summary_rows = []
category_top_rows = []

for column, counter in (
    category_stats.items()
):

    rows = category_rows[column]

    category_summary_rows.append(
        {
            "column_name": column,
            "rows": rows,
            "missing_count": (
                category_missing[column]
            ),
            "missing_rate": (
                category_missing[column]
                / rows
                if rows
                else None
            ),
            "unique_count": len(
                counter
            ),
        }
    )

    for rank, (
        value,
        count,
    ) in enumerate(
        counter.most_common(
            TOP_N
        ),
        start=1,
    ):

        category_top_rows.append(
            {
                "column_name": column,
                "rank": rank,
                "value": value,
                "row_count": count,
                "row_share": (
                    count / rows
                    if rows
                    else None
                ),
            }
        )


category_summary_df = pd.DataFrame(
    category_summary_rows
)

category_top_df = pd.DataFrame(
    category_top_rows
)


# ============================================================
# 23. Save
# ============================================================

no_df.to_csv(
    SUMMARY_DIR / "no_global_uniqueness.csv",
    index=False,
    encoding="utf-8-sig",
)

date_df.to_csv(
    SUMMARY_DIR / "date_complete_validation.csv",
    index=False,
    encoding="utf-8-sig",
)

date_detail_df.to_csv(
    SUMMARY_DIR / "date_complete_examples.csv",
    index=False,
    encoding="utf-8-sig",
)

note_df.to_csv(
    SUMMARY_DIR / "note_flag_consistency.csv",
    index=False,
    encoding="utf-8-sig",
)

area_df.to_csv(
    SUMMARY_DIR / "building_area_components_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

relationship_df.to_csv(
    SUMMARY_DIR / "building_area_relationships.csv",
    index=False,
    encoding="utf-8-sig",
)

land_df.to_csv(
    SUMMARY_DIR / "land_area_by_transaction_type.csv",
    index=False,
    encoding="utf-8-sig",
)

category_summary_df.to_csv(
    SUMMARY_DIR / "remaining_category_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

category_top_df.to_csv(
    SUMMARY_DIR / "remaining_category_top_values.csv",
    index=False,
    encoding="utf-8-sig",
)


# ============================================================
# 24. Print
# ============================================================

print("\nNO uniqueness:")
print(no_df.to_string(index=False))

print("\nCompletion date:")
print(date_df.to_string(index=False))

print("\nCompletion date examples:")
print(date_detail_df.to_string(index=False))

print("\nNote consistency:")
print(note_df.to_string(index=False))

print("\nBuilding area:")
print(area_df.to_string(index=False))

print("\nBuilding area relationships:")
print(relationship_df.to_string(index=False))

print("\nLand area:")
print(land_df.to_string(index=False))

print("\nRemaining categories:")
print(
    category_summary_df.to_string(
        index=False
    )
)

print("\nCategory top values:")
print(
    category_top_df.to_string(
        index=False
    )
)