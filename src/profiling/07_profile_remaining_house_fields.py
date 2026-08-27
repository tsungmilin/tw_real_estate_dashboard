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
    "room",
    "hall",
    "bath",
    "partition",
    "elevator",
    "date_complete",
    "building_type",
    "usage",
    "note_yn",
    "note",
]

HOUSE_TYPES = {
    "房地(土地+建物)",
    "房地(土地+建物)+車位",
}

KEEP_TYPES = HOUSE_TYPES | {"土地"}

NUMERIC_COLUMNS = [
    "room",
    "hall",
    "bath",
]

CATEGORY_COLUMNS = [
    "partition",
    "elevator",
    "building_type",
    "usage",
    "note_yn",
]

CHUNK_SIZE = 100_000
TOP_N = 30


# ============================================================
# 3. Statistics containers
# ============================================================

numeric_stats = {
    column: {
        "rows": 0,
        "missing": 0,
        "zero": 0,
        "negative": 0,
        "min": None,
        "max": None,
        "value_counts": Counter(),
    }
    for column in NUMERIC_COLUMNS
}

category_stats = {
    column: {
        "rows": 0,
        "pandas_na": 0,
        "blank": 0,
        "value_counts": Counter(),
    }
    for column in CATEGORY_COLUMNS
}


# ============================================================
# 4. date_complete statistics
# ============================================================
#
# 先不急著 parse 成日期。
#
# 這次只確認：
# - missing
# - zero
# - min / max
# - raw digit length
#
# 跑完後再根據實際格式決定 conversion rule。

date_stats = {
    "rows": 0,
    "missing": 0,
    "zero": 0,
    "negative": 0,
    "min": None,
    "max": None,
    "digit_length_counts": Counter(),
}


# ============================================================
# 5. note statistics
# ============================================================
#
# note 是 free text。
#
# 不把所有 unique note 存進 Counter，
# 避免大量不同文字占 RAM。
#
# 先回答兩件事情：
#
# 1. 有備註的比例到底多少？
# 2. 備註大概涉及哪些常見特殊情況？
#
# 以下 keyword classification 只是 profiling heuristic，
# 不是最後 cleaning rule。

note_stats = {
    "rows": 0,
    "blank": 0,
    "nonblank": 0,
}

NOTE_PATTERNS = {
    "related_party": (
        r"親友|員工|共有人|特殊關係"
    ),
    "renovation_or_furniture": (
        r"裝潢|裝修|家具|家電"
    ),
    "addition_or_unregistered": (
        r"增建|未登記|頂樓加蓋|陽台外推|夾層"
    ),
    "tenancy": (
        r"租約|租賃"
    ),
    "distressed_or_defect": (
        r"急買|急賣|瑕疵"
    ),
    "redevelopment": (
        r"都更|重建"
    ),
}

note_pattern_counts = Counter()


# ============================================================
# 6. Helper
# ============================================================

def normalize_category(value):
    """
    Profiling 用 normalization。

    不修改 raw source，
    只把 missing / blank 統一成容易辨識的文字。
    """

    if pd.isna(value):
        return "<PANDAS_NA>"

    if isinstance(value, str) and value.strip() == "":
        return "<BLANK_STRING>"

    return str(value).strip()


# ============================================================
# 7. Read .dta in chunks
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

        print(f"Processing chunk {chunk_number:,}")

        # ====================================================
        # 8. House subset
        # ====================================================
        #
        # room / hall / bath / elevator 等建物特徵
        # 只在房地交易中判斷。
        #
        # 純土地沒有這些資料是正常的，
        # 不應拿來計算 missing rate。

        house = chunk[
            chunk["type"].isin(HOUSE_TYPES)
        ].copy()


        # ====================================================
        # 9. room / hall / bath
        # ====================================================

        for column in NUMERIC_COLUMNS:

            series = pd.to_numeric(
                house[column],
                errors="coerce",
            )

            numeric_stats[column]["rows"] += len(series)

            numeric_stats[column]["missing"] += int(
                series.isna().sum()
            )

            valid = series.dropna()

            numeric_stats[column]["zero"] += int(
                (valid == 0).sum()
            )

            numeric_stats[column]["negative"] += int(
                (valid < 0).sum()
            )

            if not valid.empty:

                current_min = valid.min()
                current_max = valid.max()

                if numeric_stats[column]["min"] is None:
                    numeric_stats[column]["min"] = current_min
                    numeric_stats[column]["max"] = current_max

                else:
                    numeric_stats[column]["min"] = min(
                        numeric_stats[column]["min"],
                        current_min,
                    )

                    numeric_stats[column]["max"] = max(
                        numeric_stats[column]["max"],
                        current_max,
                    )

            # 格局一般 unique values 不會很多，
            # 所以可以做 exact full-data frequency。
            numeric_stats[column]["value_counts"].update(
                valid.astype(str).tolist()
            )


        # ====================================================
        # 10. categorical building fields
        # ====================================================

        for column in CATEGORY_COLUMNS:

            series = house[column]

            category_stats[column]["rows"] += len(series)

            category_stats[column]["pandas_na"] += int(
                series.isna().sum()
            )

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

                category_stats[column]["blank"] += int(
                    blank_mask.sum()
                )

            normalized = series.map(
                normalize_category
            )

            category_stats[column][
                "value_counts"
            ].update(
                normalized.tolist()
            )


        # ====================================================
        # 11. date_complete
        # ====================================================

        date_series = pd.to_numeric(
            house["date_complete"],
            errors="coerce",
        )

        date_stats["rows"] += len(date_series)

        date_stats["missing"] += int(
            date_series.isna().sum()
        )

        valid_dates = date_series.dropna()

        date_stats["zero"] += int(
            (valid_dates == 0).sum()
        )

        date_stats["negative"] += int(
            (valid_dates < 0).sum()
        )

        positive_dates = valid_dates[
            valid_dates > 0
        ]

        if not positive_dates.empty:

            current_min = positive_dates.min()
            current_max = positive_dates.max()

            if date_stats["min"] is None:
                date_stats["min"] = current_min
                date_stats["max"] = current_max

            else:
                date_stats["min"] = min(
                    date_stats["min"],
                    current_min,
                )

                date_stats["max"] = max(
                    date_stats["max"],
                    current_max,
                )


            # ------------------------------------------------
            # 看 raw value 有幾位數
            #
            # 例如：
            #
            # 70101   → 5 digits
            # 1010101 → 7 digits
            #
            # 先知道資料長什麼樣，再決定 parser。
            # ------------------------------------------------

            integer_dates = (
                positive_dates
                .round()
                .astype("int64")
            )

            digit_lengths = (
                integer_dates
                .astype(str)
                .str.len()
            )

            date_stats[
                "digit_length_counts"
            ].update(
                digit_lengths.tolist()
            )


        # ====================================================
        # 12. note
        # ====================================================
        #
        # note 對 canonical 保留的三種 transaction types
        # 都可能有意義，因此這裡不像格局只看房地。

        note_subset = chunk[
            chunk["type"].isin(KEEP_TYPES)
        ]["note"]

        note_text = (
            note_subset
            .fillna("")
            .astype(str)
            .str.strip()
        )

        note_stats["rows"] += len(note_text)

        blank_mask = note_text.eq("")

        note_stats["blank"] += int(
            blank_mask.sum()
        )

        note_stats["nonblank"] += int(
            (~blank_mask).sum()
        )


        # ----------------------------------------------------
        # Keyword profiling
        # ----------------------------------------------------

        nonblank_notes = note_text[
            ~blank_mask
        ]

        for category, pattern in NOTE_PATTERNS.items():

            matches = nonblank_notes.str.contains(
                pattern,
                regex=True,
                na=False,
            )

            note_pattern_counts[category] += int(
                matches.sum()
            )


# ============================================================
# 13. Numeric summary
# ============================================================

numeric_rows = []

for column, values in numeric_stats.items():

    rows = values["rows"]

    numeric_rows.append(
        {
            "column_name": column,
            "rows": rows,
            "missing_count": values["missing"],
            "missing_rate": (
                values["missing"] / rows
                if rows
                else None
            ),
            "zero_count": values["zero"],
            "zero_rate": (
                values["zero"] / rows
                if rows
                else None
            ),
            "negative_count": values["negative"],
            "min": values["min"],
            "max": values["max"],
            "unique_count": len(
                values["value_counts"]
            ),
        }
    )

numeric_df = pd.DataFrame(
    numeric_rows
)


# ============================================================
# 14. Top values
# ============================================================

top_rows = []


for column, values in numeric_stats.items():

    for rank, (value, count) in enumerate(
        values["value_counts"].most_common(TOP_N),
        start=1,
    ):

        top_rows.append(
            {
                "column_name": column,
                "rank": rank,
                "value": value,
                "row_count": count,
                "row_share": (
                    count / values["rows"]
                ),
            }
        )


for column, values in category_stats.items():

    for rank, (value, count) in enumerate(
        values["value_counts"].most_common(TOP_N),
        start=1,
    ):

        top_rows.append(
            {
                "column_name": column,
                "rank": rank,
                "value": value,
                "row_count": count,
                "row_share": (
                    count / values["rows"]
                ),
            }
        )


top_df = pd.DataFrame(
    top_rows
)


# ============================================================
# 15. Categorical summary
# ============================================================

category_rows = []

for column, values in category_stats.items():

    rows = values["rows"]

    category_rows.append(
        {
            "column_name": column,
            "rows": rows,
            "pandas_na_count": values["pandas_na"],
            "blank_count": values["blank"],
            "missing_or_blank_rate": (
                (
                    values["pandas_na"]
                    + values["blank"]
                )
                / rows
                if rows
                else None
            ),
            "unique_count": len(
                values["value_counts"]
            ),
        }
    )

category_df = pd.DataFrame(
    category_rows
)


# ============================================================
# 16. date_complete summary
# ============================================================

date_summary_df = pd.DataFrame(
    [
        {
            "rows": date_stats["rows"],
            "missing_count": date_stats["missing"],
            "missing_rate": (
                date_stats["missing"]
                / date_stats["rows"]
            ),
            "zero_count": date_stats["zero"],
            "negative_count": date_stats["negative"],
            "min": date_stats["min"],
            "max": date_stats["max"],
        }
    ]
)


date_length_rows = []

for digit_length, count in sorted(
    date_stats["digit_length_counts"].items()
):

    date_length_rows.append(
        {
            "digit_length": digit_length,
            "row_count": count,
            "row_share": (
                count / date_stats["rows"]
            ),
        }
    )

date_length_df = pd.DataFrame(
    date_length_rows
)


# ============================================================
# 17. note summary
# ============================================================

note_df = pd.DataFrame(
    [
        {
            "rows": note_stats["rows"],
            "blank_count": note_stats["blank"],
            "nonblank_count": note_stats["nonblank"],
            "nonblank_rate": (
                note_stats["nonblank"]
                / note_stats["rows"]
            ),
        }
    ]
)


note_pattern_rows = []

for category, count in note_pattern_counts.items():

    note_pattern_rows.append(
        {
            "note_category": category,
            "matched_rows": count,
            "share_of_nonblank_notes": (
                count / note_stats["nonblank"]
                if note_stats["nonblank"]
                else None
            ),
        }
    )

note_pattern_df = pd.DataFrame(
    note_pattern_rows
)


# ============================================================
# 18. Save outputs
# ============================================================

numeric_df.to_csv(
    OUTPUT_DIR / "remaining_house_numeric_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

category_df.to_csv(
    OUTPUT_DIR / "remaining_house_category_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

top_df.to_csv(
    OUTPUT_DIR / "remaining_house_top_values.csv",
    index=False,
    encoding="utf-8-sig",
)

date_summary_df.to_csv(
    OUTPUT_DIR / "date_complete_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

date_length_df.to_csv(
    OUTPUT_DIR / "date_complete_digit_lengths.csv",
    index=False,
    encoding="utf-8-sig",
)

note_df.to_csv(
    OUTPUT_DIR / "note_summary.csv",
    index=False,
    encoding="utf-8-sig",
)

note_pattern_df.to_csv(
    OUTPUT_DIR / "note_pattern_summary.csv",
    index=False,
    encoding="utf-8-sig",
)


# ============================================================
# 19. Print
# ============================================================

print("\nNumeric fields:")
print(numeric_df.to_string(index=False))

print("\nCategorical fields:")
print(category_df.to_string(index=False))

print("\nTop values:")
print(top_df.to_string(index=False))

print("\nCompletion date:")
print(date_summary_df.to_string(index=False))

print("\nCompletion date digit lengths:")
print(date_length_df.to_string(index=False))

print("\nNote:")
print(note_df.to_string(index=False))

print("\nNote keyword patterns:")
print(note_pattern_df.to_string(index=False))