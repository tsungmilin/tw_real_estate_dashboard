"""將通過驗證的 clean Parquet 安全同步到 PostgreSQL staging。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from src.cleaning.contract import CLEAN_SCHEMA
from src.database.postgres import (
    PostgreSQLExecutionError,
    PsqlConfig,
    build_psql_args,
    psql_environment,
    run_psql,
    sql_literal,
)


# 專案根目錄用來組合預設資料檔與 SQL 目錄，不依賴執行指令時的工作目錄。
ROOT = Path(__file__).resolve().parents[2]

# CLEAN_SCHEMA 是清理與載入之間唯一的欄位契約；載入器不另行維護欄位清單。
CLEAN_COLUMNS = tuple(field.name for field in CLEAN_SCHEMA)
KEY_COLUMN = "source_transaction_id"

# cleaning_run_id 是載入追蹤資訊，不是交易內容。若把它放進比較清單，每次新的
# cleaning run 都會讓所有交易看起來像被修改過。
BUSINESS_COLUMNS = tuple(
    name
    for name in CLEAN_COLUMNS
    if name not in {KEY_COLUMN, "cleaning_run_id"}
)

# 這些 staging 欄位會直接或間接影響目前的 core model：交易年月形成
# fact_transactions.transaction_month；地區欄位形成 dim_location/location_id；
# 建物、交易類型與價格則直接決定 fact_transactions 的內容。
CORE_IMPACT_COLUMNS = (
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
)

# 一筆交易真的發生變化時，除業務欄位外也要更新它所屬的 cleaning run。
UPDATE_COLUMNS = ("cleaning_run_id", *BUSINESS_COLUMNS)


class PostgreSQLLoadError(PostgreSQLExecutionError):
    """PostgreSQL 指令或載入發布檢核失敗時使用的例外。"""


@dataclass(frozen=True)
class LoadConfig:
    """一次 staging 載入所需的路徑、資料庫與執行選項。"""

    parquet_path: Path = ROOT / "data" / "processed" / "transactions_clean.parquet"
    database: str = "real_estate_dashboard"
    psql_path: str = "psql"
    sql_dir: Path = ROOT / "sql" / "staging"
    apply_ddl: bool = True


@dataclass(frozen=True)
class ParquetSnapshot:
    """Parquet 預檢後取得、且在本次載入期間不應改變的來源資訊。"""

    path: Path
    rows: int
    row_groups: int
    cleaning_run_id: str
    sha256: str


@dataclass(frozen=True)
class LoadResult:
    """成功載入後回傳給命令列或其他呼叫端的批次對帳結果。"""

    load_batch_id: str
    cleaning_run_id: str
    parquet_rows: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_deleted: int
    target_rows: int
    validation_summary: dict[str, object]


def _sha256_file(path: Path) -> str:
    """分段計算檔案 SHA-256，避免把大型 Parquet 一次讀入記憶體。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        # 8 MiB 只控制每次讀取量，不會改變 checksum 結果。
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_clean_parquet(path: Path) -> ParquetSnapshot:
    """在改動 PostgreSQL 前，驗證 clean Parquet 是否符合發布契約。"""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"clean Parquet not found: {path}")

    # 先讀 Parquet metadata 與 schema；這些檢查不需要載入所有交易欄位。
    parquet = pq.ParquetFile(path)
    if not parquet.schema_arrow.equals(CLEAN_SCHEMA, check_metadata=False):
        raise ValueError(
            "clean Parquet schema does not match Cleaning Specification v1"
        )
    if parquet.metadata.num_rows <= 0:
        raise ValueError("clean Parquet must contain at least one row")

    # 每個正式 clean Parquet 必須只屬於一個 cleaning run。逐 row group 只讀取
    # cleaning_run_id，可避免為了這項檢查 materialize 整份資料。
    run_ids: set[str] = set()
    for row_group in range(parquet.metadata.num_row_groups):
        values = parquet.read_row_group(
            row_group,
            columns=["cleaning_run_id"],
        )["cleaning_run_id"]
        run_ids.update(value for value in values.unique().to_pylist() if value)
        if len(run_ids) > 1:
            break
    if len(run_ids) != 1:
        raise ValueError(
            f"expected exactly one cleaning_run_id in Parquet, found {sorted(run_ids)}"
        )

    # checksum 只用來識別來源內容；每次載入嘗試另由 UUID 產生 batch ID。
    return ParquetSnapshot(
        path=path,
        rows=parquet.metadata.num_rows,
        row_groups=parquet.metadata.num_row_groups,
        cleaning_run_id=next(iter(run_ids)),
        sha256=_sha256_file(path),
    )


def _psql_config(config: LoadConfig) -> PsqlConfig:
    """將 staging 設定轉成不含資料層語意的共用 psql 設定。"""
    return PsqlConfig(
        database=config.database,
        psql_path=config.psql_path,
        workdir=ROOT,
    )


def _psql_args(config: LoadConfig, *extra: str) -> list[str]:
    """建立 staging 串流程序使用的共用 psql 參數。"""
    return build_psql_args(_psql_config(config), *extra)


def _run_psql(
    config: LoadConfig,
    *,
    sql: str | None = None,
    sql_file: Path | None = None,
    tuples_only: bool = False,
) -> str:
    """套用 staging 錯誤型別後呼叫共用 PostgreSQL 執行層。"""
    try:
        return run_psql(
            _psql_config(config),
            sql=sql,
            sql_file=sql_file,
            tuples_only=tuples_only,
        )
    except PostgreSQLExecutionError as error:
        raise PostgreSQLLoadError(str(error)) from error


def apply_staging_ddl(config: LoadConfig) -> None:
    """依明確順序套用目前 loader 所需、可重跑的 staging DDL。"""
    # 使用 allowlist 而不是掃描整個目錄，避免尚未納入 loader 流程的新 SQL
    # 因檔名排序而被意外執行。
    required = [
        config.sql_dir / "001_create_schema.sql",
        config.sql_dir / "002_create_stg_transactions.sql",
        config.sql_dir / "003_create_load_runs.sql",
        config.sql_dir / "004_validate_load.sql",
        config.sql_dir / "005_create_load_month_changes.sql",
        config.sql_dir / "006_validate_upsert_load.sql",
    ]
    for sql_file in required:
        if not sql_file.is_file():
            raise FileNotFoundError(f"required staging SQL not found: {sql_file}")
        _run_psql(config, sql_file=sql_file)


def build_sync_sql(snapshot: ParquetSnapshot, load_batch_id: str) -> tuple[str, str]:
    """建立 psql COPY 資料串流前後使用的兩段 SQL。"""
    # 欄位名稱來自 CLEAN_SCHEMA；集中產生可確保 COPY、比較與 UPSERT 使用相同順序。
    column_list = ",\n        ".join(CLEAN_COLUMNS)
    select_columns = ", ".join(CLEAN_COLUMNS)
    select_buffer_columns = ",\n        ".join(
        f"buffer.{name}" for name in CLEAN_COLUMNS
    )
    compare_target = ", ".join(f"target.{name}" for name in BUSINESS_COLUMNS)
    compare_buffer = ", ".join(f"buffer.{name}" for name in BUSINESS_COLUMNS)
    compare_excluded = ", ".join(
        f"EXCLUDED.{name}" for name in BUSINESS_COLUMNS
    )
    core_target = ", ".join(f"target.{name}" for name in CORE_IMPACT_COLUMNS)
    core_buffer = ", ".join(f"buffer.{name}" for name in CORE_IMPACT_COLUMNS)
    update_assignments = ",\n        ".join(
        f"{name} = EXCLUDED.{name}" for name in UPDATE_COLUMNS
    )
    batch_literal = sql_literal(load_batch_id)
    run_literal = sql_literal(snapshot.cleaning_run_id)

    # prefix 先開始 database transaction、取得 transaction-level advisory lock，
    # 再建立與正式 staging 同欄位的臨時 buffer。\copy 指令保持開啟，等待 Python
    # 把各個 Parquet row group 轉成 CSV byte stream 後送入。
    prefix = f"""\
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('staging.stg_transactions.snapshot'));

CREATE TEMP TABLE load_buffer ON COMMIT DROP AS
SELECT {select_columns}
FROM staging.stg_transactions
WITH NO DATA;

\\copy load_buffer ({select_columns}) FROM STDIN WITH (FORMAT csv, HEADER false, ENCODING 'UTF8')
"""

    # suffix 在 COPY 完成後執行，依序：
    # 1. 驗證 buffer 筆數、ID、年月與 cleaning run。
    # 2. 只做一次 LEFT JOIN，將每筆 incoming 分成 inserted/updated/unchanged。
    # 3. 使用同一份分類結果計算統計、記錄受影響月份並 UPSERT。
    # 4. 執行 UPSERT-only 專用對帳、更新 load_runs，最後 COMMIT。
    # 未出現在本次 Parquet 的 staging 舊資料不會被計數或刪除。
    suffix = f"""\
DO $parquet_buffer_gate$
DECLARE
    v_rows BIGINT;
    v_nonnull_ids BIGINT;
    v_cleaning_run_rows BIGINT;
    v_valid_period_rows BIGINT;
BEGIN
    SELECT
        count(*),
        count(source_transaction_id),
        count(*) FILTER (WHERE cleaning_run_id = {run_literal}),
        count(*) FILTER (
            WHERE transaction_year BETWEEN 2012 AND 2024
              AND transaction_month BETWEEN 1 AND 12
        )
    INTO
        v_rows,
        v_nonnull_ids,
        v_cleaning_run_rows,
        v_valid_period_rows
    FROM load_buffer;

    IF v_rows <> {snapshot.rows}
       OR v_nonnull_ids <> {snapshot.rows}
       OR v_cleaning_run_rows <> {snapshot.rows}
       OR v_valid_period_rows <> {snapshot.rows} THEN
        RAISE EXCEPTION
            'Parquet buffer validation failed: rows %, nonnull IDs %, cleaning-run rows %, valid-period rows %, expected %',
            v_rows,
            v_nonnull_ids,
            v_cleaning_run_rows,
            v_valid_period_rows,
            {snapshot.rows};
    END IF;
END
$parquet_buffer_gate$;

CREATE UNIQUE INDEX load_buffer_source_transaction_id_idx
    ON load_buffer (source_transaction_id);
ANALYZE load_buffer;

-- 這是本次唯一一次 incoming 與現有 staging 的完整比對。分類表刻意只保存
-- 主鍵、變更類型、月份與 core impact，避免再複製所有寬欄位。
CREATE TEMP TABLE load_classification ON COMMIT DROP AS
SELECT
    buffer.source_transaction_id,
    CASE
        WHEN target.source_transaction_id IS NULL THEN 'inserted'
        WHEN ROW({compare_target}) IS DISTINCT FROM ROW({compare_buffer})
            THEN 'updated'
        ELSE 'unchanged'
    END AS change_type,
    CASE
        WHEN target.source_transaction_id IS NULL THEN NULL
        ELSE MAKE_DATE(target.transaction_year, target.transaction_month, 1)
    END AS old_month,
    MAKE_DATE(buffer.transaction_year, buffer.transaction_month, 1) AS new_month,
    CASE
        WHEN target.source_transaction_id IS NULL THEN TRUE
        ELSE ROW({core_target}) IS DISTINCT FROM ROW({core_buffer})
    END AS has_core_impact
FROM load_buffer AS buffer
LEFT JOIN staging.stg_transactions AS target
    USING (source_transaction_id);

CREATE UNIQUE INDEX load_classification_source_transaction_id_idx
    ON load_classification (source_transaction_id);
ANALYZE load_classification;

CREATE TEMP TABLE load_stats ON COMMIT DROP AS
SELECT
    count(*) FILTER (WHERE change_type = 'inserted')::BIGINT
        AS rows_inserted,
    count(*) FILTER (WHERE change_type = 'updated')::BIGINT
        AS rows_updated,
    count(*) FILTER (WHERE change_type = 'unchanged')::BIGINT
        AS rows_unchanged,
    count(*) FILTER (
        WHERE change_type = 'updated'
          AND old_month IS DISTINCT FROM new_month
    )::BIGINT AS rows_moved,
    (SELECT count(*) FROM staging.stg_transactions)::BIGINT
        AS target_before
FROM load_classification;

-- 新增交易進入新月份；月份移動同時離開舊月份、進入新月份；同月份更新
-- 則只計為 changed。BOOL_OR 讓同月份只要有一筆 core 變更就標記 TRUE。
CREATE TEMP TABLE month_write_stats ON COMMIT DROP AS
WITH month_events AS (
    SELECT
        event.month_start,
        event.rows_entered,
        event.rows_changed,
        event.rows_exited,
        event.has_core_impact
    FROM load_classification AS classification
    CROSS JOIN LATERAL (
        VALUES
            (
                CASE
                    WHEN classification.change_type IN ('inserted', 'updated')
                        THEN classification.new_month
                    ELSE NULL
                END,
                CASE
                    WHEN classification.change_type = 'inserted'
                      OR (
                          classification.change_type = 'updated'
                          AND classification.old_month
                              IS DISTINCT FROM classification.new_month
                      )
                        THEN 1::BIGINT
                    ELSE 0::BIGINT
                END,
                CASE
                    WHEN classification.change_type = 'updated'
                     AND classification.old_month
                         IS NOT DISTINCT FROM classification.new_month
                        THEN 1::BIGINT
                    ELSE 0::BIGINT
                END,
                0::BIGINT,
                CASE
                    WHEN classification.change_type = 'inserted'
                      OR classification.old_month
                          IS DISTINCT FROM classification.new_month
                        THEN TRUE
                    ELSE classification.has_core_impact
                END
            ),
            (
                CASE
                    WHEN classification.change_type = 'updated'
                     AND classification.old_month
                         IS DISTINCT FROM classification.new_month
                        THEN classification.old_month
                    ELSE NULL
                END,
                0::BIGINT,
                0::BIGINT,
                CASE
                    WHEN classification.change_type = 'updated'
                     AND classification.old_month
                         IS DISTINCT FROM classification.new_month
                        THEN 1::BIGINT
                    ELSE 0::BIGINT
                END,
                TRUE
            )
    ) AS event(
        month_start,
        rows_entered,
        rows_changed,
        rows_exited,
        has_core_impact
    )
    WHERE event.month_start IS NOT NULL
),
inserted_month_changes AS (
    INSERT INTO staging.load_month_changes (
        load_batch_id,
        month_start,
        rows_entered,
        rows_changed,
        rows_exited,
        has_core_impact
    )
    SELECT
        {batch_literal},
        month_start,
        sum(rows_entered)::BIGINT,
        sum(rows_changed)::BIGINT,
        sum(rows_exited)::BIGINT,
        bool_or(has_core_impact)
    FROM month_events
    GROUP BY month_start
    RETURNING rows_entered, rows_changed, rows_exited
)
SELECT
    COALESCE(sum(rows_entered), 0)::BIGINT AS rows_entered,
    COALESCE(sum(rows_changed), 0)::BIGINT AS rows_changed,
    COALESCE(sum(rows_exited), 0)::BIGINT AS rows_exited
FROM inserted_month_changes;

CREATE TEMP TABLE apply_stats ON COMMIT DROP AS
WITH applied_rows AS (
    INSERT INTO staging.stg_transactions AS target (
        {column_list},
        load_batch_id,
        loaded_at
    )
    SELECT
        {select_buffer_columns},
        {batch_literal},
        clock_timestamp()
    FROM load_buffer AS buffer
    JOIN load_classification AS classification
        USING (source_transaction_id)
    WHERE classification.change_type IN ('inserted', 'updated')
    ON CONFLICT (source_transaction_id) DO UPDATE
    SET
        {update_assignments},
        load_batch_id = EXCLUDED.load_batch_id,
        loaded_at = EXCLUDED.loaded_at
    WHERE ROW({compare_target}) IS DISTINCT FROM ROW({compare_excluded})
    RETURNING source_transaction_id
)
SELECT count(*)::BIGINT AS rows_applied
FROM applied_rows;

CREATE TEMP TABLE validation_result ON COMMIT DROP AS
SELECT
    staging.validate_transaction_upsert(
        {snapshot.rows},
        stats.target_before,
        stats.rows_inserted,
        stats.rows_updated,
        stats.rows_unchanged,
        applied.rows_applied,
        stats.rows_moved,
        months.rows_entered,
        months.rows_changed,
        months.rows_exited
    ) AS summary,
    stats.target_before + stats.rows_inserted AS target_rows
FROM load_stats AS stats
CROSS JOIN apply_stats AS applied
CROSS JOIN month_write_stats AS months;

UPDATE staging.load_runs AS runs
SET
    rows_inserted = stats.rows_inserted,
    rows_updated = stats.rows_updated,
    rows_unchanged = stats.rows_unchanged,
    rows_deleted = 0,
    target_rows = validation.target_rows,
    status = 'success',
    validation_summary = validation.summary,
    completed_at = clock_timestamp()
FROM load_stats AS stats
CROSS JOIN validation_result AS validation
WHERE runs.load_batch_id = {batch_literal};

COMMIT;
"""
    return prefix, suffix


def _stream_snapshot(
    config: LoadConfig,
    snapshot: ParquetSnapshot,
    load_batch_id: str,
) -> None:
    """以 row group 為單位將 Parquet 串流送入同一個 psql session。"""
    prefix, suffix = build_sync_sql(snapshot, load_batch_id)
    psql_config = _psql_config(config)
    environment = psql_environment(psql_config)
    # 必須維持同一個 psql process，才能讓 prefix 建立的 transaction 與臨時表
    # 持續存在，直到所有資料與 suffix 都送完。
    process = subprocess.Popen(
        _psql_args(config),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=psql_config.workdir,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise PostgreSQLLoadError("failed to open psql streaming pipes")

    parquet = pq.ParquetFile(snapshot.path)
    try:
        process.stdin.write(prefix.encode("utf-8"))
        options = pacsv.WriteOptions(include_header=False, quoting_style="needed")

        # 每次只 materialize 一個 row group，轉成無標題 CSV 後立即送給 psql，
        # 避免在 Python 記憶體中同時保存全部交易。
        for row_group in range(parquet.metadata.num_row_groups):
            table = parquet.read_row_group(row_group, columns=list(CLEAN_COLUMNS))
            sink = pa.BufferOutputStream()
            pacsv.write_csv(table, sink, write_options=options)
            process.stdin.write(sink.getvalue().to_pybytes())
        process.stdin.write(b"\\.\n")
        process.stdin.write(suffix.encode("utf-8"))
        process.stdin.close()
    except BrokenPipeError:
        # psql 若先因 SQL 或連線問題結束，stdin 會出現 BrokenPipe；真正原因會在下方
        # 統一從 stdout／stderr 讀取，避免用次要錯誤覆蓋主要錯誤訊息。
        pass

    stdout = process.stdout.read().decode("utf-8", errors="replace")
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code != 0:
        detail = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        raise PostgreSQLLoadError(detail)


def _record_failed_load(
    config: LoadConfig,
    load_batch_id: str,
    error: Exception,
) -> None:
    """以 best effort 將失敗原因寫回已建立的 load_runs 紀錄。"""
    # 主載入 transaction 失敗時會 rollback，但 load_runs 的 running 紀錄是在此前
    # 由另一個 psql 呼叫建立，因此可用獨立指令將它更新成 failed。
    message = str(error)[-8000:]
    sql = f"""
UPDATE staging.load_runs
SET status = 'failed',
    error_message = {sql_literal(message)},
    completed_at = clock_timestamp()
WHERE load_batch_id = {sql_literal(load_batch_id)};
"""
    try:
        _run_psql(config, sql=sql)
    except Exception:
        # 記錄失敗本身不能遮蔽原始載入錯誤；呼叫端仍會拋出原本的 exception。
        pass


def _read_load_result(
    config: LoadConfig,
    load_batch_id: str,
) -> LoadResult:
    """讀取成功批次的持久化結果，並轉成型別明確的 LoadResult。"""
    sql = f"""
SELECT
    cleaning_run_id,
    parquet_rows,
    rows_inserted,
    rows_updated,
    rows_unchanged,
    rows_deleted,
    target_rows,
    validation_summary::TEXT
FROM staging.load_runs
WHERE load_batch_id = {sql_literal(load_batch_id)}
  AND status = 'success';
"""
    output = _run_psql(config, sql=sql, tuples_only=True)
    if not output:
        raise PostgreSQLLoadError(f"successful load result not found: {load_batch_id}")
    # tuples-only 查詢使用 tab 作欄位分隔；最後一欄 JSON 只切一次並交給 json.loads。
    parts = output.split("\t", 7)
    if len(parts) != 8:
        raise PostgreSQLLoadError(f"unexpected load result format: {output}")
    return LoadResult(
        load_batch_id=load_batch_id,
        cleaning_run_id=parts[0],
        parquet_rows=int(parts[1]),
        rows_inserted=int(parts[2]),
        rows_updated=int(parts[3]),
        rows_unchanged=int(parts[4]),
        rows_deleted=int(parts[5]),
        target_rows=int(parts[6]),
        validation_summary=json.loads(parts[7]),
    )


def load_postgres_staging(config: LoadConfig) -> LoadResult:
    """執行完整 staging 載入流程，成功時回傳可對帳的批次結果。"""
    # Parquet 預檢刻意放在所有資料庫改動之前，避免無效輸入留下載入批次。
    snapshot = inspect_clean_parquet(config.parquet_path)
    if config.apply_ddl:
        apply_staging_ddl(config)

    # 每次嘗試使用獨立 UUID；來源內容識別仍由 load_runs.source_sha256 負責。
    load_batch_id = f"pgload_{uuid.uuid4().hex}"
    # running 紀錄先以獨立指令持久化；即使後續同步 rollback，也能再標記為 failed。
    start_sql = f"""
INSERT INTO staging.load_runs (
    load_batch_id,
    cleaning_run_id,
    source_file,
    source_sha256,
    parquet_rows,
    status
) VALUES (
    {sql_literal(load_batch_id)},
    {sql_literal(snapshot.cleaning_run_id)},
    {sql_literal(str(snapshot.path))},
    {sql_literal(snapshot.sha256)},
    {snapshot.rows},
    'running'
);
"""
    _run_psql(config, sql=start_sql)

    try:
        _stream_snapshot(config, snapshot, load_batch_id)
    except Exception as error:
        _record_failed_load(config, load_batch_id, error)
        raise

    # 不直接信任記憶體內的統計值；以資料庫中已成功 COMMIT 的 load_runs 為準。
    return _read_load_result(config, load_batch_id)
