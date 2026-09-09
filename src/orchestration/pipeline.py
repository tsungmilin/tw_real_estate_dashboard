"""在同一把 PostgreSQL 鎖下依序執行 staging、core 與 analytics。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.analytics.postgres_loader import (
    AnalyticsConfig,
    AnalyticsRefreshResult,
    apply_analytics_ddl,
    refresh_analytics_batch,
)
from src.core.postgres_loader import (
    CoreConfig,
    CoreSyncResult,
    apply_core_ddl,
    sync_core_batch,
)
from src.database.postgres import (
    CROSS_LAYER_PIPELINE_LOCK_NAME,
    PsqlConfig,
    hold_advisory_lock,
    run_psql,
)
from src.loading.postgres_loader import (
    LoadConfig,
    LoadResult,
    apply_staging_ddl,
    load_postgres_staging,
)


ROOT = Path(__file__).resolve().parents[2]
PIPELINE_LOCK_NAME = CROSS_LAYER_PIPELINE_LOCK_NAME


class PipelineBlockedError(RuntimeError):
    """資料庫尚未準備好，或另一條資料流程尚未妥善結束。"""


@dataclass(frozen=True)
class PipelineConfig:
    """一次 staging → core → analytics 執行需要的共同設定。"""

    parquet_path: Path = ROOT / "data" / "processed" / "transactions_clean.parquet"
    database: str = "real_estate_dashboard"
    psql_path: str = "psql"
    apply_ddl: bool = True
    lock_name: str = PIPELINE_LOCK_NAME


@dataclass(frozen=True)
class PipelineResult:
    """一條完整資料流程已成功提交的三層結果。"""

    staging: LoadResult
    core: CoreSyncResult
    analytics: AnalyticsRefreshResult
    recovered_core_batches: tuple[CoreSyncResult, ...] = ()
    recovered_analytics_batches: tuple[AnalyticsRefreshResult, ...] = ()


def _psql_config(config: PipelineConfig) -> PsqlConfig:
    return PsqlConfig(
        database=config.database,
        psql_path=config.psql_path,
        workdir=ROOT,
    )


def _staging_config(config: PipelineConfig, *, apply_ddl: bool) -> LoadConfig:
    return LoadConfig(
        parquet_path=config.parquet_path,
        database=config.database,
        psql_path=config.psql_path,
        apply_ddl=apply_ddl,
    )


def _core_config(config: PipelineConfig, *, apply_ddl: bool) -> CoreConfig:
    return CoreConfig(
        database=config.database,
        psql_path=config.psql_path,
        apply_ddl=apply_ddl,
    )


def _analytics_config(
    config: PipelineConfig,
    *,
    apply_ddl: bool,
) -> AnalyticsConfig:
    return AnalyticsConfig(
        database=config.database,
        psql_path=config.psql_path,
        apply_ddl=apply_ddl,
        lock_name=config.lock_name,
    )


def _assert_core_dimensions_ready(config: PipelineConfig) -> None:
    """在建立新 staging batch 前確認固定維度已完成初始化。"""
    sql = """
SELECT
    (SELECT count(*) FROM core.dim_location),
    (SELECT count(*) FROM core.dim_building_type);
"""
    output = run_psql(_psql_config(config), sql=sql, tuples_only=True)
    if output != "368\t14":
        raise PipelineBlockedError(
            "core dimensions are not initialized: expected 368 locations "
            f"and 14 building types, found {output!r}"
        )


def _unfinished_core_runs(config: PipelineConfig) -> list[tuple[str, str]]:
    """找出已登記但尚未成功的 core batch，防止新的 staging 覆蓋 batch ID。"""
    sql = """
SELECT load_batch_id, status
FROM core.sync_runs
WHERE status IN ('running', 'failed')
ORDER BY started_at, load_batch_id;
"""
    output = run_psql(_psql_config(config), sql=sql, tuples_only=True)
    if not output:
        return []

    result: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise PipelineBlockedError(
                f"unexpected unfinished core batch format: {line}"
            )
        result.append((parts[0], parts[1]))
    return result


def _recover_failed_core_batch(
    config: PipelineConfig,
) -> tuple[CoreSyncResult, ...]:
    """先重試唯一的 failed batch；running 或多個 failed 都需人工確認。"""
    unfinished = _unfinished_core_runs(config)
    running = [batch_id for batch_id, status in unfinished if status == "running"]
    failed = [batch_id for batch_id, status in unfinished if status == "failed"]

    if running:
        raise PipelineBlockedError(
            "core contains a running batch; resolve it before loading new staging "
            f"data: {', '.join(running)}"
        )
    if len(failed) > 1:
        raise PipelineBlockedError(
            "core contains multiple failed batches; preserve batch order and "
            f"resolve them manually: {', '.join(failed)}"
        )
    if not failed:
        return ()

    recovered = sync_core_batch(
        _core_config(config, apply_ddl=False),
        failed[0],
    )
    return (recovered,)


def _unfinished_analytics_runs(
    config: PipelineConfig,
) -> list[tuple[str, str, str]]:
    """讀取未完成 incremental，以及仍是最新狀態的 full 失敗／執行中紀錄。"""
    sql = """
WITH latest_full AS (
    SELECT refresh_mode, COALESCE(load_batch_id, '') AS load_batch_id, status
    FROM analytics.refresh_runs
    WHERE refresh_mode = 'full'
    ORDER BY started_at DESC, analytics_run_id DESC
    LIMIT 1
),
unfinished AS (
    SELECT refresh_mode, COALESCE(load_batch_id, '') AS load_batch_id, status
    FROM analytics.refresh_runs
    WHERE refresh_mode = 'incremental'
      AND status IN ('running', 'failed')

    UNION ALL

    SELECT *
    FROM latest_full
    WHERE status IN ('running', 'failed')
)
SELECT refresh_mode, COALESCE(load_batch_id, ''), status
FROM unfinished
ORDER BY refresh_mode, load_batch_id;
"""
    output = run_psql(_psql_config(config), sql=sql, tuples_only=True)
    if not output:
        return []

    result: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise PipelineBlockedError(
                f"unexpected unfinished Analytics run format: {line}"
            )
        result.append((fields[0], fields[1], fields[2]))
    return result


def _recover_failed_analytics_batch(
    config: PipelineConfig,
) -> tuple[AnalyticsRefreshResult, ...]:
    """先重試唯一 incremental failed；full 失敗與 running 一律阻擋日常流程。"""
    unfinished = _unfinished_analytics_runs(config)
    full = [row for row in unfinished if row[0] == "full"]
    running = [row for row in unfinished if row[2] == "running"]
    failed_incremental = [
        load_batch_id
        for mode, load_batch_id, status in unfinished
        if mode == "incremental" and status == "failed"
    ]

    if full:
        raise PipelineBlockedError(
            "latest full Analytics refresh did not succeed; run and validate a "
            "manual full refresh before the daily pipeline"
        )
    if running:
        labels = [f"{mode}:{batch_id or 'full'}" for mode, batch_id, _ in running]
        raise PipelineBlockedError(
            "Analytics contains a running refresh; resolve it before loading new "
            f"staging data: {', '.join(labels)}"
        )
    if len(failed_incremental) > 1:
        raise PipelineBlockedError(
            "Analytics contains multiple failed batches; preserve batch order and "
            f"resolve them manually: {', '.join(failed_incremental)}"
        )
    if not failed_incremental:
        return ()

    recovered = refresh_analytics_batch(
        _analytics_config(config, apply_ddl=False),
        failed_incremental[0],
        lock_held=True,
    )
    return (recovered,)


def run_pipeline(config: PipelineConfig) -> PipelineResult:
    """在同一把跨層鎖下完成 staging、core 與 analytics 增量更新。"""
    with hold_advisory_lock(_psql_config(config), config.lock_name):
        # 先只套用可重跑的 DDL；完整 core／Analytics rebuild 都是明確命令。
        if config.apply_ddl:
            apply_staging_ddl(_staging_config(config, apply_ddl=False))
            apply_core_ddl(_core_config(config, apply_ddl=False))
            apply_analytics_ddl(_analytics_config(config, apply_ddl=False))

        _assert_core_dimensions_ready(config)
        recovered_analytics = list(_recover_failed_analytics_batch(config))
        recovered_core = _recover_failed_core_batch(config)
        for recovered in recovered_core:
            recovered_analytics.append(
                refresh_analytics_batch(
                    _analytics_config(config, apply_ddl=False),
                    recovered.load_batch_id,
                    lock_held=True,
                )
            )

        staging_result = load_postgres_staging(
            _staging_config(config, apply_ddl=False)
        )
        core_result = sync_core_batch(
            _core_config(config, apply_ddl=False),
            staging_result.load_batch_id,
        )
        analytics_result = refresh_analytics_batch(
            _analytics_config(config, apply_ddl=False),
            staging_result.load_batch_id,
            lock_held=True,
        )
        return PipelineResult(
            staging=staging_result,
            core=core_result,
            analytics=analytics_result,
            recovered_core_batches=recovered_core,
            recovered_analytics_batches=tuple(recovered_analytics),
        )
