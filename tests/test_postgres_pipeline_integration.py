from __future__ import annotations

import getpass
import os
import secrets
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.analytics.postgres_loader import (
    AnalyticsConfig,
    AnalyticsRefreshError,
    refresh_analytics_batch,
    refresh_analytics_full,
)
from src.cleaning.contract import CLEAN_SCHEMA
from src.core.postgres_loader import (
    CoreConfig,
    CoreSyncError,
    initialize_core,
    sync_core_batch,
)
from src.database.postgres import (
    PostgreSQLExecutionError,
    PsqlConfig,
    hold_advisory_lock,
    run_psql,
    sql_literal,
)
from src.loading.postgres_loader import LoadConfig, load_postgres_staging
from src.orchestration.pipeline import PipelineConfig, run_pipeline


RUN_INTEGRATION = os.environ.get("RUN_POSTGRES_INTEGRATION") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_SQL_DIR = PROJECT_ROOT / "sql" / "analytics"

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set RUN_POSTGRES_INTEGRATION=1 to start disposable PostgreSQL",
)


@dataclass(frozen=True)
class IntegrationDatabase:
    """一次性 PostgreSQL 的連線資料；不使用既有 PG* 設定。"""

    database: str
    psql_path: str


def _run_command(args: list[str]) -> None:
    """執行 PostgreSQL 管理工具並在失敗時保留可讀原因。"""
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail)


@pytest.fixture
def integration_database() -> IntegrationDatabase:
    """建立、啟動並在測試結束後移除一次性 PostgreSQL cluster。"""
    required = {
        name: shutil.which(name)
        for name in ("initdb", "pg_ctl", "createdb", "psql")
    }
    missing = [name for name, path in required.items() if path is None]
    if missing:
        pytest.skip("missing PostgreSQL tools: " + ", ".join(missing))

    # PostgreSQL 的 Unix socket 路徑上限很短；macOS 與 Linux 都提供短路徑 /tmp。
    with tempfile.TemporaryDirectory(
        prefix="dashboard-pg-",
        dir="/tmp",
    ) as root_text:
        root = Path(root_text)
        data_dir = root / "data"
        socket_dir = root / "socket"
        log_path = root / "postgres.log"
        socket_dir.mkdir()
        # 伺服器停用 TCP；高位 port 只用來區分 Unix socket 檔名。
        port = 40_000 + secrets.randbelow(20_000)
        admin_user = f"{getpass.getuser()}_dashboard_test"

        _run_command(
            [
                required["initdb"] or "initdb",
                "--pgdata",
                str(data_dir),
                "--auth=trust",
                "--username",
                admin_user,
                "--no-locale",
                "--encoding=UTF8",
                "--no-sync",
            ]
        )

        server_options = " ".join(
            [
                "-F",
                "-h ''",
                f"-k {shlex.quote(str(socket_dir))}",
                f"-p {port}",
            ]
        )
        _run_command(
            [
                required["pg_ctl"] or "pg_ctl",
                "--pgdata",
                str(data_dir),
                "--log",
                str(log_path),
                "--options",
                server_options,
                "--wait",
                "start",
            ]
        )

        try:
            database_name = "dashboard_integration"
            _run_command(
                [
                    required["createdb"] or "createdb",
                    "--host",
                    str(socket_dir),
                    "--port",
                    str(port),
                    "--username",
                    admin_user,
                    database_name,
                ]
            )
            conninfo = " ".join(
                [
                    f"host={socket_dir}",
                    f"port={port}",
                    f"user={admin_user}",
                    f"dbname={database_name}",
                ]
            )
            yield IntegrationDatabase(
                database=conninfo,
                psql_path=required["psql"] or "psql",
            )
        finally:
            _run_command(
                [
                    required["pg_ctl"] or "pg_ctl",
                    "--pgdata",
                    str(data_dir),
                    "--wait",
                    "--mode=fast",
                    "stop",
                ]
            )


def _transaction(
    source_transaction_id: str,
    *,
    cleaning_run_id: str,
    month: int,
    transaction_type: str = "房地(土地+建物)",
    building_type: str | None = "公寓(5樓含以下無電梯)",
    total_price_ntd: int = 8_000_000,
    unit_price_ntd_m2: float = 100_000.0,
    room_count: int | None = 3,
) -> dict[str, object]:
    """建立符合 Cleaning Specification v1 的小型測試交易。"""
    is_land = transaction_type == "土地"
    return {
        "cleaning_run_id": cleaning_run_id,
        "source_transaction_id": source_transaction_id,
        "transaction_year": 2024,
        "transaction_month": month,
        "transaction_date": date(2024, month, 15),
        "transaction_date_valid": True,
        "completion_date": None if is_land else date(2015, 1, 1),
        "completion_date_status": "not_applicable" if is_land else "valid",
        "completion_after_transaction": None if is_land else False,
        "county_id": "09007",
        "town_id": "09007010",
        "city": "連江縣",
        "district": "南竿鄉",
        "transaction_type": transaction_type,
        "urban_land_use_type": "住",
        "nonurban_land_use_zone": None,
        "building_type": None if is_land else building_type,
        "primary_use": None if is_land else "住家用",
        "has_management": None if is_land else False,
        "land_transfer_area_m2": 50.0,
        "building_transfer_area_m2": None if is_land else 80.0,
        "room_count": None if is_land else room_count,
        "hall_count": None if is_land else 2,
        "bathroom_count": None if is_land else 1,
        "total_price_ntd": total_price_ntd,
        "unit_price_ntd_m2": unit_price_ntd_m2,
        "total_price_valid": True,
        "unit_price_valid": True,
        "land_transfer_area_valid": True,
        "building_transfer_area_valid": None if is_land else True,
        "parking_data_complete": None if is_land else False,
        "has_note": False,
    }


def _write_parquet(
    path: Path,
    rows: list[dict[str, object]],
) -> Path:
    """用正式 clean schema 發布一個測試用 Parquet。"""
    table = pa.Table.from_pylist(rows, schema=CLEAN_SCHEMA)
    pq.write_table(table, path)
    return path


def _query(database: IntegrationDatabase, sql: str) -> str:
    """對一次性資料庫執行測試查詢。"""
    return run_psql(
        PsqlConfig(
            database=database.database,
            psql_path=database.psql_path,
        ),
        sql=sql,
        tuples_only=True,
    )


def _run_sql_file(
    database: IntegrationDatabase,
    sql_file: Path,
    *,
    variables: dict[str, str] | None = None,
    tuples_only: bool = False,
) -> str:
    """在一次性資料庫執行專案內的真實 SQL 檔。"""
    return run_psql(
        PsqlConfig(
            database=database.database,
            psql_path=database.psql_path,
            workdir=PROJECT_ROOT,
        ),
        sql_file=sql_file,
        variables=variables,
        tuples_only=tuples_only,
    )


def _analytics_validation_lines(
    database: IntegrationDatabase,
) -> list[str]:
    """執行 004，回傳各 validation SELECT 的非空資料列。"""
    output = _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "004_validate_national_monthly_kpi.sql",
        tuples_only=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def _assert_analytics_validation(
    lines: list[str],
    *,
    cutoff_status: str,
    expected_month_count: int,
) -> None:
    """確認 004 的 cutoff、coverage、base 與 YoY 四組摘要。"""
    assert len(lines) == 4
    assert lines[0].split("\t")[-1] == cutoff_status
    assert lines[1] == (
        f"{expected_month_count}\t{expected_month_count}\t0\t0"
    )
    assert lines[2] == "\t".join(["0"] * 6)
    assert lines[3] == "\t".join(["0"] * 5)


def _city_analytics_validation_lines(
    database: IntegrationDatabase,
) -> list[str]:
    """執行 008，回傳各 validation SELECT 的非空資料列。"""
    output = _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "008_validate_city_monthly_kpi.sql",
        tuples_only=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def _assert_city_analytics_validation(
    lines: list[str],
    *,
    expected_month_count: int,
) -> None:
    """確認 008 的 range、mapping、骨架、KPI、YoY 與跨粒度摘要。"""
    expected_row_count = expected_month_count * 22

    # 第 7 個查詢在健康狀態回傳 0 rows，因此輸出只有前六組摘要。
    assert len(lines) == 6
    assert lines[0].split("\t")[-1] == "PASS_ALIGNED"
    assert lines[1] == "22\tPASS_MAPPING"
    assert lines[2] == "\t".join(
        [
            str(expected_row_count),
            str(expected_row_count),
            str(expected_month_count),
            str(expected_month_count),
            "22",
            "22",
            "0",
            "0",
            "0",
        ]
    )
    assert lines[3] == "\t".join(["0"] * 7)
    assert lines[4] == "\t".join(["0"] * 5)
    assert lines[5] == "0"


def _district_analytics_validation_lines(
    database: IntegrationDatabase,
) -> list[str]:
    """執行 012，回傳 coverage、KPI 與跨粒度三組摘要。"""
    output = _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "012_validate_district_rolling_3m.sql",
        tuples_only=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def _assert_district_analytics_validation(
    lines: list[str],
    *,
    expected_anchor_count: int,
    expected_location_count: int = 368,
) -> None:
    """確認 012 的 coverage、KPI／YoY 與 City/District count 對帳。"""
    expected_row_count = expected_anchor_count * expected_location_count

    # 第四個查詢只在不一致時回傳明細；健康狀態固定只有三列摘要。
    assert len(lines) == 3
    coverage = lines[0].split("\t")
    assert coverage[6:] == [
        str(expected_location_count),
        str(expected_anchor_count),
        str(expected_row_count),
        str(expected_row_count),
        "0",
        "0",
        "0",
    ]
    assert lines[1] == "\t".join(["0"] * 11)
    assert lines[2] == "0"


def _record_successful_analytics_batch(
    database: IntegrationDatabase,
    *,
    load_batch_id: str,
    month_start: str,
    rows_entered: int = 0,
    rows_changed: int = 0,
) -> None:
    """建立 003 所需的成功 staging/core batch 與月份影響摘要。"""
    source_rows = rows_entered + rows_changed
    batch_literal = sql_literal(load_batch_id)
    _query(
        database,
        f"""
INSERT INTO staging.load_runs (
    load_batch_id,
    cleaning_run_id,
    source_file,
    source_sha256,
    parquet_rows,
    rows_inserted,
    rows_updated,
    rows_unchanged,
    rows_deleted,
    target_rows,
    status,
    validation_summary,
    completed_at
)
VALUES (
    {batch_literal},
    {batch_literal},
    'analytics-integration-fixture',
    repeat('a', 64),
    {source_rows},
    {rows_entered},
    {rows_changed},
    0,
    0,
    0,
    'success',
    '{{}}'::JSONB,
    clock_timestamp()
);

INSERT INTO core.sync_runs (
    load_batch_id,
    status,
    source_rows,
    rows_inserted,
    rows_updated,
    rows_unchanged,
    rows_applied,
    validation_summary,
    completed_at
)
VALUES (
    {batch_literal},
    'success',
    {source_rows},
    {rows_entered},
    {rows_changed},
    0,
    {source_rows},
    '{{}}'::JSONB,
    clock_timestamp()
);

INSERT INTO staging.load_month_changes (
    load_batch_id,
    month_start,
    rows_entered,
    rows_changed,
    rows_exited,
    has_core_impact
)
VALUES (
    {batch_literal},
    DATE {sql_literal(month_start)},
    {rows_entered},
    {rows_changed},
    0,
    TRUE
);
""",
    )


def _load_config(
    database: IntegrationDatabase,
    parquet_path: Path,
) -> LoadConfig:
    return LoadConfig(
        parquet_path=parquet_path,
        database=database.database,
        psql_path=database.psql_path,
    )


def _core_config(database: IntegrationDatabase) -> CoreConfig:
    return CoreConfig(
        database=database.database,
        psql_path=database.psql_path,
        apply_ddl=False,
    )


def test_disposable_postgres_staging_to_core_pipeline(
    integration_database: IntegrationDatabase,
    tmp_path: Path,
) -> None:
    """驗證初始化、增量分類、冪等、rollback、重試與 running guard。"""
    first_run = "cleaning-integration-1"
    first_parquet = _write_parquet(
        tmp_path / "batch_1.parquet",
        [
            _transaction(
                "TEST-A",
                cleaning_run_id=first_run,
                month=1,
                transaction_type="土地",
            ),
            _transaction("TEST-B", cleaning_run_id=first_run, month=1),
            _transaction(
                "TEST-C",
                cleaning_run_id=first_run,
                month=2,
                building_type="透天厝",
            ),
        ],
    )
    first_load = load_postgres_staging(
        _load_config(integration_database, first_parquet)
    )

    assert re.fullmatch(r"pgload_[0-9a-f]{32}", first_load.load_batch_id)
    assert first_load.rows_inserted == 3
    assert first_load.rows_updated == 0
    assert first_load.rows_unchanged == 0
    assert first_load.rows_deleted == 0

    initialize_core(_core_config(integration_database))
    assert _query(
        integration_database,
        "SELECT count(*) FROM core.dim_location;",
    ) == "368"
    assert _query(
        integration_database,
        "SELECT count(*) FROM core.dim_building_type;",
    ) == "14"
    assert _query(
        integration_database,
        "SELECT count(*) FROM core.fact_transactions;",
    ) == "3"

    second_run = "cleaning-integration-2"
    second_parquet = _write_parquet(
        tmp_path / "batch_2.parquet",
        [
            # 只換 cleaning_run_id，不算 staging 業務變更。
            _transaction(
                "TEST-A",
                cleaning_run_id=second_run,
                month=1,
                transaction_type="土地",
            ),
            # core 價格欄位更新。
            _transaction(
                "TEST-B",
                cleaning_run_id=second_run,
                month=1,
                total_price_ntd=8_500_000,
            ),
            # 只有 staging 格局欄位更新，core 應判定 unchanged。
            _transaction(
                "TEST-C",
                cleaning_run_id=second_run,
                month=2,
                building_type="透天厝",
                room_count=4,
            ),
            _transaction(
                "TEST-D",
                cleaning_run_id=second_run,
                month=3,
                building_type="華廈(10層含以下有電梯)",
            ),
        ],
    )
    second_load = load_postgres_staging(
        _load_config(integration_database, second_parquet)
    )

    assert second_load.rows_inserted == 1
    assert second_load.rows_updated == 2
    assert second_load.rows_unchanged == 1
    assert second_load.rows_deleted == 0
    assert _query(
        integration_database,
        f"""
SELECT month_start, rows_entered, rows_changed, rows_exited, has_core_impact
FROM staging.load_month_changes
WHERE load_batch_id = {sql_literal(second_load.load_batch_id)}
ORDER BY month_start;
""",
    ).splitlines() == [
        "2024-01-01\t0\t1\t0\tt",
        "2024-02-01\t0\t1\t0\tf",
        "2024-03-01\t1\t0\t0\tt",
    ]

    second_core = sync_core_batch(
        _core_config(integration_database),
        second_load.load_batch_id,
    )
    assert second_core.source_rows == 3
    assert second_core.rows_inserted == 1
    assert second_core.rows_updated == 1
    assert second_core.rows_unchanged == 1
    assert second_core.rows_applied == 2
    assert second_core.attempt_count == 1

    replayed_core = sync_core_batch(
        _core_config(integration_database),
        second_load.load_batch_id,
    )
    assert replayed_core == second_core
    assert _query(
        integration_database,
        "SELECT count(*) FROM core.fact_transactions;",
    ) == "4"
    assert _query(
        integration_database,
        """
SELECT total_price_ntd
FROM core.fact_transactions
WHERE source_transaction_id = 'TEST-B';
""",
    ) == "8500000"

    third_run = "cleaning-integration-3"
    third_parquet = _write_parquet(
        tmp_path / "batch_3.parquet",
        [
            _transaction("TEST-E", cleaning_run_id=third_run, month=4),
            _transaction("TEST-F", cleaning_run_id=third_run, month=4),
        ],
    )
    third_load = load_postgres_staging(
        _load_config(integration_database, third_parquet)
    )
    assert third_load.rows_inserted == 2

    _query(
        integration_database,
        """
CREATE FUNCTION core.reject_test_f()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.source_transaction_id = 'TEST-F' THEN
        RAISE EXCEPTION 'intentional integration failure';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER reject_test_f
BEFORE INSERT OR UPDATE ON core.fact_transactions
FOR EACH ROW EXECUTE FUNCTION core.reject_test_f();
""",
    )

    with pytest.raises(CoreSyncError, match="intentional integration failure"):
        sync_core_batch(
            _core_config(integration_database),
            third_load.load_batch_id,
        )

    assert _query(
        integration_database,
        """
SELECT count(*)
FROM core.fact_transactions
WHERE source_transaction_id IN ('TEST-E', 'TEST-F');
""",
    ) == "0"
    failed_status = _query(
        integration_database,
        f"""
SELECT status, attempt_count, error_message IS NOT NULL
FROM core.sync_runs
WHERE load_batch_id = {sql_literal(third_load.load_batch_id)};
""",
    )
    assert failed_status == "failed\t1\tt"

    _query(
        integration_database,
        """
DROP TRIGGER reject_test_f ON core.fact_transactions;
DROP FUNCTION core.reject_test_f();
""",
    )
    retried_core = sync_core_batch(
        _core_config(integration_database),
        third_load.load_batch_id,
    )
    assert retried_core.rows_inserted == 2
    assert retried_core.rows_applied == 2
    assert retried_core.attempt_count == 2

    fourth_run = "cleaning-integration-4"
    fourth_parquet = _write_parquet(
        tmp_path / "batch_4.parquet",
        [_transaction("TEST-G", cleaning_run_id=fourth_run, month=5)],
    )
    fourth_load = load_postgres_staging(
        _load_config(integration_database, fourth_parquet)
    )
    _query(
        integration_database,
        f"""
INSERT INTO core.sync_runs (load_batch_id, status)
VALUES ({sql_literal(fourth_load.load_batch_id)}, 'running');
""",
    )

    with pytest.raises(CoreSyncError, match="already running"):
        sync_core_batch(
            _core_config(integration_database),
            fourth_load.load_batch_id,
        )
    assert _query(
        integration_database,
        """
SELECT count(*)
FROM core.fact_transactions
WHERE source_transaction_id = 'TEST-G';
""",
    ) == "0"

    # 將測試用 running 狀態明確改成 failed，再由 core loader 重試，確保後續
    # orchestrator 不會在未完成狀態下建立新 staging batch。
    _query(
        integration_database,
        f"""
UPDATE core.sync_runs
SET
    status = 'failed',
    error_message = 'resolved integration interruption',
    completed_at = clock_timestamp()
WHERE load_batch_id = {sql_literal(fourth_load.load_batch_id)};
""",
    )
    fourth_core = sync_core_batch(
        _core_config(integration_database),
        fourth_load.load_batch_id,
    )
    assert fourth_core.attempt_count == 2

    # 日常流程只做增量更新；先明確完整重建分析層所需的月份骨架。
    full_analytics = refresh_analytics_full(
        AnalyticsConfig(
            database=integration_database.database,
            psql_path=integration_database.psql_path,
        )
    )
    assert full_analytics.status == "success"
    assert full_analytics.refresh_mode == "full"

    # 增量失敗會保留狀態，並允許同一批重試；空 SQL 目錄會在取得執行權後失敗，
    # 但不會修改正式來源表。
    missing_analytics_sql = tmp_path / "missing-analytics-sql"
    missing_analytics_sql.mkdir()
    with pytest.raises(AnalyticsRefreshError):
        refresh_analytics_batch(
            AnalyticsConfig(
                database=integration_database.database,
                psql_path=integration_database.psql_path,
                sql_dir=missing_analytics_sql,
                apply_ddl=False,
            ),
            second_load.load_batch_id,
        )
    assert _query(
        integration_database,
        f"""
SELECT status, attempt_count, error_message IS NOT NULL
FROM analytics.refresh_runs
WHERE load_batch_id = {sql_literal(second_load.load_batch_id)};
""",
    ) == "failed\t1\tt"

    retried_analytics = refresh_analytics_batch(
        AnalyticsConfig(
            database=integration_database.database,
            psql_path=integration_database.psql_path,
            apply_ddl=False,
        ),
        second_load.load_batch_id,
    )
    assert retried_analytics.status == "success"
    assert retried_analytics.attempt_count == 2
    assert (
        refresh_analytics_batch(
            AnalyticsConfig(
                database=integration_database.database,
                psql_path=integration_database.psql_path,
                apply_ddl=False,
            ),
            second_load.load_batch_id,
        )
        == retried_analytics
    )

    # 同一資料庫只能有一條 orchestrated pipeline 持有跨層 advisory lock。
    lock_config = PsqlConfig(
        database=integration_database.database,
        psql_path=integration_database.psql_path,
    )
    with hold_advisory_lock(lock_config, "integration.pipeline.lock"):
        with pytest.raises(PostgreSQLExecutionError, match="already held"):
            with hold_advisory_lock(lock_config, "integration.pipeline.lock"):
                pass

    # 獨立分析層命令共用跨層鎖，整體流程持有鎖時不得進入。
    shared_lock_name = "integration.analytics.pipeline.lock"
    with hold_advisory_lock(lock_config, shared_lock_name):
        with pytest.raises(PostgreSQLExecutionError, match="already held"):
            refresh_analytics_batch(
                AnalyticsConfig(
                    database=integration_database.database,
                    psql_path=integration_database.psql_path,
                    apply_ddl=False,
                    lock_name=shared_lock_name,
                ),
                second_load.load_batch_id,
            )

    fifth_run = "cleaning-integration-5"
    fifth_parquet = _write_parquet(
        tmp_path / "batch_5.parquet",
        [_transaction("TEST-H", cleaning_run_id=fifth_run, month=6)],
    )
    pipeline_config = PipelineConfig(
        parquet_path=fifth_parquet,
        database=integration_database.database,
        psql_path=integration_database.psql_path,
    )
    pipeline_result = run_pipeline(pipeline_config)
    assert pipeline_result.staging.rows_inserted == 1
    assert pipeline_result.core.rows_inserted == 1
    assert pipeline_result.core.load_batch_id == (
        pipeline_result.staging.load_batch_id
    )
    assert pipeline_result.analytics.status == "success"
    assert pipeline_result.analytics.load_batch_id == (
        pipeline_result.staging.load_batch_id
    )

    # 相同 Parquet 再跑一次會建立新的 load attempt，但不重寫 staging 或 core。
    replayed_pipeline = run_pipeline(pipeline_config)
    assert replayed_pipeline.staging.rows_inserted == 0
    assert replayed_pipeline.staging.rows_updated == 0
    assert replayed_pipeline.staging.rows_unchanged == 1
    assert replayed_pipeline.core.source_rows == 0
    assert replayed_pipeline.core.rows_applied == 0
    assert replayed_pipeline.analytics.status == "success"
    assert (
        replayed_pipeline.analytics.refresh_summary["national"][
            "affected_base_month_count"
        ]
        == 0
    )


def test_disposable_postgres_national_monthly_analytics(
    integration_database: IntegrationDatabase,
) -> None:
    """驗證 full、incremental、cutoff、M/M+12、no-shrink 與 gap gate。"""
    database = integration_database

    # 使用正式 staging/core DDL 建立 Analytics 所依賴的資料庫契約。
    for sql_file in (
        PROJECT_ROOT / "sql" / "staging" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "staging" / "002_create_stg_transactions.sql",
        PROJECT_ROOT / "sql" / "staging" / "003_create_load_runs.sql",
        PROJECT_ROOT / "sql" / "staging" / "005_create_load_month_changes.sql",
        PROJECT_ROOT / "sql" / "core" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "core" / "002_create_dim_location.sql",
        PROJECT_ROOT / "sql" / "core" / "005_create_dim_building_type.sql",
        PROJECT_ROOT / "sql" / "core" / "008_create_fact_transactions.sql",
        PROJECT_ROOT / "sql" / "core" / "011_create_sync_runs.sql",
        ANALYTICS_SQL_DIR / "001_create_national_monthly_kpi.sql",
    ):
        _run_sql_file(database, sql_file)

    # 2012-08 至 2013-08 多數月份各 4 筆；2013-01 只有 2 筆，
    # 仍應保留在完整骨架。尾端 2013-09 先只有 2 筆，低於 3 筆門檻。
    _query(
        database,
        """
INSERT INTO core.dim_location (
    county_id,
    town_id,
    city,
    district
)
VALUES ('00001', '00000001', '測試市', '測試區');

INSERT INTO core.dim_building_type (
    building_type_id,
    building_type_name,
    member_type
)
VALUES (2, '測試住宅', 'official');

WITH monthly_counts AS (
    SELECT
        generated.month_start::DATE AS month_start,
        CASE
            WHEN generated.month_start::DATE IN (
                DATE '2013-01-01',
                DATE '2013-09-01'
            ) THEN 2
            ELSE 4
        END AS transaction_count
    FROM GENERATE_SERIES(
        DATE '2012-08-01',
        DATE '2013-09-01',
        INTERVAL '1 month'
    ) AS generated(month_start)
)
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    FORMAT(
        'BASE-%s-%s',
        TO_CHAR(monthly.month_start, 'YYYYMM'),
        generated.sequence_number
    ),
    monthly.month_start,
    (SELECT location_id FROM core.dim_location LIMIT 1),
    2,
    '房地(土地+建物)',
    8000000
        + EXTRACT(MONTH FROM monthly.month_start)::BIGINT * 10000
        + generated.sequence_number * 100000,
    100000.0
        + EXTRACT(MONTH FROM monthly.month_start) * 100
        + generated.sequence_number * 1000
FROM monthly_counts AS monthly
CROSS JOIN LATERAL GENERATE_SERIES(
    1,
    monthly.transaction_count
) AS generated(sequence_number);
""",
    )

    # 完整更新：中位數為 4、門檻為 3，截止應為 2013-08。
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "002_refresh_national_monthly_kpi_full.sql",
    )
    assert _query(
        database,
        """
SELECT
    MIN(month_start),
    MAX(month_start),
    COUNT(*),
    MAX(price_complete_transaction_count) FILTER (
        WHERE month_start = DATE '2013-01-01'
    ),
    COUNT(*) FILTER (
        WHERE month_start = DATE '2013-09-01'
    )
FROM analytics.national_monthly_kpi;
""",
    ) == "2012-08-01\t2013-08-01\t13\t2\t0"
    _assert_analytics_validation(
        _analytics_validation_lines(database),
        cutoff_status="PASS_ALIGNED",
        expected_month_count=13,
    )

    # 增加兩筆 2013-09 交易後達到門檻，003 應延伸一個月。
    _query(
        database,
        """
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    'EXT-201309-' || generated.sequence_number,
    DATE '2013-09-01',
    (SELECT location_id FROM core.dim_location LIMIT 1),
    2,
    '房地(土地+建物)',
    9000000 + generated.sequence_number * 100000,
    120000.0 + generated.sequence_number * 1000
FROM GENERATE_SERIES(1, 2) AS generated(sequence_number);
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="analytics-extension",
        month_start="2013-09-01",
        rows_entered=2,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "analytics-extension"},
    )
    assert _query(
        database,
        """
SELECT MAX(month_start), COUNT(*), MAX(price_complete_transaction_count)
FROM analytics.national_monthly_kpi
WHERE month_start = DATE '2013-09-01'
   OR month_start < DATE '2013-09-01';
""",
    ) == "2013-09-01\t14\t4"
    _assert_analytics_validation(
        _analytics_validation_lines(database),
        cutoff_status="PASS_ALIGNED",
        expected_month_count=14,
    )

    # 修改 M=2012-08 的價格後，M 的 base KPI 與 M+12=2013-08 的 YoY
    # 都必須改變，並繼續通過 004 全量對帳。
    before_values = _query(
        database,
        """
SELECT
    (SELECT median_unit_price_10k_ping
     FROM analytics.national_monthly_kpi
     WHERE month_start = DATE '2012-08-01'),
    (SELECT median_unit_price_yoy
     FROM analytics.national_monthly_kpi
     WHERE month_start = DATE '2013-08-01');
""",
    )
    _query(
        database,
        """
UPDATE core.fact_transactions
SET
    total_price_ntd = total_price_ntd * 2,
    unit_price_ntd_m2 = unit_price_ntd_m2 * 2
WHERE source_transaction_id = 'BASE-201208-1';
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="analytics-m-plus-12",
        month_start="2012-08-01",
        rows_changed=1,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "analytics-m-plus-12"},
    )
    after_values = _query(
        database,
        """
SELECT
    (SELECT median_unit_price_10k_ping
     FROM analytics.national_monthly_kpi
     WHERE month_start = DATE '2012-08-01'),
    (SELECT median_unit_price_yoy
     FROM analytics.national_monthly_kpi
     WHERE month_start = DATE '2013-08-01');
""",
    )
    assert after_values != before_values
    _assert_analytics_validation(
        _analytics_validation_lines(database),
        cutoff_status="PASS_ALIGNED",
        expected_month_count=14,
    )

    # 尾端月份失去價格完整案件後，candidate 回到 2013-08；incremental
    # 仍保留 current 2013-09，並把該月重算為 count=0、price=NULL。
    _query(
        database,
        """
UPDATE core.fact_transactions
SET
    total_price_ntd = NULL,
    unit_price_ntd_m2 = NULL
WHERE transaction_month = DATE '2013-09-01';
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="analytics-no-shrink",
        month_start="2013-09-01",
        rows_changed=4,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "analytics-no-shrink"},
    )
    assert _query(
        database,
        """
SELECT
    month_start,
    price_complete_transaction_count,
    median_unit_price_10k_ping IS NULL,
    avg_unit_price_10k_ping IS NULL,
    median_total_price_10k IS NULL,
    avg_total_price_10k IS NULL
FROM analytics.national_monthly_kpi
WHERE month_start = DATE '2013-09-01';
""",
    ) == "2013-09-01\t0\tt\tt\tt\tt"
    _assert_analytics_validation(
        _analytics_validation_lines(database),
        cutoff_status="REVIEW_CURRENT_AHEAD_OF_CANDIDATE",
        expected_month_count=14,
    )

    # 人為刪除已發布骨架中的月份後，003 必須在任何 KPI 寫入前中止。
    _query(
        database,
        "DELETE FROM analytics.national_monthly_kpi "
        "WHERE month_start = DATE '2013-01-01';",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="analytics-gap-gate",
        month_start="2013-02-01",
        rows_changed=1,
    )
    with pytest.raises(
        PostgreSQLExecutionError,
        match="missing target month 2013-01-01",
    ):
        _run_sql_file(
            database,
            ANALYTICS_SQL_DIR
            / "003_refresh_national_monthly_kpi_incremental.sql",
            variables={"load_batch_id": "analytics-gap-gate"},
        )


def test_disposable_postgres_city_monthly_analytics(
    integration_database: IntegrationDatabase,
) -> None:
    """驗證 City full/incremental、零交易骨架、M+12、validation 與 gap gate。"""
    database = integration_database

    # 使用正式 DDL 與完整 368-row location reference，確保測試真的覆蓋
    # 22 個縣市，而不是用單一假縣市繞過 county contract。
    for sql_file in (
        PROJECT_ROOT / "sql" / "staging" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "staging" / "002_create_stg_transactions.sql",
        PROJECT_ROOT / "sql" / "staging" / "003_create_load_runs.sql",
        PROJECT_ROOT / "sql" / "staging" / "005_create_load_month_changes.sql",
        PROJECT_ROOT / "sql" / "core" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "core" / "002_create_dim_location.sql",
        PROJECT_ROOT / "sql" / "core" / "005_create_dim_building_type.sql",
        PROJECT_ROOT / "sql" / "core" / "008_create_fact_transactions.sql",
        PROJECT_ROOT / "sql" / "core" / "011_create_sync_runs.sql",
        PROJECT_ROOT / "sql" / "core" / "003_upsert_dim_location.sql",
        PROJECT_ROOT / "sql" / "core" / "006_upsert_dim_building_type.sql",
        ANALYTICS_SQL_DIR / "001_create_national_monthly_kpi.sql",
        ANALYTICS_SQL_DIR / "005_create_city_monthly_kpi.sql",
    ):
        _run_sql_file(database, sql_file)

    # 每個完整月份放 4 筆臺北市交易；2013-01 是中間低量月，2013-09
    # 起初是未達門檻的尾端月份。其他 21 縣市刻意保持零交易。
    _query(
        database,
        """
WITH monthly_counts AS (
    SELECT
        generated.month_start::DATE AS month_start,
        CASE
            WHEN generated.month_start::DATE IN (
                DATE '2013-01-01',
                DATE '2013-09-01'
            ) THEN 2
            ELSE 4
        END AS transaction_count
    FROM GENERATE_SERIES(
        DATE '2012-08-01',
        DATE '2013-09-01',
        INTERVAL '1 month'
    ) AS generated(month_start)
)
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    FORMAT(
        'CITY-BASE-%s-%s',
        TO_CHAR(monthly.month_start, 'YYYYMM'),
        generated.sequence_number
    ),
    monthly.month_start,
    (
        SELECT location.location_id
        FROM core.dim_location AS location
        WHERE location.city = '臺北市'
        ORDER BY location.location_id
        LIMIT 1
    ),
    2,
    '房地(土地+建物)',
    8000000
        + EXTRACT(MONTH FROM monthly.month_start)::BIGINT * 10000
        + generated.sequence_number * 100000,
    100000.0
        + EXTRACT(MONTH FROM monthly.month_start) * 100
        + generated.sequence_number * 1000
FROM monthly_counts AS monthly
CROSS JOIN LATERAL GENERATE_SERIES(
    1,
    monthly.transaction_count
) AS generated(sequence_number);
""",
    )

    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "002_refresh_national_monthly_kpi_full.sql",
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "006_refresh_city_monthly_kpi_full.sql",
    )

    # 13 個已發布月份 × 22 個縣市；無交易的連江縣仍有明確的 0/NULL row。
    assert _query(
        database,
        """
SELECT
    MIN(month_start),
    MAX(month_start),
    COUNT(DISTINCT month_start),
    COUNT(DISTINCT county_id),
    COUNT(*)
FROM analytics.city_monthly_kpi;
""",
    ) == "2012-08-01\t2013-08-01\t13\t22\t286"
    assert _query(
        database,
        """
SELECT
    price_complete_transaction_count,
    median_unit_price_10k_ping IS NULL,
    avg_unit_price_10k_ping IS NULL,
    median_total_price_10k IS NULL,
    avg_total_price_10k IS NULL
FROM analytics.city_monthly_kpi
WHERE month_start = DATE '2012-08-01'
  AND city = '連江縣';
""",
    ) == "0\tt\tt\tt\tt"
    _assert_city_analytics_validation(
        _city_analytics_validation_lines(database),
        expected_month_count=13,
    )

    # 資料表結構必須拒絕案件數為 0、價格卻非 NULL 的不一致資料列。
    with pytest.raises(
        PostgreSQLExecutionError,
        match="city_monthly_kpi_price_population_check",
    ):
        _query(
            database,
            """
INSERT INTO analytics.city_monthly_kpi (
    month_start,
    county_id,
    city,
    median_unit_price_10k_ping,
    price_complete_transaction_count
)
SELECT
    DATE '2014-01-01',
    county.county_id,
    county.city,
    1,
    0
FROM (
    SELECT DISTINCT location.county_id, location.city
    FROM core.dim_location AS location
    WHERE location.city = '連江縣'
) AS county;
""",
        )

    # 故意破壞一個值，008 必須回報一筆 median mismatch 和一筆明細；
    # 再執行 full refresh 應可恢復完全一致。
    _query(
        database,
        """
UPDATE analytics.city_monthly_kpi
SET median_unit_price_10k_ping = median_unit_price_10k_ping + 1
WHERE month_start = DATE '2012-08-01'
  AND city = '臺北市';
""",
    )
    broken_validation = _city_analytics_validation_lines(database)
    assert broken_validation[3].split("\t")[2] == "1"
    assert len(broken_validation) == 7

    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "006_refresh_city_monthly_kpi_full.sql",
    )
    _assert_city_analytics_validation(
        _city_analytics_validation_lines(database),
        expected_month_count=13,
    )

    # 補兩筆 2013-09 後 National 先延伸，City 再建立該月全部 22 rows。
    _query(
        database,
        """
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    'CITY-EXT-201309-' || generated.sequence_number,
    DATE '2013-09-01',
    (
        SELECT location.location_id
        FROM core.dim_location AS location
        WHERE location.city = '臺北市'
        ORDER BY location.location_id
        LIMIT 1
    ),
    2,
    '房地(土地+建物)',
    9000000 + generated.sequence_number * 100000,
    120000.0 + generated.sequence_number * 1000
FROM GENERATE_SERIES(1, 2) AS generated(sequence_number);
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="city-analytics-extension",
        month_start="2013-09-01",
        rows_entered=2,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "city-analytics-extension"},
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "007_refresh_city_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "city-analytics-extension"},
    )
    assert _query(
        database,
        """
SELECT
    COUNT(DISTINCT month_start),
    COUNT(DISTINCT county_id),
    COUNT(*),
    MAX(price_complete_transaction_count) FILTER (
        WHERE month_start = DATE '2013-09-01'
          AND city = '連江縣'
    )
FROM analytics.city_monthly_kpi;
""",
    ) == "14\t22\t308\t0"
    _assert_city_analytics_validation(
        _city_analytics_validation_lines(database),
        expected_month_count=14,
    )

    # M=2012-08 的價格修訂必須同時改變臺北市 M base KPI 與
    # M+12=2013-08 的 YoY。
    before_values = _query(
        database,
        """
SELECT
    (SELECT median_unit_price_10k_ping
     FROM analytics.city_monthly_kpi
     WHERE month_start = DATE '2012-08-01'
       AND city = '臺北市'),
    (SELECT median_unit_price_yoy
     FROM analytics.city_monthly_kpi
     WHERE month_start = DATE '2013-08-01'
       AND city = '臺北市');
""",
    )
    _query(
        database,
        """
UPDATE core.fact_transactions
SET
    total_price_ntd = total_price_ntd * 2,
    unit_price_ntd_m2 = unit_price_ntd_m2 * 2
WHERE source_transaction_id = 'CITY-BASE-201208-1';
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="city-analytics-m-plus-12",
        month_start="2012-08-01",
        rows_changed=1,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "city-analytics-m-plus-12"},
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "007_refresh_city_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "city-analytics-m-plus-12"},
    )
    after_values = _query(
        database,
        """
SELECT
    (SELECT median_unit_price_10k_ping
     FROM analytics.city_monthly_kpi
     WHERE month_start = DATE '2012-08-01'
       AND city = '臺北市'),
    (SELECT median_unit_price_yoy
     FROM analytics.city_monthly_kpi
     WHERE month_start = DATE '2013-08-01'
       AND city = '臺北市');
""",
    )
    assert after_values != before_values
    _assert_city_analytics_validation(
        _city_analytics_validation_lines(database),
        expected_month_count=14,
    )

    # 已發布骨架若少一個 county row，incremental 必須在寫入前停止。
    lienchiang_county_id = _query(
        database,
        """
SELECT DISTINCT county_id
FROM core.dim_location
WHERE city = '連江縣';
""",
    )
    _query(
        database,
        f"""
DELETE FROM analytics.city_monthly_kpi
WHERE month_start = DATE '2013-01-01'
  AND county_id = {sql_literal(lienchiang_county_id)};
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="city-analytics-gap-gate",
        month_start="2013-02-01",
        rows_changed=1,
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "003_refresh_national_monthly_kpi_incremental.sql",
        variables={"load_batch_id": "city-analytics-gap-gate"},
    )
    with pytest.raises(
        PostgreSQLExecutionError,
        match="missing target row for month 2013-01-01",
    ):
        _run_sql_file(
            database,
            ANALYTICS_SQL_DIR / "007_refresh_city_monthly_kpi_incremental.sql",
            variables={"load_batch_id": "city-analytics-gap-gate"},
        )


def test_disposable_postgres_district_rolling_analytics(
    integration_database: IntegrationDatabase,
) -> None:
    """驗證 District full/incremental、三月視窗、YoY、延伸與 gap gate。"""
    database = integration_database

    # 使用正式 DDL 與完整 location reference，讓動態行政區骨架與跨層
    # 縣市與鄉鎮市區案件數對帳都依真實契約執行。
    for sql_file in (
        PROJECT_ROOT / "sql" / "staging" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "staging" / "002_create_stg_transactions.sql",
        PROJECT_ROOT / "sql" / "staging" / "003_create_load_runs.sql",
        PROJECT_ROOT / "sql" / "staging" / "005_create_load_month_changes.sql",
        PROJECT_ROOT / "sql" / "core" / "001_create_schema.sql",
        PROJECT_ROOT / "sql" / "core" / "002_create_dim_location.sql",
        PROJECT_ROOT / "sql" / "core" / "005_create_dim_building_type.sql",
        PROJECT_ROOT / "sql" / "core" / "008_create_fact_transactions.sql",
        PROJECT_ROOT / "sql" / "core" / "011_create_sync_runs.sql",
        PROJECT_ROOT / "sql" / "core" / "003_upsert_dim_location.sql",
        PROJECT_ROOT / "sql" / "core" / "006_upsert_dim_building_type.sql",
        ANALYTICS_SQL_DIR / "001_create_national_monthly_kpi.sql",
        ANALYTICS_SQL_DIR / "005_create_city_monthly_kpi.sql",
        ANALYTICS_SQL_DIR / "009_create_district_rolling_3m.sql",
    ):
        _run_sql_file(database, sql_file)

    taipei_town_id = _query(
        database,
        """
SELECT location.town_id
FROM core.dim_location AS location
WHERE location.city = '臺北市'
ORDER BY location.location_id
LIMIT 1;
""",
    )

    # 2012-08 至 2013-12 各 4 筆；2014-01 先只有 2 筆，因此 National
    # 75% cutoff 停在 2013-12。所有交易集中在同一臺北行政區，其他
    # 行政區用來驗證零交易骨架。
    _query(
        database,
        f"""
WITH monthly_counts AS (
    SELECT
        generated.month_start::DATE AS month_start,
        CASE
            WHEN generated.month_start::DATE = DATE '2014-01-01'
            THEN 2
            ELSE 4
        END AS transaction_count
    FROM GENERATE_SERIES(
        DATE '2012-08-01',
        DATE '2014-01-01',
        INTERVAL '1 month'
    ) AS generated(month_start)
)
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    FORMAT(
        'DISTRICT-BASE-%s-%s',
        TO_CHAR(monthly.month_start, 'YYYYMM'),
        generated.sequence_number
    ),
    monthly.month_start,
    (
        SELECT location.location_id
        FROM core.dim_location AS location
        WHERE location.town_id = {sql_literal(taipei_town_id)}
    ),
    (
        SELECT MIN(building_type.building_type_id)
        FROM core.dim_building_type AS building_type
        WHERE building_type.building_type_id <> 0
    ),
    '房地(土地+建物)',
    8000000
        + EXTRACT(MONTH FROM monthly.month_start)::BIGINT * 10000
        + generated.sequence_number * 100000,
    100000.0
        + EXTRACT(MONTH FROM monthly.month_start) * 100
        + generated.sequence_number * 1000
FROM monthly_counts AS monthly
CROSS JOIN LATERAL GENERATE_SERIES(
    1,
    monthly.transaction_count
) AS generated(sequence_number);
""",
    )

    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "002_refresh_national_monthly_kpi_full.sql",
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "006_refresh_city_monthly_kpi_full.sql",
    )
    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "010_refresh_district_rolling_3m_full.sql",
    )

    # 全國發布 2012-08 至 2013-12；鄉鎮市區完整三個月錨點為
    # 2012-10 至 2013-12，共 15 × 368 rows。
    assert _query(
        database,
        """
SELECT
    MIN(month_start),
    MAX(month_start),
    COUNT(DISTINCT month_start),
    COUNT(DISTINCT town_id),
    COUNT(*)
FROM analytics.district_rolling_3m;
""",
    ) == "2012-10-01\t2013-12-01\t15\t368\t5520"

    # 2012-10 anchor 直接合併 Aug/Sep/Oct 的 12 筆交易；不是三個月
    # 中位數的平均。
    assert _query(
        database,
        f"""
SELECT price_complete_transaction_count
FROM analytics.district_rolling_3m
WHERE month_start = DATE '2012-10-01'
  AND town_id = {sql_literal(taipei_town_id)};
""",
    ) == "12"

    # 沒有交易的行政區仍保留，案件數為 0 且四個價格欄位為 NULL。
    assert _query(
        database,
        """
SELECT
    price_complete_transaction_count,
    median_unit_price_10k_ping IS NULL,
    avg_unit_price_10k_ping IS NULL,
    median_total_price_10k IS NULL,
    avg_total_price_10k IS NULL
FROM analytics.district_rolling_3m
WHERE month_start = DATE '2012-10-01'
  AND city = '連江縣'
ORDER BY town_id
LIMIT 1;
""",
    ) == "0\tt\tt\tt\tt"

    _assert_district_analytics_validation(
        _district_analytics_validation_lines(database),
        expected_anchor_count=15,
    )

    # 人為破壞一個中位數，012 必須回報一筆 mismatch 和一筆明細；
    # full refresh 應能恢復完全一致。
    _query(
        database,
        f"""
UPDATE analytics.district_rolling_3m
SET median_unit_price_10k_ping = median_unit_price_10k_ping + 1
WHERE month_start = DATE '2012-10-01'
  AND town_id = {sql_literal(taipei_town_id)};
""",
    )
    broken_validation = _district_analytics_validation_lines(database)
    assert broken_validation[1].split("\t")[1] == "1"
    assert len(broken_validation) == 4

    _run_sql_file(
        database,
        ANALYTICS_SQL_DIR / "010_refresh_district_rolling_3m_full.sql",
    )
    _assert_district_analytics_validation(
        _district_analytics_validation_lines(database),
        expected_anchor_count=15,
    )

    # M=2012-10 的四筆價格修訂，必須更新 base anchors 2012-10～12，
    # 並改變 2013-10～12 對應的 YoY。
    before_base_values = _query(
        database,
        f"""
SELECT month_start, median_unit_price_10k_ping
FROM analytics.district_rolling_3m
WHERE town_id = {sql_literal(taipei_town_id)}
  AND month_start BETWEEN DATE '2012-10-01' AND DATE '2012-12-01'
ORDER BY month_start;
""",
    )
    before_yoy_values = _query(
        database,
        f"""
SELECT month_start, median_unit_price_yoy
FROM analytics.district_rolling_3m
WHERE town_id = {sql_literal(taipei_town_id)}
  AND month_start BETWEEN DATE '2013-10-01' AND DATE '2013-12-01'
ORDER BY month_start;
""",
    )

    _query(
        database,
        """
UPDATE core.fact_transactions
SET
    total_price_ntd = total_price_ntd * 10,
    unit_price_ntd_m2 = unit_price_ntd_m2 * 10
WHERE transaction_month = DATE '2012-10-01';
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="district-analytics-m-through-m-plus-2",
        month_start="2012-10-01",
        rows_changed=4,
    )
    for sql_name in (
        "003_refresh_national_monthly_kpi_incremental.sql",
        "007_refresh_city_monthly_kpi_incremental.sql",
        "011_refresh_district_rolling_3m_incremental.sql",
    ):
        _run_sql_file(
            database,
            ANALYTICS_SQL_DIR / sql_name,
            variables={
                "load_batch_id": "district-analytics-m-through-m-plus-2"
            },
        )

    after_base_values = _query(
        database,
        f"""
SELECT month_start, median_unit_price_10k_ping
FROM analytics.district_rolling_3m
WHERE town_id = {sql_literal(taipei_town_id)}
  AND month_start BETWEEN DATE '2012-10-01' AND DATE '2012-12-01'
ORDER BY month_start;
""",
    )
    after_yoy_values = _query(
        database,
        f"""
SELECT month_start, median_unit_price_yoy
FROM analytics.district_rolling_3m
WHERE town_id = {sql_literal(taipei_town_id)}
  AND month_start BETWEEN DATE '2013-10-01' AND DATE '2013-12-01'
ORDER BY month_start;
""",
    )
    assert after_base_values != before_base_values
    assert after_yoy_values != before_yoy_values
    _assert_district_analytics_validation(
        _district_analytics_validation_lines(database),
        expected_anchor_count=15,
    )

    # 2014-01 補至 4 筆後，National 延伸一個月；District incremental
    # 應建立完整的新 anchor × location spine。
    _query(
        database,
        f"""
INSERT INTO core.fact_transactions (
    source_transaction_id,
    transaction_month,
    location_id,
    building_type_id,
    transaction_type,
    total_price_ntd,
    unit_price_ntd_m2
)
SELECT
    'DISTRICT-EXT-201401-' || generated.sequence_number,
    DATE '2014-01-01',
    (
        SELECT location.location_id
        FROM core.dim_location AS location
        WHERE location.town_id = {sql_literal(taipei_town_id)}
    ),
    (
        SELECT MIN(building_type.building_type_id)
        FROM core.dim_building_type AS building_type
        WHERE building_type.building_type_id <> 0
    ),
    '房地(土地+建物)',
    9000000 + generated.sequence_number * 100000,
    120000.0 + generated.sequence_number * 1000
FROM GENERATE_SERIES(1, 2) AS generated(sequence_number);
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="district-analytics-extension",
        month_start="2014-01-01",
        rows_entered=2,
    )
    for sql_name in (
        "003_refresh_national_monthly_kpi_incremental.sql",
        "007_refresh_city_monthly_kpi_incremental.sql",
        "011_refresh_district_rolling_3m_incremental.sql",
    ):
        _run_sql_file(
            database,
            ANALYTICS_SQL_DIR / sql_name,
            variables={"load_batch_id": "district-analytics-extension"},
        )

    assert _query(
        database,
        f"""
SELECT
    MAX(month_start),
    COUNT(DISTINCT month_start),
    COUNT(*),
    MAX(price_complete_transaction_count) FILTER (
        WHERE month_start = DATE '2014-01-01'
          AND town_id = {sql_literal(taipei_town_id)}
    )
FROM analytics.district_rolling_3m;
""",
    ) == "2014-01-01\t16\t5888\t12"
    _assert_district_analytics_validation(
        _district_analytics_validation_lines(database),
        expected_anchor_count=16,
    )

    # 已發布骨架若少一個 district row，incremental 必須在任何 KPI
    # 寫入前中止。
    _query(
        database,
        f"""
DELETE FROM analytics.district_rolling_3m
WHERE month_start = DATE '2013-01-01'
  AND town_id = {sql_literal(taipei_town_id)};
""",
    )
    _record_successful_analytics_batch(
        database,
        load_batch_id="district-analytics-gap-gate",
        month_start="2013-02-01",
        rows_changed=1,
    )
    with pytest.raises(
        PostgreSQLExecutionError,
        match="missing target row for anchor 2013-01-01",
    ):
        _run_sql_file(
            database,
            ANALYTICS_SQL_DIR / "011_refresh_district_rolling_3m_incremental.sql",
            variables={"load_batch_id": "district-analytics-gap-gate"},
        )
