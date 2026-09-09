from __future__ import annotations

import csv

from src.loading.postgres_loader import ROOT


CORE_SQL_DIR = ROOT / "sql" / "core"
LOCATION_REFERENCE = ROOT / "data" / "reference" / "location_lookup.csv"


def test_full_fact_sync_is_upsert_only() -> None:
    sql = (CORE_SQL_DIR / "009_sync_fact_transactions.sql").read_text(
        encoding="utf-8"
    )

    assert "ON CONFLICT (source_transaction_id)" in sql
    assert "DO UPDATE SET" in sql
    assert "DELETE FROM core.fact_transactions" not in sql
    assert "未出現在 staging 的既有 core 交易一律保留" in sql


def test_location_reference_contains_exactly_368_unique_districts() -> None:
    with LOCATION_REFERENCE.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))

    assert len(rows) == 368
    assert set(rows[0]) == {
        "legacy_county_code",
        "legacy_town_code",
        "county_id",
        "town_id",
        "city",
        "district",
    }
    assert len({(row["county_id"], row["town_id"]) for row in rows}) == 368
    assert len({row["town_id"] for row in rows}) == 368
    assert len({row["county_id"] for row in rows}) == 22
    assert len({row["city"] for row in rows}) == 22
    assert len({(row["county_id"], row["city"]) for row in rows}) == 22
    assert all(len(row["county_id"]) == 5 for row in rows)
    assert all(len(row["town_id"]) == 8 for row in rows)


def test_dim_location_uses_complete_reference_instead_of_transactions() -> None:
    sql = (CORE_SQL_DIR / "003_upsert_dim_location.sql").read_text(
        encoding="utf-8"
    )

    assert "data/reference/location_lookup.csv" in sql
    assert "CREATE TEMP TABLE location_reference_buffer" in sql
    assert "v_rows <> 368" in sql
    assert "v_unique_county_town_ids <> 368" in sql
    assert "v_unique_town_ids <> 368" in sql
    assert "v_unique_county_ids <> 22" in sql
    assert "v_unique_cities <> 22" in sql
    assert "v_unique_county_city_pairs <> 22" in sql
    assert "FROM location_reference_buffer AS source" in sql
    assert "FROM staging.stg_transactions" not in sql
    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")


def test_dim_location_audit_uses_368_row_contract() -> None:
    sql = (CORE_SQL_DIR / "004_validate_dim_location.sql").read_text(
        encoding="utf-8"
    )

    assert "都必須是 368" in sql
    assert "unique_county_town_ids" in sql
    assert "unique_town_ids" in sql
    assert "unique_county_ids" in sql
    assert "unique_cities" in sql
    assert "unique_county_city_pairs" in sql
    assert "invalid_reference_rows" in sql
    assert "364" not in sql


def test_sync_runs_ddl_enforces_state_and_reconciliation_contract() -> None:
    sql = (CORE_SQL_DIR / "011_create_sync_runs.sql").read_text(
        encoding="utf-8"
    )

    for column in (
        "load_batch_id",
        "status",
        "source_rows",
        "rows_inserted",
        "rows_updated",
        "rows_unchanged",
        "rows_applied",
        "attempt_count",
        "validation_summary",
        "error_message",
        "started_at",
        "completed_at",
    ):
        assert column in sql

    assert "REFERENCES staging.load_runs (load_batch_id)" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "status IN ('running', 'success', 'failed')" in sql
    assert "source_rows =" in sql
    assert "rows_inserted + rows_updated + rows_unchanged" in sql
    assert "rows_applied = rows_inserted + rows_updated" in sql
    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")


def test_incremental_core_sql_uses_explicit_batch_and_is_upsert_only() -> None:
    sql = (CORE_SQL_DIR / "012_upsert_core_batch.sql").read_text(
        encoding="utf-8"
    )

    assert "\\if :{?load_batch_id}" in sql
    assert "VALUES (:'load_batch_id')" in sql
    assert "source.load_batch_id = context.load_batch_id" in sql
    assert "load_run.rows_inserted + load_run.rows_updated" in sql
    assert "CREATE TEMP TABLE core_batch_classification" in sql
    assert "ON CONFLICT (source_transaction_id)" in sql
    assert "DO UPDATE SET" in sql
    assert "DELETE FROM core.fact_transactions" not in sql
    assert "INSERT INTO core.dim_location" not in sql
    assert "INSERT INTO core.dim_building_type" not in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_incremental_core_sql_has_blocking_mapping_and_write_gates() -> None:
    sql = (CORE_SQL_DIR / "012_upsert_core_batch.sql").read_text(
        encoding="utf-8"
    )

    assert "pg_advisory_xact_lock" in sql
    assert "v_location_rows <> 368" in sql
    assert "v_building_type_rows <> 14" in sql
    assert "v_unmapped_location_rows <> 0" in sql
    assert "v_mismatched_location_name_rows <> 0" in sql
    assert "v_unmapped_building_type_rows <> 0" in sql
    assert "v_rows_applied <> v_rows_inserted + v_rows_updated" in sql
    assert "v_final_mismatch_rows <> 0" in sql
    assert "UPDATE core.sync_runs AS sync_run" in sql
    assert "status = 'success'" in sql
    assert "validation_summary = validation.summary" in sql


def test_staging_has_index_for_incremental_core_batch_lookup() -> None:
    sql = (
        ROOT / "sql" / "staging" / "002_create_stg_transactions.sql"
    ).read_text(encoding="utf-8")

    assert "idx_stg_transactions_load_batch" in sql
    assert "ON staging.stg_transactions (load_batch_id)" in sql
