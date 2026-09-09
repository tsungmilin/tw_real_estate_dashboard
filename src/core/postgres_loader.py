"""執行 PostgreSQL core 初始化與單一 staging batch 同步。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from src.database.postgres import (
    PostgreSQLExecutionError,
    PsqlConfig,
    run_psql,
    sql_literal,
)


ROOT = Path(__file__).resolve().parents[2]

CORE_DDL_FILES = (
    "001_create_schema.sql",
    "002_create_dim_location.sql",
    "005_create_dim_building_type.sql",
    "008_create_fact_transactions.sql",
    "011_create_sync_runs.sql",
)

# 初始化／完整修復是明確的人工操作，不會由日常 batch 自動觸發。
CORE_INITIALIZATION_FILES = (
    *CORE_DDL_FILES,
    "003_upsert_dim_location.sql",
    "006_upsert_dim_building_type.sql",
    "009_sync_fact_transactions.sql",
    "004_validate_dim_location.sql",
    "007_validate_dim_building_type.sql",
    "010_validate_fact_transactions.sql",
)


class CoreSyncError(PostgreSQLExecutionError):
    """core 初始化、批次狀態或 SQL 執行不符合契約時使用的例外。"""


@dataclass(frozen=True)
class CoreConfig:
    """一次 core 操作所需的資料庫與 SQL 設定。"""

    database: str = "real_estate_dashboard"
    psql_path: str = "psql"
    sql_dir: Path = ROOT / "sql" / "core"
    apply_ddl: bool = True


@dataclass(frozen=True)
class CoreSyncResult:
    """已成功提交的 core batch 統計。"""

    load_batch_id: str
    status: str
    source_rows: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_applied: int
    attempt_count: int
    validation_summary: dict[str, object]
    started_at: str
    completed_at: str


def _psql_config(config: CoreConfig) -> PsqlConfig:
    """將 core 設定轉成不含資料層語意的共用 psql 設定。"""
    return PsqlConfig(
        database=config.database,
        psql_path=config.psql_path,
        # 003 的 client-side \copy 使用專案相對路徑，必須固定在根目錄執行。
        workdir=ROOT,
    )


def _run_psql(
    config: CoreConfig,
    *,
    sql: str | None = None,
    sql_file: Path | None = None,
    variables: Mapping[str, str] | None = None,
    tuples_only: bool = False,
) -> str:
    """套用 core 錯誤型別後呼叫共用 PostgreSQL 執行層。"""
    try:
        return run_psql(
            _psql_config(config),
            sql=sql,
            sql_file=sql_file,
            variables=variables,
            tuples_only=tuples_only,
        )
    except PostgreSQLExecutionError as error:
        raise CoreSyncError(str(error)) from error


def _required_sql_files(
    config: CoreConfig,
    names: tuple[str, ...],
) -> tuple[Path, ...]:
    """依 allowlist 解析 SQL，缺少任何一檔都在連線前停止。"""
    files = tuple(config.sql_dir / name for name in names)
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "required core SQL not found: " + ", ".join(missing)
        )
    return files


def apply_core_ddl(config: CoreConfig) -> None:
    """只建立日常 core loader 必要且可重跑的 schema／table／index。"""
    for sql_file in _required_sql_files(config, CORE_DDL_FILES):
        _run_psql(config, sql_file=sql_file)


def initialize_core(config: CoreConfig) -> tuple[str, ...]:
    """明確執行 core 維度初始化、完整 fact UPSERT 與全量稽核。"""
    sql_files = _required_sql_files(config, CORE_INITIALIZATION_FILES)
    for sql_file in sql_files:
        _run_psql(config, sql_file=sql_file)
    return tuple(path.name for path in sql_files)


def _validate_load_batch_id(load_batch_id: str) -> str:
    """拒絕空白或不合理長度的 batch ID；內容由 psql literal 再安全引用。"""
    normalized = load_batch_id.strip()
    if not normalized:
        raise ValueError("load_batch_id must not be empty")
    if normalized != load_batch_id:
        raise ValueError("load_batch_id must not have surrounding whitespace")
    if len(normalized) > 255:
        raise ValueError("load_batch_id must not exceed 255 characters")
    return normalized


def _read_batch_state(
    config: CoreConfig,
    load_batch_id: str,
) -> tuple[str, str | None]:
    """讀取 staging 成功狀態與目前 core 狀態，不自行猜測最新批次。"""
    sql = f"""
SELECT
    load_run.status,
    COALESCE(sync_run.status, 'not_started')
FROM staging.load_runs AS load_run
LEFT JOIN core.sync_runs AS sync_run
    USING (load_batch_id)
WHERE load_run.load_batch_id = {sql_literal(load_batch_id)};
"""
    output = _run_psql(config, sql=sql, tuples_only=True)
    if not output:
        raise CoreSyncError(f"staging load batch does not exist: {load_batch_id}")

    parts = output.split("\t", 1)
    if len(parts) != 2:
        raise CoreSyncError(f"unexpected batch state format: {output}")
    return parts[0], None if parts[1] == "not_started" else parts[1]


def _claim_sync_run(config: CoreConfig, load_batch_id: str) -> bool:
    """以單一原子指令建立首次 running，或把 failed 重啟為 running。"""
    sql = f"""
WITH eligible_batch AS (
    SELECT load_batch_id
    FROM staging.load_runs
    WHERE load_batch_id = {sql_literal(load_batch_id)}
      AND status = 'success'
),
claimed AS (
    INSERT INTO core.sync_runs AS sync_run (
        load_batch_id,
        status,
        attempt_count
    )
    SELECT
        load_batch_id,
        'running',
        1
    FROM eligible_batch
    ON CONFLICT (load_batch_id)
    DO UPDATE SET
        status = 'running',
        source_rows = NULL,
        rows_inserted = NULL,
        rows_updated = NULL,
        rows_unchanged = NULL,
        rows_applied = NULL,
        attempt_count = sync_run.attempt_count + 1,
        validation_summary = NULL,
        error_message = NULL,
        started_at = clock_timestamp(),
        completed_at = NULL
    WHERE sync_run.status = 'failed'
    RETURNING load_batch_id
)
SELECT load_batch_id
FROM claimed;
"""
    return bool(_run_psql(config, sql=sql, tuples_only=True))


def _record_failed_sync(
    config: CoreConfig,
    load_batch_id: str,
    error: Exception,
) -> None:
    """以獨立指令保存失敗；記錄失敗本身不能遮蔽原始錯誤。"""
    message = str(error)[-8000:] or type(error).__name__
    sql = f"""
INSERT INTO core.sync_runs AS sync_run (
    load_batch_id,
    status,
    attempt_count,
    error_message,
    completed_at
) VALUES (
    {sql_literal(load_batch_id)},
    'failed',
    1,
    {sql_literal(message)},
    clock_timestamp()
)
ON CONFLICT (load_batch_id)
DO UPDATE SET
    status = 'failed',
    source_rows = NULL,
    rows_inserted = NULL,
    rows_updated = NULL,
    rows_unchanged = NULL,
    rows_applied = NULL,
    validation_summary = NULL,
    error_message = EXCLUDED.error_message,
    completed_at = EXCLUDED.completed_at
WHERE sync_run.status = 'running';
"""
    try:
        _run_psql(config, sql=sql)
    except Exception:
        pass


def _read_sync_result(
    config: CoreConfig,
    load_batch_id: str,
) -> CoreSyncResult:
    """只相信資料庫中已成功提交的 core.sync_runs 結果。"""
    sql = f"""
SELECT
    load_batch_id,
    status,
    source_rows,
    rows_inserted,
    rows_updated,
    rows_unchanged,
    rows_applied,
    attempt_count,
    validation_summary::TEXT,
    started_at::TEXT,
    completed_at::TEXT
FROM core.sync_runs
WHERE load_batch_id = {sql_literal(load_batch_id)}
  AND status = 'success';
"""
    output = _run_psql(config, sql=sql, tuples_only=True)
    if not output:
        raise CoreSyncError(f"successful core result not found: {load_batch_id}")

    parts = output.split("\t", 10)
    if len(parts) != 11:
        raise CoreSyncError(f"unexpected core result format: {output}")
    return CoreSyncResult(
        load_batch_id=parts[0],
        status=parts[1],
        source_rows=int(parts[2]),
        rows_inserted=int(parts[3]),
        rows_updated=int(parts[4]),
        rows_unchanged=int(parts[5]),
        rows_applied=int(parts[6]),
        attempt_count=int(parts[7]),
        validation_summary=json.loads(parts[8]),
        started_at=parts[9],
        completed_at=parts[10],
    )


def sync_core_batch(
    config: CoreConfig,
    load_batch_id: str,
) -> CoreSyncResult:
    """將一個明確且成功的 staging batch 冪等同步到 core。"""
    load_batch_id = _validate_load_batch_id(load_batch_id)
    if config.apply_ddl:
        apply_core_ddl(config)

    staging_status, sync_status = _read_batch_state(config, load_batch_id)
    if staging_status != "success":
        raise CoreSyncError(
            f"staging load batch {load_batch_id} has status {staging_status!r}; "
            "expected 'success'"
        )
    if sync_status == "success":
        return _read_sync_result(config, load_batch_id)
    if sync_status == "running":
        raise CoreSyncError(f"core batch is already running: {load_batch_id}")
    if sync_status not in {None, "failed"}:
        raise CoreSyncError(
            f"unsupported core batch status for {load_batch_id}: {sync_status}"
        )

    if not _claim_sync_run(config, load_batch_id):
        # 另一個程序可能剛好先取得批次；重新讀取真實狀態後再決定。
        _, current_status = _read_batch_state(config, load_batch_id)
        if current_status == "success":
            return _read_sync_result(config, load_batch_id)
        raise CoreSyncError(
            f"core batch could not be claimed; current status is "
            f"{current_status!r}: {load_batch_id}"
        )

    batch_sql = config.sql_dir / "012_upsert_core_batch.sql"
    if not batch_sql.is_file():
        error = FileNotFoundError(f"required core SQL not found: {batch_sql}")
        _record_failed_sync(config, load_batch_id, error)
        raise error

    try:
        _run_psql(
            config,
            sql_file=batch_sql,
            variables={"load_batch_id": load_batch_id},
            tuples_only=True,
        )
    except Exception as error:
        _record_failed_sync(config, load_batch_id, error)
        raise

    return _read_sync_result(config, load_batch_id)
