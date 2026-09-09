from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from src.cleaning.contract import CLEAN_SCHEMA, EXCLUDED_SCHEMA
from src.cleaning.pipeline import CleaningConfig, run_cleaning


ROOT = Path(__file__).resolve().parents[1]


def _base_row(source_id: str) -> dict[str, object]:
    return {
        "no": source_id,
        "trans_y": 112,
        "trans_m": 1,
        "trans_d": 15,
        "county": "連江縣",
        "countycd": "Z",
        "town": "南竿鄉",
        "type": "房地(土地+建物)",
        "trans_landsize": 50.0,
        "usage_type": "住",
        "nonurban_type": "",
        "building_type": "住宅大樓(11層含以上有電梯)",
        "usage": "住家用",
        "date_complete": "1000101",
        "trans_size": 100.0,
        "room": 3,
        "hall": 2,
        "bath": 2,
        "manage": "有",
        "housing_totprice": 10_000_000.0,
        "h_price_m2": 100_000.0,
        "parking_size": 0.0,
        "parking_price": 0,
        "note": "",
        "date_trans": "1120115",
    }


def _synthetic_rows() -> list[dict[str, object]]:
    rows = [_base_row("VALID")]

    bad_day = _base_row("BAD_DAY")
    bad_day.update({"trans_m": 2, "trans_d": 30, "housing_totprice": 0.0})
    rows.append(bad_day)

    land = _base_row("LAND")
    land.update({"type": "土地", "date_complete": "", "note": "有備註"})
    rows.append(land)

    alias = _base_row("ALIAS")
    alias.update(
        {
            "county": "苗栗縣",
            "countycd": "K",
            "town": "頭份鎮",
            "type": "土地",
            "date_complete": "",
        }
    )
    rows.append(alias)

    parking = _base_row("PARKING")
    parking.update(
        {
            "type": "房地(土地+建物)+車位",
            "parking_size": 10.0,
            "parking_price": 500_000,
        }
    )
    rows.append(parking)

    rows[0]["h_price_m2"] = 100_000.25

    exact = _base_row("EXACT")
    rows.extend([exact.copy(), exact.copy()])

    conflict_a = _base_row("CONFLICT")
    conflict_b = conflict_a.copy()
    conflict_b["housing_totprice"] = 12_000_000.0
    rows.extend([conflict_a, conflict_b])

    missing = _base_row("")
    missing["trans_y"] = 100
    rows.append(missing)

    invalid_period = _base_row("INVALID_PERIOD")
    invalid_period["trans_y"] = 100
    rows.append(invalid_period)

    unmapped = _base_row("UNMAPPED")
    unmapped["town"] = ""
    rows.append(unmapped)

    excluded_type = _base_row("EXCLUDED_TYPE")
    excluded_type["type"] = "車位"
    rows.append(excluded_type)
    return rows


def test_full_cleaning_pipeline_on_synthetic_stata(tmp_path: Path) -> None:
    raw_path = tmp_path / "synthetic.dta"
    pd.DataFrame(_synthetic_rows()).to_stata(
        raw_path,
        write_index=False,
        version=118,
    )
    output_dir = tmp_path / "processed"
    audit_dir = tmp_path / "audit"

    result = run_cleaning(
        CleaningConfig(
            raw_path=raw_path,
            lookup_path=ROOT / "data" / "reference" / "location_lookup.csv",
            aliases_path=ROOT / "data" / "reference" / "location_aliases.csv",
            output_dir=output_dir,
            audit_dir=audit_dir,
            chunk_size=3,
        )
    )

    assert result.input_rows == 13
    assert result.clean_rows == 6
    assert result.excluded_rows == 7
    assert pq.ParquetFile(result.clean_path).schema_arrow.equals(
        CLEAN_SCHEMA,
        check_metadata=False,
    )
    assert pq.ParquetFile(result.excluded_path).schema_arrow.equals(
        EXCLUDED_SCHEMA,
        check_metadata=False,
    )

    clean = pd.read_parquet(result.clean_path)
    excluded = pd.read_parquet(result.excluded_path)
    assert clean["source_transaction_id"].is_unique
    assert set(clean["source_transaction_id"]) == {
        "VALID",
        "BAD_DAY",
        "LAND",
        "ALIAS",
        "PARKING",
        "EXACT",
    }
    assert excluded["primary_exclusion_reason"].value_counts().to_dict() == {
        "duplicate_conflict": 2,
        "missing_source_transaction_id": 1,
        "duplicate_exact": 1,
        "invalid_transaction_period": 1,
        "unmapped_location": 1,
        "excluded_transaction_type": 1,
    }

    by_id = clean.set_index("source_transaction_id")
    assert not bool(by_id.loc["BAD_DAY", "transaction_date_valid"])
    assert pd.isna(by_id.loc["BAD_DAY", "transaction_date"])
    assert not bool(by_id.loc["BAD_DAY", "total_price_valid"])
    assert pd.isna(by_id.loc["BAD_DAY", "total_price_ntd"])
    assert by_id.loc["ALIAS", "district"] == "頭份市"
    assert by_id.loc["ALIAS", "city"] == "苗栗縣"
    assert pd.isna(by_id.loc["LAND", "building_type"])
    assert pd.isna(by_id.loc["LAND", "building_transfer_area_valid"])
    assert by_id.loc["LAND", "completion_date_status"] == "not_applicable"
    assert bool(by_id.loc["PARKING", "parking_data_complete"])
    assert by_id.loc["VALID", "unit_price_ntd_m2"] == 100_000.25
    assert bool(by_id.loc["VALID", "unit_price_valid"])

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "success"
    assert audit["current_source_baseline"]["applies"] is False
    assert audit["publication_gates"]["row_counts_reconciled"] is True
    assert audit["results"]["row_counts"] == {
        "clean_rows": 6,
        "excluded_rows": 7,
        "input_rows": 13,
        "reconciled": True,
    }


def test_missing_required_column_writes_failed_audit(tmp_path: Path) -> None:
    raw_path = tmp_path / "missing-column.dta"
    frame = pd.DataFrame(_synthetic_rows()).drop(columns=["town"])
    frame.to_stata(raw_path, write_index=False, version=118)
    audit_dir = tmp_path / "audit"

    try:
        run_cleaning(
            CleaningConfig(
                raw_path=raw_path,
                lookup_path=ROOT / "data" / "reference" / "location_lookup.csv",
                aliases_path=ROOT / "data" / "reference" / "location_aliases.csv",
                output_dir=tmp_path / "processed",
                audit_dir=audit_dir,
                chunk_size=3,
            )
        )
    except ValueError as error:
        assert "missing required columns" in str(error)
    else:
        raise AssertionError("missing required raw column should fail")

    audits = list(audit_dir.glob("cleaning_run_*.json"))
    assert len(audits) == 1
    payload = json.loads(audits[0].read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"]["type"] == "ValueError"
