from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "data" / "reference"
SUMMARY_DIR = ROOT / "profiling_output" / "summary"


def test_location_lookup_contract() -> None:
    lookup = pd.read_csv(REFERENCE_DIR / "location_lookup.csv", dtype="string")

    assert list(lookup.columns) == [
        "legacy_county_code",
        "legacy_town_code",
        "county_id",
        "town_id",
        "city",
        "district",
    ]
    assert len(lookup) == 368
    assert not lookup.isna().any().any()
    assert lookup["county_id"].str.fullmatch(r"\d{5}").all()
    assert lookup["town_id"].str.fullmatch(r"\d{8}").all()
    assert lookup["county_id"].nunique() == 22
    assert lookup["city"].nunique() == 22
    assert len(lookup[["county_id", "city"]].drop_duplicates()) == 22
    assert not lookup.duplicated(["legacy_county_code", "district"]).any()
    assert not lookup.duplicated(["county_id", "town_id"]).any()


def test_location_aliases_resolve() -> None:
    lookup = pd.read_csv(REFERENCE_DIR / "location_lookup.csv", dtype="string")
    aliases = pd.read_csv(REFERENCE_DIR / "location_aliases.csv", dtype="string")

    assert len(aliases) == 3
    assert not aliases.duplicated(["legacy_county_code", "raw_town"]).any()
    resolved = aliases.merge(
        lookup,
        left_on=["legacy_county_code", "normalized_town"],
        right_on=["legacy_county_code", "district"],
        how="left",
        validate="many_to_one",
    )
    assert resolved["town_id"].notna().all()


def test_six_municipality_validation_passes() -> None:
    report = pd.read_csv(
        SUMMARY_DIR / "six_municipality_transition_validation.csv",
        dtype="string",
    )
    expected = {"臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市"}

    assert set(report["canonical_city"]) == expected
    assert report["validation_status"].eq("PASS").all()
    assert pd.to_numeric(report["unmapped_nonblank_town_rows"]).eq(0).all()
