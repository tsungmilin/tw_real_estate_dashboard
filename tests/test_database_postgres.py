from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.database.postgres as postgres
from src.database.postgres import (
    PostgreSQLExecutionError,
    PsqlConfig,
    build_psql_args,
    hold_advisory_lock,
    run_psql,
    sql_literal,
)


def test_sql_literal_escapes_single_quotes() -> None:
    assert sql_literal("batch'one") == "'batch''one'"


def test_build_psql_args_is_noninteractive_and_supports_variables() -> None:
    args = build_psql_args(
        PsqlConfig(database="dashboard_test", psql_path="missing-test-psql"),
        variables={"load_batch_id": "pgload_123"},
        tuples_only=True,
    )

    assert args[0] == "missing-test-psql"
    assert "-X" in args
    assert "--no-password" in args
    assert "--set=ON_ERROR_STOP=1" in args
    assert "--set=load_batch_id=pgload_123" in args
    assert "--quiet" in args
    assert "\t" in args


def test_build_psql_args_rejects_invalid_variable_names() -> None:
    with pytest.raises(ValueError, match="variable name"):
        build_psql_args(
            PsqlConfig(),
            variables={"bad-name": "value"},
        )


def test_run_psql_uses_workdir_and_connection_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(postgres.subprocess, "run", fake_run)
    output = run_psql(
        PsqlConfig(
            database="dashboard_test",
            psql_path="missing-test-psql",
            workdir=tmp_path,
            connect_timeout_seconds=7,
        ),
        sql="SELECT 1;",
    )

    assert output == "ok"
    assert captured["cwd"] == tmp_path
    assert captured["env"]["PGCONNECT_TIMEOUT"] == "7"
    assert captured["args"][-2:] == ["--command", "SELECT 1;"]


def test_run_psql_raises_shared_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        postgres.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="database unavailable",
        ),
    )

    with pytest.raises(PostgreSQLExecutionError, match="database unavailable"):
        run_psql(PsqlConfig(psql_path="missing-test-psql"), sql="SELECT 1;")


def test_advisory_lock_rejects_blank_name_before_starting_psql() -> None:
    with pytest.raises(ValueError, match="lock_name"):
        with hold_advisory_lock(PsqlConfig(), " "):
            pass
