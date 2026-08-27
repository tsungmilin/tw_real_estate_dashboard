"""Build the versioned district lookup from the two approved source files."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import pandas as pd


EXPECTED_DISTRICTS = 368
EXPECTED_SIX_MUNICIPALITY_DISTRICTS = {
    "臺北市": 12,
    "新北市": 29,
    "桃園市": 13,
    "臺中市": 29,
    "臺南市": 37,
    "高雄市": 38,
}

TOWN_COLUMNS = ["hsn_nm", "town_nm", "hsn_cd", "town_cd"]
EXCEL_COLUMNS = [
    "county_id",
    "town_id",
    "hsn_nm",
    "town_nm",
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        result[column] = (
            result[column]
            .astype("string")
            .str.strip()
            .replace("", pd.NA)
        )
    return result


def require_columns(frame: pd.DataFrame, required: list[str], label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{label} missing columns: {missing}")


def require_unique(frame: pd.DataFrame, columns: list[str], label: str) -> None:
    duplicates = frame[frame.duplicated(columns, keep=False)]
    if not duplicates.empty:
        raise ValueError(
            f"{label} is not unique on {columns}; duplicate rows={len(duplicates)}"
        )


def build_lookup(
    town_source: Path,
    official_id_source: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    town = pd.read_stata(
        town_source,
        convert_categoricals=False,
        convert_dates=False,
        preserve_dtypes=True,
    )
    require_columns(town, TOWN_COLUMNS, "town source")
    town = normalize_text(town[TOWN_COLUMNS], TOWN_COLUMNS)

    if len(town) != EXPECTED_DISTRICTS:
        raise ValueError(f"town source rows={len(town)}; expected {EXPECTED_DISTRICTS}")
    require_unique(town, ["hsn_cd", "town_cd"], "town source legacy codes")
    require_unique(town, ["hsn_cd", "town_nm"], "town source names")

    excel = pd.read_excel(
        official_id_source,
        usecols=EXCEL_COLUMNS,
        dtype=str,
        engine="openpyxl",
    )
    require_columns(excel, EXCEL_COLUMNS, "official ID source")
    excel = normalize_text(excel, EXCEL_COLUMNS)

    districts = excel[EXCEL_COLUMNS].drop_duplicates()
    if len(districts) != EXPECTED_DISTRICTS:
        raise ValueError(
            f"crosswalk district rows={len(districts)}; expected {EXPECTED_DISTRICTS}"
        )

    if not districts["county_id"].str.fullmatch(r"\d{5}", na=False).all():
        raise ValueError("county_id must be a five-character numeric string")
    if not districts["town_id"].str.fullmatch(r"\d{8}", na=False).all():
        raise ValueError("town_id must be an eight-character numeric string")

    require_unique(districts, ["county_id", "town_id"], "official location IDs")
    require_unique(districts, ["hsn_nm", "town_nm"], "crosswalk location names")

    merged = town.merge(
        districts,
        on=["hsn_nm", "town_nm"],
        how="left",
        validate="one_to_one",
        indicator=True,
    )
    if not merged["_merge"].eq("both").all():
        missing = merged.loc[
            merged["_merge"] != "both",
            ["hsn_nm", "town_nm"],
        ]
        raise ValueError(f"town/crosswalk reconciliation failed:\n{missing}")

    lookup = merged.rename(
        columns={
            "hsn_cd": "legacy_county_code",
            "town_cd": "legacy_town_code",
            "hsn_nm": "city",
            "town_nm": "district",
        }
    )[
        [
            "legacy_county_code",
            "legacy_town_code",
            "county_id",
            "town_id",
            "city",
            "district",
        ]
    ].sort_values(["county_id", "town_id"], kind="stable").reset_index(drop=True)

    require_unique(
        lookup,
        ["legacy_county_code", "district"],
        "raw location join key",
    )
    require_unique(lookup, ["county_id", "town_id"], "canonical location IDs")

    counts = lookup.groupby("city").size().to_dict()
    actual_six = {
        city: int(counts.get(city, 0))
        for city in EXPECTED_SIX_MUNICIPALITY_DISTRICTS
    }
    if actual_six != EXPECTED_SIX_MUNICIPALITY_DISTRICTS:
        raise ValueError(
            "six-municipality district counts differ: "
            f"actual={actual_six}, expected={EXPECTED_SIX_MUNICIPALITY_DISTRICTS}"
        )

    aliases = pd.DataFrame(
        [
            ("K", "頭份鎮", "頭份市"),
            ("N", "員林鎮", "員林市"),
            ("Q", "阿里山", "阿里山鄉"),
        ],
        columns=["legacy_county_code", "raw_town", "normalized_town"],
    )
    require_unique(
        aliases,
        ["legacy_county_code", "raw_town"],
        "location aliases",
    )

    alias_targets = aliases.merge(
        lookup,
        left_on=["legacy_county_code", "normalized_town"],
        right_on=["legacy_county_code", "district"],
        how="left",
        validate="many_to_one",
    )
    if alias_targets["town_id"].isna().any():
        raise ValueError("one or more aliases do not resolve to the canonical lookup")

    return lookup, aliases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--town-source", required=True, type=Path)
    parser.add_argument("--official-id-source", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data" / "reference",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    lookup, aliases = build_lookup(args.town_source, args.official_id_source)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    lookup_path = args.output_dir / "location_lookup.csv"
    aliases_path = args.output_dir / "location_aliases.csv"
    lookup.to_csv(lookup_path, index=False, encoding="utf-8", lineterminator="\n")
    aliases.to_csv(aliases_path, index=False, encoding="utf-8", lineterminator="\n")

    print(f"town_source_sha256={file_sha256(args.town_source)}")
    print(f"official_id_source_sha256={file_sha256(args.official_id_source)}")
    print(f"lookup_rows={len(lookup)} aliases={len(aliases)}")
    print(f"lookup_output={lookup_path}")
    print(f"aliases_output={aliases_path}")


if __name__ == "__main__":
    main()
