"""更新並驗證 PostgreSQL 分析層的三種資料粒度。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from src.database.postgres import (
    CROSS_LAYER_PIPELINE_LOCK_NAME,
    PostgreSQLExecutionError,
    PsqlConfig,
    hold_advisory_lock,
    run_psql,
    sql_literal,
)


ROOT = Path(__file__).resolve().parents[2]
ANALYTICS_DDL_FILES = (
    "001_create_national_monthly_kpi.sql",
    "005_create_city_monthly_kpi.sql",
    "009_create_district_rolling_3m.sql",
    "013_create_refresh_runs.sql",
)

FULL_REFRESH_FILES = (
    ("national", "002_refresh_national_monthly_kpi_full.sql"),
    ("city", "006_refresh_city_monthly_kpi_full.sql"),
    ("district", "010_refresh_district_rolling_3m_full.sql"),
)

INCREMENTAL_REFRESH_FILES = (
    ("national", "003_refresh_national_monthly_kpi_incremental.sql"),
    ("city", "007_refresh_city_monthly_kpi_incremental.sql"),
    ("district", "011_refresh_district_rolling_3m_incremental.sql"),
)

VALIDATION_FILES = (
    ("national", "004_validate_national_monthly_kpi.sql"),
    ("city", "008_validate_city_monthly_kpi.sql"),
    ("district", "012_validate_district_rolling_3m.sql"),
)

FULL_EXECUTION_FILES = (
    ("refresh", "national", "002_refresh_national_monthly_kpi_full.sql"),
    ("validate", "national", "004_validate_national_monthly_kpi.sql"),
    ("refresh", "city", "006_refresh_city_monthly_kpi_full.sql"),
    ("validate", "city", "008_validate_city_monthly_kpi.sql"),
    ("refresh", "district", "010_refresh_district_rolling_3m_full.sql"),
    ("validate", "district", "012_validate_district_rolling_3m.sql"),
)


class AnalyticsRefreshError(PostgreSQLExecutionError):
    """分析層的輸入、執行或驗證不符合契約。"""


@dataclass(frozen=True)
class AnalyticsConfig:
    """一次分析層操作使用的資料庫與 SQL 設定。"""

    database: str = "real_estate_dashboard"
    psql_path: str = "psql"
    sql_dir: Path = ROOT / "sql" / "analytics"
    apply_ddl: bool = True
    lock_name: str = CROSS_LAYER_PIPELINE_LOCK_NAME


@dataclass(frozen=True)
class AnalyticsRefreshResult:
    """已成功提交的分析層更新結果。"""

    analytics_run_id: str
    refresh_mode: str
    load_batch_id: str | None
    status: str
    attempt_count: int
    refresh_summary: dict[str, object]
    validation_summary: dict[str, object]
    started_at: str
    completed_at: str


def _psql_config(config: AnalyticsConfig) -> PsqlConfig:
    return PsqlConfig(
        database=config.database,
        psql_path=config.psql_path,
        workdir=ROOT,
    )


def _run_psql(
    config: AnalyticsConfig,
    *,
    sql: str | None = None,
    sql_file: Path | None = None,
    variables: Mapping[str, str] | None = None,
    tuples_only: bool = False,
) -> str:
    try:
        return run_psql(
            _psql_config(config),
            sql=sql,
            sql_file=sql_file,
            variables=variables,
            tuples_only=tuples_only,
        )
    except PostgreSQLExecutionError as error:
        raise AnalyticsRefreshError(str(error)) from error


def _required_sql_files(
    config: AnalyticsConfig,
    names: tuple[str, ...],
) -> tuple[Path, ...]:
    files = tuple(config.sql_dir / name for name in names)
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "required Analytics SQL not found: " + ", ".join(missing)
        )
    return files


def apply_analytics_ddl(config: AnalyticsConfig) -> None:
    """只建立更新流程需要且可重跑的資料表與索引。"""
    for sql_file in _required_sql_files(config, ANALYTICS_DDL_FILES):
        _run_psql(config, sql_file=sql_file)


def _validate_load_batch_id(load_batch_id: str) -> str:
    normalized = load_batch_id.strip()
    if not normalized:
        raise ValueError("load_batch_id must not be empty")
    if normalized != load_batch_id:
        raise ValueError("load_batch_id must not have surrounding whitespace")
    if len(normalized) > 255:
        raise ValueError("load_batch_id must not exceed 255 characters")
    return normalized


def _row(
    output: str,
    expected_columns: int,
    label: str,
    *,
    nullable_trailing_columns: int = 0,
) -> list[str]:
    lines = output.splitlines()
    if len(lines) != 1:
        raise AnalyticsRefreshError(
            f"unexpected {label} output row count: {len(lines)}"
        )
    fields = lines[0].split("\t")
    minimum_columns = expected_columns - nullable_trailing_columns
    if minimum_columns <= len(fields) < expected_columns:
        # `run_psql` 會移除尾端空白；最後一欄若為 NULL，代表它的 tab 也會消失。
        fields.extend([""] * (expected_columns - len(fields)))
    if len(fields) != expected_columns:
        raise AnalyticsRefreshError(
            f"unexpected {label} output column count: {len(fields)}"
        )
    return fields


def _integer(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise AnalyticsRefreshError(
            f"expected integer for {label}, found {value!r}"
        ) from error


def _date_array(value: str) -> list[str]:
    if not value:
        return []
    if not (value.startswith("{") and value.endswith("}")):
        raise AnalyticsRefreshError(f"unexpected PostgreSQL date array: {value!r}")
    body = value[1:-1]
    return [] if not body else body.split(",")


def _parse_refresh_summary(mode: str, grain: str, output: str) -> dict[str, object]:
    if mode == "full" and grain == "national":
        fields = _row(output, 5, "national full refresh")
        return {
            "analysis_start_month": fields[0],
            "baseline_transaction_count": fields[1],
            "tail_volume_threshold": fields[2],
            "analysis_end_month": fields[3],
            "refreshed_month_count": _integer(fields[4], "refreshed_month_count"),
        }
    if mode == "full" and grain == "city":
        fields = _row(output, 5, "city full refresh")
        return {
            "analysis_start_month": fields[0],
            "analysis_end_month": fields[1],
            "refreshed_month_count": _integer(fields[2], "refreshed_month_count"),
            "refreshed_county_count": _integer(fields[3], "refreshed_county_count"),
            "refreshed_row_count": _integer(fields[4], "refreshed_row_count"),
        }
    if mode == "full" and grain == "district":
        fields = _row(output, 5, "district full refresh")
        return {
            "first_anchor_month": fields[0],
            "published_end_month": fields[1],
            "location_count": _integer(fields[2], "location_count"),
            "anchor_month_count": _integer(fields[3], "anchor_month_count"),
            "refreshed_row_count": _integer(fields[4], "refreshed_row_count"),
        }

    if grain == "national":
        fields = _row(
            output,
            10,
            "national incremental refresh",
            nullable_trailing_columns=2,
        )
        return {
            "load_batch_id": fields[0],
            "baseline_transaction_count": fields[1],
            "tail_volume_threshold": fields[2],
            "candidate_end_month": fields[3] or None,
            "current_end_month": fields[4] or None,
            "effective_end_month": fields[5] or None,
            "affected_base_month_count": _integer(fields[6], "affected_base_month_count"),
            "affected_yoy_month_count": _integer(fields[7], "affected_yoy_month_count"),
            "affected_base_months": _date_array(fields[8]),
            "affected_yoy_months": _date_array(fields[9]),
        }
    if grain == "city":
        fields = _row(
            output,
            8,
            "city incremental refresh",
            nullable_trailing_columns=2,
        )
        return {
            "load_batch_id": fields[0],
            "current_end_month": fields[1] or None,
            "published_end_month": fields[2] or None,
            "county_count": _integer(fields[3], "county_count"),
            "affected_base_month_count": _integer(fields[4], "affected_base_month_count"),
            "affected_yoy_month_count": _integer(fields[5], "affected_yoy_month_count"),
            "affected_base_months": _date_array(fields[6]),
            "affected_yoy_months": _date_array(fields[7]),
        }
    if grain == "district":
        fields = _row(
            output,
            8,
            "district incremental refresh",
            nullable_trailing_columns=2,
        )
        return {
            "load_batch_id": fields[0],
            "current_end_month": fields[1] or None,
            "published_end_month": fields[2] or None,
            "location_count": _integer(fields[3], "location_count"),
            "affected_base_anchor_count": _integer(fields[4], "affected_base_anchor_count"),
            "affected_yoy_anchor_count": _integer(fields[5], "affected_yoy_anchor_count"),
            "affected_base_anchors": _date_array(fields[6]),
            "affected_yoy_anchors": _date_array(fields[7]),
        }
    raise AssertionError(f"unsupported refresh parser: {mode}/{grain}")


def _zero_fields(fields: list[str], label: str) -> None:
    if any(_integer(value, label) != 0 for value in fields):
        raise AnalyticsRefreshError(f"{label} failed: {fields}")


def _parse_national_validation(output: str, *, mode: str) -> dict[str, object]:
    lines = output.splitlines()
    if len(lines) != 4:
        raise AnalyticsRefreshError(
            f"national validation returned {len(lines)} rows; expected 4 summary rows"
        )
    cutoff = lines[0].split("\t")
    coverage = lines[1].split("\t")
    base = lines[2].split("\t")
    yoy = lines[3].split("\t")
    if len(cutoff) != 8 or len(coverage) != 4 or len(base) != 6 or len(yoy) != 5:
        raise AnalyticsRefreshError("unexpected national validation output shape")
    allowed = {"PASS_ALIGNED"}
    if mode == "incremental":
        allowed.add("REVIEW_CURRENT_AHEAD_OF_CANDIDATE")
    if cutoff[7] not in allowed:
        raise AnalyticsRefreshError(f"national cutoff validation failed: {cutoff[7]}")
    if coverage[0] != coverage[1]:
        raise AnalyticsRefreshError(f"national coverage count mismatch: {coverage}")
    _zero_fields(coverage[2:], "national coverage")
    _zero_fields(base, "national base KPI validation")
    _zero_fields(yoy, "national YoY validation")
    return {
        "cutoff_status": cutoff[7],
        "candidate_end_month": cutoff[4] or None,
        "current_end_month": cutoff[5] or None,
        "expected_month_count": _integer(coverage[0], "expected_month_count"),
        "mismatch_count": 0,
    }


def _parse_city_validation(output: str) -> dict[str, object]:
    lines = output.splitlines()
    if len(lines) != 6:
        raise AnalyticsRefreshError(
            f"city validation returned {len(lines)} rows; expected 6 summary rows"
        )
    range_row = lines[0].split("\t")
    mapping = lines[1].split("\t")
    spine = lines[2].split("\t")
    base = lines[3].split("\t")
    yoy = lines[4].split("\t")
    cross_grain = lines[5].split("\t")
    if len(range_row) != 9 or len(mapping) != 2 or len(spine) != 9:
        raise AnalyticsRefreshError("unexpected city validation output shape")
    if len(base) != 7 or len(yoy) != 5 or len(cross_grain) != 1:
        raise AnalyticsRefreshError("unexpected city validation metric shape")
    if range_row[-1] != "PASS_ALIGNED" or mapping[1] != "PASS_MAPPING":
        raise AnalyticsRefreshError(
            f"city range/mapping validation failed: {range_row[-1]}, {mapping[1]}"
        )
    if spine[0] != spine[1] or spine[2] != spine[3] or spine[4] != spine[5]:
        raise AnalyticsRefreshError(f"city spine count mismatch: {spine}")
    _zero_fields(spine[6:], "city spine validation")
    _zero_fields(base, "city base KPI validation")
    _zero_fields(yoy, "city YoY validation")
    _zero_fields(cross_grain, "national/city count validation")
    return {
        "range_status": range_row[-1],
        "county_mapping_status": mapping[1],
        "county_member_count": _integer(mapping[0], "county_member_count"),
        "expected_row_count": _integer(spine[0], "expected_row_count"),
        "mismatch_count": 0,
    }


def _parse_district_validation(output: str) -> dict[str, object]:
    lines = output.splitlines()
    if len(lines) != 3:
        raise AnalyticsRefreshError(
            f"district validation returned {len(lines)} rows; expected 3 summary rows"
        )
    coverage = lines[0].split("\t")
    metrics = lines[1].split("\t")
    cross_grain = lines[2].split("\t")
    if len(coverage) != 13 or len(metrics) != 11 or len(cross_grain) != 1:
        raise AnalyticsRefreshError("unexpected district validation output shape")
    if coverage[2] != coverage[0] or coverage[4] != coverage[1]:
        raise AnalyticsRefreshError(f"district date range mismatch: {coverage[:6]}")
    if coverage[8] != coverage[9]:
        raise AnalyticsRefreshError(f"district row count mismatch: {coverage[8:10]}")
    _zero_fields(coverage[10:], "district spine validation")
    _zero_fields(metrics, "district KPI validation")
    _zero_fields(cross_grain, "city/district count validation")
    return {
        "published_end_month": coverage[3] or None,
        "location_count": _integer(coverage[6], "location_count"),
        "expected_anchor_month_count": _integer(coverage[7], "expected_anchor_month_count"),
        "expected_row_count": _integer(coverage[8], "expected_row_count"),
        "mismatch_count": 0,
    }


def _read_incremental_state(
    config: AnalyticsConfig,
    load_batch_id: str,
) -> tuple[str, str | None]:
    output = _run_psql(
        config,
        sql=f"""
SELECT core_run.status, analytics_run.status
FROM core.sync_runs AS core_run
LEFT JOIN analytics.refresh_runs AS analytics_run
    ON analytics_run.load_batch_id = core_run.load_batch_id
   AND analytics_run.refresh_mode = 'incremental'
WHERE core_run.load_batch_id = {sql_literal(load_batch_id)};
""",
        tuples_only=True,
    )
    if not output:
        raise AnalyticsRefreshError(f"core sync batch does not exist: {load_batch_id}")
    fields = output.split("\t", 1)
    if len(fields) == 1:
        fields.append("")
    return fields[0], fields[1] or None


def _claim_incremental_run(config: AnalyticsConfig, load_batch_id: str) -> str | None:
    proposed_run_id = str(uuid.uuid4())
    output = _run_psql(
        config,
        sql=f"""
WITH claimed AS (
    INSERT INTO analytics.refresh_runs AS refresh_run (
        analytics_run_id, refresh_mode, load_batch_id, status, attempt_count
    ) VALUES (
        {sql_literal(proposed_run_id)}::UUID,
        'incremental',
        {sql_literal(load_batch_id)},
        'running',
        1
    )
    ON CONFLICT (load_batch_id) WHERE refresh_mode = 'incremental'
    DO UPDATE SET
        status = 'running',
        attempt_count = refresh_run.attempt_count + 1,
        refresh_summary = NULL,
        validation_summary = NULL,
        error_message = NULL,
        started_at = clock_timestamp(),
        completed_at = NULL
    WHERE refresh_run.status = 'failed'
    RETURNING analytics_run_id
)
SELECT analytics_run_id FROM claimed;
""",
        tuples_only=True,
    )
    return output or None


def _claim_full_run(config: AnalyticsConfig) -> str:
    analytics_run_id = str(uuid.uuid4())
    _run_psql(
        config,
        sql=f"""
INSERT INTO analytics.refresh_runs (
    analytics_run_id, refresh_mode, status, attempt_count
) VALUES (
    {sql_literal(analytics_run_id)}::UUID, 'full', 'running', 1
);
""",
    )
    return analytics_run_id


def _record_failed_run(
    config: AnalyticsConfig,
    analytics_run_id: str,
    error: Exception,
) -> None:
    message = str(error)[-8000:] or type(error).__name__
    sql = f"""
UPDATE analytics.refresh_runs
SET status = 'failed',
    validation_summary = NULL,
    error_message = {sql_literal(message)},
    completed_at = clock_timestamp()
WHERE analytics_run_id = {sql_literal(analytics_run_id)}::UUID
  AND status = 'running';
"""
    try:
        _run_psql(config, sql=sql)
    except Exception:
        pass


def _record_successful_run(
    config: AnalyticsConfig,
    analytics_run_id: str,
    refresh_summary: dict[str, object],
    validation_summary: dict[str, object],
) -> None:
    refresh_json = json.dumps(refresh_summary, ensure_ascii=False, sort_keys=True)
    validation_json = json.dumps(
        validation_summary, ensure_ascii=False, sort_keys=True
    )
    _run_psql(
        config,
        sql=f"""
UPDATE analytics.refresh_runs
SET status = 'success',
    refresh_summary = {sql_literal(refresh_json)}::JSONB,
    validation_summary = {sql_literal(validation_json)}::JSONB,
    error_message = NULL,
    completed_at = clock_timestamp()
WHERE analytics_run_id = {sql_literal(analytics_run_id)}::UUID
  AND status = 'running';
""",
    )


def _read_result(
    config: AnalyticsConfig,
    *,
    analytics_run_id: str | None = None,
    load_batch_id: str | None = None,
) -> AnalyticsRefreshResult:
    if (analytics_run_id is None) == (load_batch_id is None):
        raise ValueError("provide exactly one Analytics result key")
    predicate = (
        f"analytics_run_id = {sql_literal(analytics_run_id or '')}::UUID"
        if analytics_run_id is not None
        else f"load_batch_id = {sql_literal(load_batch_id or '')}"
    )
    output = _run_psql(
        config,
        sql=f"""
SELECT analytics_run_id::TEXT,
       refresh_mode,
       COALESCE(load_batch_id, ''),
       status,
       attempt_count,
       refresh_summary::TEXT,
       validation_summary::TEXT,
       started_at::TEXT,
       completed_at::TEXT
FROM analytics.refresh_runs
WHERE {predicate}
  AND status = 'success';
""",
        tuples_only=True,
    )
    fields = _row(output, 9, "successful Analytics result")
    return AnalyticsRefreshResult(
        analytics_run_id=fields[0],
        refresh_mode=fields[1],
        load_batch_id=fields[2] or None,
        status=fields[3],
        attempt_count=_integer(fields[4], "attempt_count"),
        refresh_summary=json.loads(fields[5]),
        validation_summary=json.loads(fields[6]),
        started_at=fields[7],
        completed_at=fields[8],
    )


def _execute_refresh(
    config: AnalyticsConfig,
    *,
    analytics_run_id: str,
    mode: str,
    load_batch_id: str | None,
) -> AnalyticsRefreshResult:
    refresh_summary: dict[str, object] = {}
    validation_summary: dict[str, object] = {}
    variables = None if load_batch_id is None else {"load_batch_id": load_batch_id}
    try:
        execution_steps = (
            FULL_EXECUTION_FILES
            if mode == "full"
            else (
                *(
                    ("refresh", grain, filename)
                    for grain, filename in INCREMENTAL_REFRESH_FILES
                ),
                *(
                    ("validate", grain, filename)
                    for grain, filename in VALIDATION_FILES
                ),
            )
        )
        for operation, grain, filename in execution_steps:
            output = _run_psql(
                config,
                sql_file=config.sql_dir / filename,
                variables=variables if operation == "refresh" else None,
                tuples_only=True,
            )
            if operation == "refresh":
                refresh_summary[grain] = _parse_refresh_summary(
                    mode, grain, output
                )
            elif grain == "national":
                validation_summary[grain] = _parse_national_validation(
                    output, mode=mode
                )
            elif grain == "city":
                validation_summary[grain] = _parse_city_validation(output)
            else:
                validation_summary[grain] = _parse_district_validation(output)

        _record_successful_run(
            config,
            analytics_run_id,
            refresh_summary,
            validation_summary,
        )
    except Exception as error:
        _record_failed_run(config, analytics_run_id, error)
        raise

    return _read_result(config, analytics_run_id=analytics_run_id)


def _run_with_lock(config: AnalyticsConfig, *, lock_held: bool):
    return nullcontext() if lock_held else hold_advisory_lock(
        _psql_config(config), config.lock_name
    )


def refresh_analytics_batch(
    config: AnalyticsConfig,
    load_batch_id: str,
    *,
    lock_held: bool = False,
) -> AnalyticsRefreshResult:
    """依一個成功的 core 批次，冪等更新所有分析粒度。"""
    load_batch_id = _validate_load_batch_id(load_batch_id)
    with _run_with_lock(config, lock_held=lock_held):
        if config.apply_ddl:
            apply_analytics_ddl(config)
        core_status, refresh_status = _read_incremental_state(
            config, load_batch_id
        )
        if core_status != "success":
            raise AnalyticsRefreshError(
                f"core sync batch {load_batch_id} has status {core_status!r}; expected 'success'"
            )
        if refresh_status == "success":
            return _read_result(config, load_batch_id=load_batch_id)
        if refresh_status == "running":
            raise AnalyticsRefreshError(
                f"Analytics batch is already running: {load_batch_id}"
            )
        if refresh_status not in {None, "failed"}:
            raise AnalyticsRefreshError(
                f"unsupported Analytics batch status: {refresh_status!r}"
            )
        analytics_run_id = _claim_incremental_run(config, load_batch_id)
        if analytics_run_id is None:
            _, current_status = _read_incremental_state(config, load_batch_id)
            if current_status == "success":
                return _read_result(config, load_batch_id=load_batch_id)
            raise AnalyticsRefreshError(
                f"Analytics batch could not be claimed; current status is {current_status!r}: {load_batch_id}"
            )
        return _execute_refresh(
            config,
            analytics_run_id=analytics_run_id,
            mode="incremental",
            load_batch_id=load_batch_id,
        )


def refresh_analytics_full(
    config: AnalyticsConfig,
    *,
    lock_held: bool = False,
) -> AnalyticsRefreshResult:
    """明確完整重建所有分析粒度；呼叫前應先完成人工確認。"""
    with _run_with_lock(config, lock_held=lock_held):
        if config.apply_ddl:
            apply_analytics_ddl(config)
        analytics_run_id = _claim_full_run(config)
        return _execute_refresh(
            config,
            analytics_run_id=analytics_run_id,
            mode="full",
            load_batch_id=None,
        )
