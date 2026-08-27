"""Validate raw location mapping and six-municipality name transitions."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd


CHUNK_SIZE = 100_000
CANONICAL_START = 10108
CANONICAL_END = 11312
EXPECTED_RAW_ROWS = 4_380_208
EXPECTED_INVALID_PERIOD_ROWS = 34_519
EXPECTED_UNMAPPED_ROWS = 97
SIX_MUNICIPALITIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市"]


def normalize(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace("", pd.NA)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument(
        "--lookup",
        type=Path,
        default=root / "data" / "reference" / "location_lookup.csv",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=root / "data" / "reference" / "location_aliases.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            root
            / "profiling_output"
            / "summary"
            / "six_municipality_transition_validation.csv"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    lookup = pd.read_csv(args.lookup, dtype="string")
    aliases = pd.read_csv(args.aliases, dtype="string")

    city_counts_by_code = lookup.groupby("legacy_county_code")["city"].nunique()
    if not city_counts_by_code.eq(1).all():
        raise ValueError("a legacy county code maps to more than one canonical city")
    city_by_code = (
        lookup.drop_duplicates("legacy_county_code")
        .set_index("legacy_county_code")["city"]
        .to_dict()
    )

    total_rows = 0
    valid_period_rows = 0
    mapped_rows = 0
    unmapped_rows = 0
    unmapped_nonblank_town_rows = 0
    alias_hits: Counter[tuple[str, str, str]] = Counter()
    six_stats: Counter[tuple[str, str, str, bool, bool]] = Counter()

    with pd.read_stata(
        args.raw,
        columns=["trans_y", "trans_m", "countycd", "county", "town"],
        chunksize=CHUNK_SIZE,
        convert_categoricals=False,
        convert_dates=False,
        convert_missing=False,
        preserve_dtypes=True,
    ) as reader:
        for chunk_number, chunk in enumerate(reader, start=1):
            total_rows += len(chunk)
            year = pd.to_numeric(chunk["trans_y"], errors="coerce")
            month = pd.to_numeric(chunk["trans_m"], errors="coerce")
            period = year * 100 + month
            valid = year.notna() & month.between(1, 12) & period.between(
                CANONICAL_START,
                CANONICAL_END,
            )

            work = chunk.loc[valid, ["countycd", "county", "town"]].copy()
            valid_period_rows += len(work)
            for column in ["countycd", "county", "town"]:
                work[column] = normalize(work[column])
            work["normalized_town"] = work["town"]

            for alias in aliases.itertuples(index=False):
                mask = (
                    work["countycd"].eq(alias.legacy_county_code)
                    & work["town"].eq(alias.raw_town)
                )
                hits = int(mask.sum())
                if hits:
                    alias_hits[
                        (
                            alias.legacy_county_code,
                            alias.raw_town,
                            alias.normalized_town,
                        )
                    ] += hits
                    work.loc[mask, "normalized_town"] = alias.normalized_town

            mapped = work.merge(
                lookup,
                left_on=["countycd", "normalized_town"],
                right_on=["legacy_county_code", "district"],
                how="left",
                validate="many_to_one",
            )
            is_mapped = mapped["town_id"].notna()
            mapped_rows += int(is_mapped.sum())
            unmapped_rows += int((~is_mapped).sum())
            unmapped_nonblank_town_rows += int(
                ((~is_mapped) & mapped["normalized_town"].notna()).sum()
            )

            expected_city = mapped["countycd"].map(city_by_code)
            six_mask = expected_city.isin(SIX_MUNICIPALITIES)
            six_rows = mapped.loc[six_mask].copy()
            six_rows["canonical_city"] = expected_city.loc[six_mask]
            six_rows["raw_county_name"] = six_rows["county"].fillna("<BLANK>")
            six_rows["is_mapped"] = is_mapped.loc[six_mask].to_numpy()
            six_rows["unmapped_nonblank"] = (
                ~six_rows["is_mapped"] & six_rows["normalized_town"].notna()
            )

            grouped = six_rows.groupby(
                [
                    "canonical_city",
                    "countycd",
                    "raw_county_name",
                    "is_mapped",
                    "unmapped_nonblank",
                ],
                dropna=False,
            ).size()
            for key, count in grouped.items():
                six_stats[key] += int(count)

            print(
                f"chunk={chunk_number} total_rows={total_rows} "
                f"valid_period_rows={valid_period_rows}"
            )

    invalid_period_rows = total_rows - valid_period_rows
    if total_rows != EXPECTED_RAW_ROWS:
        raise ValueError(f"raw rows={total_rows}; expected {EXPECTED_RAW_ROWS}")
    if invalid_period_rows != EXPECTED_INVALID_PERIOD_ROWS:
        raise ValueError(
            f"invalid period rows={invalid_period_rows}; "
            f"expected {EXPECTED_INVALID_PERIOD_ROWS}"
        )
    if unmapped_rows != EXPECTED_UNMAPPED_ROWS:
        raise ValueError(
            f"unmapped rows={unmapped_rows}; expected {EXPECTED_UNMAPPED_ROWS}"
        )
    if unmapped_nonblank_town_rows:
        raise ValueError(
            f"unmapped rows with nonblank town={unmapped_nonblank_town_rows}"
        )

    detail_rows = []
    for (
        canonical_city,
        legacy_county_code,
        raw_county_name,
        is_mapped,
        unmapped_nonblank,
    ), count in six_stats.items():
        detail_rows.append(
            {
                "canonical_city": canonical_city,
                "legacy_county_code": legacy_county_code,
                "raw_county_name": raw_county_name,
                "transaction_rows": count,
                "mapped_rows": count if is_mapped else 0,
                "unmapped_rows": 0 if is_mapped else count,
                "unmapped_nonblank_town_rows": count if unmapped_nonblank else 0,
            }
        )

    report = pd.DataFrame(detail_rows)
    report = (
        report.groupby(
            ["canonical_city", "legacy_county_code", "raw_county_name"],
            as_index=False,
        )[
            [
                "transaction_rows",
                "mapped_rows",
                "unmapped_rows",
                "unmapped_nonblank_town_rows",
            ]
        ]
        .sum()
    )
    report["historical_name"] = report["raw_county_name"].ne(
        report["canonical_city"]
    )
    report["validation_status"] = report["unmapped_nonblank_town_rows"].map(
        lambda value: "PASS" if value == 0 else "FAIL"
    )
    report["canonical_city"] = pd.Categorical(
        report["canonical_city"],
        categories=SIX_MUNICIPALITIES,
        ordered=True,
    )
    report = report.sort_values(
        ["canonical_city", "legacy_county_code", "raw_county_name"],
        kind="stable",
    )

    if set(report["canonical_city"].astype("string")) != set(SIX_MUNICIPALITIES):
        raise ValueError("one or more six municipalities are absent from raw data")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(args.output, index=False, encoding="utf-8", lineterminator="\n")

    print(f"raw_rows={total_rows}")
    print(f"invalid_period_rows={invalid_period_rows}")
    print(f"mapped_rows={mapped_rows}")
    print(f"unmapped_rows={unmapped_rows}")
    print(f"unmapped_nonblank_town_rows={unmapped_nonblank_town_rows}")
    print(f"alias_hits={dict(alias_hits)}")
    print(f"six_municipality_output={args.output}")


if __name__ == "__main__":
    main()
