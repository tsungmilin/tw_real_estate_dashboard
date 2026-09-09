"""以一致且非互動式的方式執行 PostgreSQL psql 指令。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


_PSQL_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 各資料層的獨立寫入與整體流程共用此 session-level advisory lock，避免並行發布。
CROSS_LAYER_PIPELINE_LOCK_NAME = "taiwan_real_estate_dashboard.staging_to_core"


class PostgreSQLExecutionError(RuntimeError):
    """psql 無法啟動、連線或執行 SQL 時使用的共用例外。"""


@dataclass(frozen=True)
class PsqlConfig:
    """執行 psql 所需的共用連線與程序設定。"""

    database: str = "real_estate_dashboard"
    psql_path: str = "psql"
    workdir: Path | None = None
    connect_timeout_seconds: int = 10


def sql_literal(value: str) -> str:
    """將程式產生的文字安全包成單一 PostgreSQL 字串 literal。"""
    return "'" + value.replace("'", "''") + "'"


def psql_environment(config: PsqlConfig) -> dict[str, str]:
    """複製目前 PG* 環境，並補上有限的連線等待時間。"""
    environment = os.environ.copy()
    environment.setdefault(
        "PGCONNECT_TIMEOUT",
        str(config.connect_timeout_seconds),
    )
    return environment


def build_psql_args(
    config: PsqlConfig,
    *extra: str,
    variables: Mapping[str, str] | None = None,
    tuples_only: bool = False,
) -> list[str]:
    """建立不讀個人設定、遇錯即停且不互動詢問密碼的 psql 參數。"""
    executable = shutil.which(config.psql_path) or config.psql_path
    args = [
        executable,
        "-X",
        "--no-password",
        "--set=ON_ERROR_STOP=1",
        "--dbname",
        config.database,
    ]

    if variables:
        for name, value in variables.items():
            if not _PSQL_VARIABLE_NAME.fullmatch(name):
                raise ValueError(f"invalid psql variable name: {name!r}")
            # name=value 是單一 argv，不經 shell 解讀；SQL 檔仍應使用 :'name'
            # 讓 psql 產生正確的 SQL literal。
            args.append(f"--set={name}={value}")

    if tuples_only:
        # quiet 避免 BEGIN、CREATE TABLE 等 command tag 混入機器解析輸出。
        args.extend(
            [
                "--quiet",
                "--no-align",
                "--tuples-only",
                "--field-separator",
                "\t",
            ]
        )

    args.extend(extra)
    return args


def run_psql(
    config: PsqlConfig,
    *,
    sql: str | None = None,
    sql_file: Path | None = None,
    variables: Mapping[str, str] | None = None,
    tuples_only: bool = False,
) -> str:
    """執行一段 SQL 或單一 SQL 檔，並把失敗轉成一致的例外。"""
    if (sql is None) == (sql_file is None):
        raise ValueError("provide exactly one of sql or sql_file")

    args = build_psql_args(
        config,
        variables=variables,
        tuples_only=tuples_only,
    )
    if sql_file is not None:
        args.extend(["--file", str(sql_file)])
    else:
        args.extend(["--command", sql or ""])

    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=psql_environment(config),
            cwd=config.workdir,
        )
    except OSError as error:
        raise PostgreSQLExecutionError(str(error)) from error

    if completed.returncode != 0:
        # psql 有時把主要原因寫在 stdout，有時寫在 stderr。
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PostgreSQLExecutionError(detail or "psql failed without output")
    return completed.stdout.strip()


def _close_lock_session(
    process: subprocess.Popen[str],
    lock_name: str,
) -> None:
    """結束持有 advisory lock 的 psql；連線中斷也會由 PostgreSQL 自動解鎖。"""
    try:
        if process.stdin is not None and process.poll() is None:
            process.stdin.write(
                "SELECT pg_advisory_unlock("
                f"hashtextextended({sql_literal(lock_name)}, 0)"
                ");\n\\quit\n"
            )
            process.stdin.flush()
            process.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def hold_advisory_lock(
    config: PsqlConfig,
    lock_name: str,
) -> Iterator[None]:
    """以獨立 psql session 在多個 loader 執行期間持有 PostgreSQL advisory lock。"""
    if not lock_name or lock_name != lock_name.strip():
        raise ValueError("lock_name must be non-empty without surrounding whitespace")

    args = build_psql_args(config, tuples_only=True)
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=psql_environment(config),
            cwd=config.workdir,
        )
    except OSError as error:
        raise PostgreSQLExecutionError(str(error)) from error

    if process.stdin is None or process.stdout is None or process.stderr is None:
        _close_lock_session(process, lock_name)
        raise PostgreSQLExecutionError("failed to open advisory lock session")

    process.stdin.write(
        "SELECT CASE WHEN pg_try_advisory_lock("
        f"hashtextextended({sql_literal(lock_name)}, 0)"
        ") THEN 'acquired' ELSE 'busy' END;\n"
    )
    process.stdin.flush()
    state = process.stdout.readline().strip()

    if state != "acquired":
        detail = ""
        if process.poll() is not None:
            detail = process.stderr.read().strip()
        _close_lock_session(process, lock_name)
        if state == "busy":
            raise PostgreSQLExecutionError(
                f"PostgreSQL advisory lock is already held: {lock_name}"
            )
        raise PostgreSQLExecutionError(
            detail or f"unexpected advisory lock response: {state!r}"
        )

    try:
        yield
    finally:
        _close_lock_session(process, lock_name)
