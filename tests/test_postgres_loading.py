from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import src.loading.postgres_loader as postgres_loader
from src.cleaning.contract import CLEAN_SCHEMA
from src.loading.postgres_loader import (
    BUSINESS_COLUMNS,
    CORE_IMPACT_COLUMNS,
    LoadConfig,
    LoadResult,
    ParquetSnapshot,
    ROOT,
    build_sync_sql,
    inspect_clean_parquet,
    load_postgres_staging,
)


def _clean_row() -> dict[str, object]:
    return {
        "cleaning_run_id": "cleaning-test-run",
        "source_transaction_id": "TEST-1",
        "transaction_year": 2024,
        "transaction_month": 1,
        "transaction_date": date(2024, 1, 15),
        "transaction_date_valid": True,
        "completion_date": None,
        "completion_date_status": "not_applicable",
        "completion_after_transaction": None,
        "county_id": "09007",
        "town_id": "09007010",
        "city": "連江縣",
        "district": "南竿鄉",
        "transaction_type": "土地",
        "urban_land_use_type": None,
        "nonurban_land_use_zone": None,
        "building_type": None,
        "primary_use": None,
        "has_management": None,
        "land_transfer_area_m2": 50.0,
        "building_transfer_area_m2": None,
        "room_count": None,
        "hall_count": None,
        "bathroom_count": None,
        "total_price_ntd": 1_000_000,
        "unit_price_ntd_m2": 20_000.0,
        "total_price_valid": True,
        "unit_price_valid": True,
        "land_transfer_area_valid": True,
        "building_transfer_area_valid": None,
        "parking_data_complete": None,
        "has_note": False,
    }


def test_inspect_clean_parquet_accepts_contract(tmp_path: Path) -> None:
    path = tmp_path / "clean.parquet"
    table = pa.Table.from_pylist([_clean_row()], schema=CLEAN_SCHEMA)
    pq.write_table(table, path)

    snapshot = inspect_clean_parquet(path)

    assert snapshot.rows == 1
    assert snapshot.cleaning_run_id == "cleaning-test-run"
    assert len(snapshot.sha256) == 64


def test_inspect_clean_parquet_rejects_schema_drift(tmp_path: Path) -> None:
    path = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"source_transaction_id": ["TEST-1"]}), path)

    with pytest.raises(ValueError, match="schema"):
        inspect_clean_parquet(path)


def _snapshot(tmp_path: Path) -> ParquetSnapshot:
    return ParquetSnapshot(
        path=tmp_path / "clean.parquet",
        rows=10,
        row_groups=1,
        cleaning_run_id="cleaning-test-run",
        sha256="a" * 64,
    )


def test_sync_sql_is_transactional_and_upsert_only(tmp_path: Path) -> None:
    prefix, suffix = build_sync_sql(_snapshot(tmp_path), "load-test")

    assert prefix.startswith("BEGIN;")
    assert "\\copy load_buffer" in prefix
    assert "Parquet buffer validation failed" in suffix
    assert "CREATE TEMP TABLE load_classification" in suffix
    assert "ON CONFLICT (source_transaction_id) DO UPDATE" in suffix
    assert "IS DISTINCT FROM" in suffix
    assert "INSERT INTO staging.load_month_changes" in suffix
    assert "DELETE FROM staging.stg_transactions" not in suffix
    assert "validate_transaction_snapshot" not in suffix
    assert "validate_transaction_upsert" in suffix
    assert "rows_deleted = 0" in suffix
    assert suffix.rstrip().endswith("COMMIT;")


def test_sync_sql_classifies_incoming_against_staging_once(tmp_path: Path) -> None:
    _, suffix = build_sync_sql(_snapshot(tmp_path), "load-test")

    assert suffix.count("LEFT JOIN staging.stg_transactions AS target") == 1
    assert "FROM load_classification;" in suffix
    assert "JOIN load_classification AS classification" in suffix


def test_change_detection_excludes_load_metadata() -> None:
    assert "source_transaction_id" not in BUSINESS_COLUMNS
    assert "cleaning_run_id" not in BUSINESS_COLUMNS
    assert set(CORE_IMPACT_COLUMNS).issubset(BUSINESS_COLUMNS)
    assert set(CORE_IMPACT_COLUMNS) == {
        "transaction_year",
        "transaction_month",
        "county_id",
        "town_id",
        "city",
        "district",
        "transaction_type",
        "building_type",
        "total_price_ntd",
        "unit_price_ntd_m2",
    }


def test_sync_sql_compares_business_columns_but_updates_run_metadata(
    tmp_path: Path,
) -> None:
    _, suffix = build_sync_sql(_snapshot(tmp_path), "load-test")
    classification_sql = suffix.split(
        "CREATE TEMP TABLE load_classification",
        1,
    )[1].split("CREATE UNIQUE INDEX load_classification", 1)[0]

    assert "target.cleaning_run_id" not in classification_sql
    for name in BUSINESS_COLUMNS:
        assert f"target.{name}" in classification_sql
        assert f"buffer.{name}" in classification_sql
    assert "cleaning_run_id = EXCLUDED.cleaning_run_id" in suffix


def test_sync_sql_tracks_month_moves_and_same_month_changes(tmp_path: Path) -> None:
    _, suffix = build_sync_sql(_snapshot(tmp_path), "load-test")

    assert "classification.old_month" in suffix
    assert "IS DISTINCT FROM classification.new_month" in suffix
    assert "IS NOT DISTINCT FROM classification.new_month" in suffix
    assert "bool_or(has_core_impact)" in suffix
    assert "stats.target_before + stats.rows_inserted" in suffix


def test_apply_staging_ddl_uses_upsert_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_names = [
        "001_create_schema.sql",
        "002_create_stg_transactions.sql",
        "003_create_load_runs.sql",
        "004_validate_load.sql",
        "005_create_load_month_changes.sql",
        "006_validate_upsert_load.sql",
    ]
    for name in expected_names:
        (tmp_path / name).touch()

    applied_names: list[str] = []

    def fake_run_psql(
        config: LoadConfig,
        *,
        sql: str | None = None,
        sql_file: Path | None = None,
        tuples_only: bool = False,
    ) -> str:
        del config, sql, tuples_only
        assert sql_file is not None
        applied_names.append(sql_file.name)
        return ""

    monkeypatch.setattr(postgres_loader, "_run_psql", fake_run_psql)

    postgres_loader.apply_staging_ddl(LoadConfig(sql_dir=tmp_path))

    assert applied_names == expected_names


def test_upsert_validation_sql_checks_all_reconciliations() -> None:
    sql = (
        ROOT / "sql" / "staging" / "006_validate_upsert_load.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION staging.validate_transaction_upsert" in sql
    assert "p_rows_inserted + p_rows_updated + p_rows_unchanged" in sql
    assert "p_rows_applied <> p_rows_inserted + p_rows_updated" in sql
    assert "p_month_rows_entered <> p_rows_inserted + p_rows_moved" in sql
    assert "'mode', 'upsert_only'" in sql
    assert "'rows_deleted', 0" in sql


def test_each_staging_attempt_uses_an_independent_uuid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    expected_id = "pgload_1234567890abcdef1234567890abcdef"
    expected = LoadResult(
        load_batch_id=expected_id,
        cleaning_run_id=snapshot.cleaning_run_id,
        parquet_rows=snapshot.rows,
        rows_inserted=10,
        rows_updated=0,
        rows_unchanged=0,
        rows_deleted=0,
        target_rows=10,
        validation_summary={"status": "success"},
    )
    start_sql: list[str] = []
    streamed: list[str] = []

    monkeypatch.setattr(
        postgres_loader,
        "inspect_clean_parquet",
        lambda path: snapshot,
    )
    monkeypatch.setattr(
        postgres_loader.uuid,
        "uuid4",
        lambda: type("FixedUUID", (), {"hex": expected_id.removeprefix("pgload_")})(),
    )
    monkeypatch.setattr(
        postgres_loader,
        "_run_psql",
        lambda config, **kwargs: start_sql.append(str(kwargs["sql"])) or "",
    )
    monkeypatch.setattr(
        postgres_loader,
        "_stream_snapshot",
        lambda config, source, load_batch_id: streamed.append(load_batch_id),
    )
    monkeypatch.setattr(
        postgres_loader,
        "_read_load_result",
        lambda config, load_batch_id: expected,
    )

    result = load_postgres_staging(
        LoadConfig(parquet_path=snapshot.path, apply_ddl=False)
    )

    assert result is expected
    assert streamed == [expected_id]
    assert expected_id in start_sql[0]
