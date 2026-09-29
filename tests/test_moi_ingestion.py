from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from src.cleaning.contract import REQUIRED_RAW_COLUMNS
from src.cleaning.pipeline import CleaningConfig, run_cleaning
from src.ingestion.moi_transactions import (
    inspect_moi_zip,
    iter_moi_transaction_chunks,
)


ROOT = Path(__file__).resolve().parents[1]

MOI_HEADERS = [
    "鄉鎮市區",
    "交易標的",
    "土地位置建物門牌",
    "土地移轉總面積平方公尺",
    "都市土地使用分區",
    "非都市土地使用分區",
    "非都市土地使用編定",
    "交易年月日",
    "交易筆棟數",
    "移轉層次",
    "總樓層數",
    "建物型態",
    "主要用途",
    "主要建材",
    "建築完成年月",
    "建物移轉總面積平方公尺",
    "建物現況格局-房",
    "建物現況格局-廳",
    "建物現況格局-衛",
    "建物現況格局-隔間",
    "有無管理組織",
    "總價元",
    "單價元平方公尺",
    "車位類別",
    "車位移轉總面積平方公尺",
    "車位總價元",
    "備註",
    "編號",
    "主建物面積",
    "附屬建物面積",
    "陽台面積",
    "電梯",
    "移轉編號",
]


def _row(source_id: str, town: str) -> dict[str, str]:
    return {
        "鄉鎮市區": town,
        "交易標的": "房地(土地+建物)",
        "土地位置建物門牌": f"{town}測試路1號",
        "土地移轉總面積平方公尺": "50",
        "都市土地使用分區": "住宅區",
        "非都市土地使用分區": "",
        "非都市土地使用編定": "",
        "交易年月日": "1140102",
        "交易筆棟數": "土地1建物1車位0",
        "移轉層次": "一層",
        "總樓層數": "五層",
        "建物型態": "公寓(5樓含以下無電梯)",
        "主要用途": "住家用",
        "主要建材": "鋼筋混凝土造",
        "建築完成年月": "1000101",
        "建物移轉總面積平方公尺": "100",
        "建物現況格局-房": "3",
        "建物現況格局-廳": "2",
        "建物現況格局-衛": "2",
        "建物現況格局-隔間": "有",
        "有無管理組織": "無",
        "總價元": "10000000",
        "單價元平方公尺": "100000",
        "車位類別": "",
        "車位移轉總面積平方公尺": "0",
        "車位總價元": "0",
        "備註": "",
        "編號": source_id,
        "主建物面積": "90",
        "附屬建物面積": "10",
        "陽台面積": "5",
        "電梯": "無",
        "移轉編號": "",
    }


def _csv_bytes(
    rows: list[dict[str, str]],
    *,
    old_parking_header: bool = False,
) -> bytes:
    headers = MOI_HEADERS.copy()
    if old_parking_header:
        index = headers.index("車位移轉總面積平方公尺")
        headers[index] = "車位移轉總面積(平方公尺)"

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerow(["English field description"] * len(headers))
    for row in rows:
        writer.writerow(
            [
                row.get(
                    "車位移轉總面積平方公尺"
                    if header == "車位移轉總面積(平方公尺)"
                    else header,
                    "",
                )
                for header in headers
            ]
        )
    return output.getvalue().encode("utf-8-sig")


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("i_lvr_land_a.csv", _csv_bytes([_row("CHIAYI", "嘉義市")]))
        archive.writestr(
            "o_lvr_land_a.csv",
            _csv_bytes([_row("HSINCHU", "新竹市")], old_parking_header=True),
        )
        archive.writestr("i_lvr_land_a_build.csv", b"ignored")


def test_moi_zip_adapter_uses_main_files_and_dta_compatibility_towns(
    tmp_path: Path,
) -> None:
    source = tmp_path / "moi.zip"
    _write_zip(source)

    inspection = inspect_moi_zip(source)
    assert inspection.transaction_files == (
        "i_lvr_land_a.csv",
        "o_lvr_land_a.csv",
    )
    assert inspection.temporary_location_rules == (
        "I:嘉義市:東區",
        "O:新竹市:東區",
    )

    chunks = list(iter_moi_transaction_chunks(source, chunk_size=1))
    adapted = pd.concat(chunks, ignore_index=True)
    assert tuple(adapted.columns) == REQUIRED_RAW_COLUMNS
    assert adapted["no"].tolist() == ["CHIAYI", "HSINCHU"]
    assert adapted["countycd"].tolist() == ["I", "O"]
    assert adapted["county"].tolist() == ["嘉義市", "新竹市"]
    assert adapted["town"].tolist() == ["東區", "東區"]
    assert adapted["trans_y"].tolist() == [114, 114]
    assert adapted["trans_m"].tolist() == [1, 1]
    assert adapted["trans_d"].tolist() == [2, 2]
    assert adapted["usage_type"].tolist() == ["住", "住"]


def test_moi_cleaning_requires_explicit_period_end(tmp_path: Path) -> None:
    source = tmp_path / "moi.zip"
    _write_zip(source)

    with pytest.raises(ValueError, match="period_end is required"):
        run_cleaning(
            CleaningConfig(
                raw_path=source,
                lookup_path=ROOT / "data" / "reference" / "location_lookup.csv",
                aliases_path=ROOT / "data" / "reference" / "location_aliases.csv",
                output_dir=tmp_path / "processed",
                audit_dir=tmp_path / "audit",
            )
        )


def test_cleaning_pipeline_accepts_moi_zip_and_records_temporary_mapping(
    tmp_path: Path,
) -> None:
    source = tmp_path / "moi.zip"
    _write_zip(source)

    result = run_cleaning(
        CleaningConfig(
            raw_path=source,
            lookup_path=ROOT / "data" / "reference" / "location_lookup.csv",
            aliases_path=ROOT / "data" / "reference" / "location_aliases.csv",
            output_dir=tmp_path / "processed",
            audit_dir=tmp_path / "audit",
            chunk_size=1,
            period_end=11412,
            source_release_id="2025-12-21",
        )
    )

    clean = pd.read_parquet(result.clean_path).set_index("source_transaction_id")
    assert clean.loc["CHIAYI", "city"] == "嘉義市"
    assert clean.loc["CHIAYI", "district"] == "東區"
    assert clean.loc["HSINCHU", "city"] == "新竹市"
    assert clean.loc["HSINCHU", "district"] == "東區"

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["inputs"]["raw"]["format"] == "moi_zip"
    assert audit["configuration"]["period_end"] == 11412
    assert audit["configuration"]["source_release_id"] == "2025-12-21"
    checks = audit["results"]["independent_checks"]
    assert checks["temporary_dta_compatibility_mapping:I:嘉義市:東區"] == 1
    assert checks["temporary_dta_compatibility_mapping:O:新竹市:東區"] == 1


def test_moi_taitung_alias_resolves_to_official_location(tmp_path: Path) -> None:
    source = tmp_path / "moi.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "v_lvr_land_a.csv",
            _csv_bytes([_row("TAITUNG", "台東市")]),
        )

    result = run_cleaning(
        CleaningConfig(
            raw_path=source,
            lookup_path=ROOT / "data" / "reference" / "location_lookup.csv",
            aliases_path=ROOT / "data" / "reference" / "location_aliases.csv",
            output_dir=tmp_path / "processed",
            audit_dir=tmp_path / "audit",
            period_end=11412,
        )
    )

    clean = pd.read_parquet(result.clean_path)
    assert clean.loc[0, "city"] == "臺東縣"
    assert clean.loc[0, "district"] == "臺東市"
