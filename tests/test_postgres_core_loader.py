from __future__ import annotations

from pathlib import Path

import pytest

import src.core.postgres_loader as core_loader
from src.core.postgres_loader import (
    CORE_DDL_FILES,
    CORE_INITIALIZATION_FILES,
    CoreConfig,
    CoreSyncError,
    CoreSyncResult,
    apply_core_ddl,
    initialize_core,
    sync_core_batch,
)


def _create_sql_files(directory: Path, names: tuple[str, ...]) -> None:
    for name in names:
        (directory / name).touch()


def _result(load_batch_id: str = "pgload_test") -> CoreSyncResult:
    return CoreSyncResult(
        load_batch_id=load_batch_id,
        status="success",
        source_rows=3,
        rows_inserted=1,
        rows_updated=1,
        rows_unchanged=1,
        rows_applied=2,
        attempt_count=1,
        validation_summary={"status": "success"},
        started_at="2026-09-04 10:00:00+08",
        completed_at="2026-09-04 10:00:01+08",
    )


def test_apply_core_ddl_uses_explicit_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_sql_files(tmp_path, CORE_DDL_FILES)
    applied: list[str] = []

    def fake_run(
        config: CoreConfig,
        **kwargs: object,
    ) -> str:
        del config
        sql_file = kwargs["sql_file"]
        assert isinstance(sql_file, Path)
        applied.append(sql_file.name)
        return ""

    monkeypatch.setattr(core_loader, "_run_psql", fake_run)
    apply_core_ddl(CoreConfig(sql_dir=tmp_path))

    assert tuple(applied) == CORE_DDL_FILES
    assert "003_upsert_dim_location.sql" not in applied
    assert "009_sync_fact_transactions.sql" not in applied


def test_initialize_core_is_explicit_and_uses_full_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_sql_files(tmp_path, CORE_INITIALIZATION_FILES)
    applied: list[str] = []

    def fake_run(config: CoreConfig, **kwargs: object) -> str:
        del config
        sql_file = kwargs["sql_file"]
        assert isinstance(sql_file, Path)
        applied.append(sql_file.name)
        return ""

    monkeypatch.setattr(core_loader, "_run_psql", fake_run)
    result = initialize_core(CoreConfig(sql_dir=tmp_path))

    assert result == CORE_INITIALIZATION_FILES
    assert tuple(applied) == CORE_INITIALIZATION_FILES


def test_successful_batch_is_returned_without_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _result()
    monkeypatch.setattr(
        core_loader,
        "_read_batch_state",
        lambda config, load_batch_id: ("success", "success"),
    )
    monkeypatch.setattr(
        core_loader,
        "_read_sync_result",
        lambda config, load_batch_id: expected,
    )
    monkeypatch.setattr(
        core_loader,
        "_claim_sync_run",
        lambda config, load_batch_id: pytest.fail("must not claim success"),
    )

    result = sync_core_batch(
        CoreConfig(apply_ddl=False),
        expected.load_batch_id,
    )

    assert result is expected


def test_new_batch_is_claimed_and_executes_012(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sql = tmp_path / "012_upsert_core_batch.sql"
    batch_sql.touch()
    expected = _result()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        core_loader,
        "_read_batch_state",
        lambda config, load_batch_id: ("success", None),
    )
    monkeypatch.setattr(
        core_loader,
        "_claim_sync_run",
        lambda config, load_batch_id: True,
    )
    monkeypatch.setattr(
        core_loader,
        "_read_sync_result",
        lambda config, load_batch_id: expected,
    )

    def fake_run(config: CoreConfig, **kwargs: object) -> str:
        del config
        calls.append(kwargs)
        return ""

    monkeypatch.setattr(core_loader, "_run_psql", fake_run)
    result = sync_core_batch(
        CoreConfig(sql_dir=tmp_path, apply_ddl=False),
        expected.load_batch_id,
    )

    assert result is expected
    assert calls == [
        {
            "sql_file": batch_sql,
            "variables": {"load_batch_id": expected.load_batch_id},
            "tuples_only": True,
        }
    ]


def test_failed_012_records_failure_without_hiding_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "012_upsert_core_batch.sql").touch()
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(
        core_loader,
        "_read_batch_state",
        lambda config, load_batch_id: ("success", "failed"),
    )
    monkeypatch.setattr(
        core_loader,
        "_claim_sync_run",
        lambda config, load_batch_id: True,
    )
    monkeypatch.setattr(
        core_loader,
        "_run_psql",
        lambda config, **kwargs: (_ for _ in ()).throw(
            CoreSyncError("012 failed")
        ),
    )
    monkeypatch.setattr(
        core_loader,
        "_record_failed_sync",
        lambda config, load_batch_id, error: recorded.append(
            (load_batch_id, str(error))
        ),
    )

    with pytest.raises(CoreSyncError, match="012 failed"):
        sync_core_batch(
            CoreConfig(sql_dir=tmp_path, apply_ddl=False),
            "pgload_retry",
        )

    assert recorded == [("pgload_retry", "012 failed")]


@pytest.mark.parametrize("sync_status", ["running", "unexpected"])
def test_non_retryable_core_status_is_blocked(
    sync_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_loader,
        "_read_batch_state",
        lambda config, load_batch_id: ("success", sync_status),
    )

    with pytest.raises(CoreSyncError):
        sync_core_batch(
            CoreConfig(apply_ddl=False),
            "pgload_test",
        )


def test_unsuccessful_staging_batch_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_loader,
        "_read_batch_state",
        lambda config, load_batch_id: ("failed", None),
    )

    with pytest.raises(CoreSyncError, match="expected 'success'"):
        sync_core_batch(
            CoreConfig(apply_ddl=False),
            "pgload_test",
        )


def test_claim_sql_only_starts_new_or_failed_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(config: CoreConfig, **kwargs: object) -> str:
        del config
        sql = kwargs["sql"]
        assert isinstance(sql, str)
        captured.append(sql)
        return "pgload_test"

    monkeypatch.setattr(core_loader, "_run_psql", fake_run)

    assert core_loader._claim_sync_run(CoreConfig(), "pgload_test")
    assert "WHERE sync_run.status = 'failed'" in captured[0]
    assert "attempt_count = sync_run.attempt_count + 1" in captured[0]
    assert "AND status = 'success'" in captured[0]


def test_missing_staging_batch_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_loader, "_run_psql", lambda config, **kwargs: "")

    with pytest.raises(CoreSyncError, match="does not exist"):
        core_loader._read_batch_state(CoreConfig(), "pgload_missing")


def test_batch_without_core_run_uses_explicit_not_started_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_loader,
        "_run_psql",
        lambda config, **kwargs: "success\tnot_started",
    )

    assert core_loader._read_batch_state(
        CoreConfig(),
        "pgload_new",
    ) == ("success", None)


def test_success_result_is_parsed_from_persisted_sync_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\t".join(
        [
            "pgload_test",
            "success",
            "3",
            "1",
            "1",
            "1",
            "2",
            "4",
            '{"status": "success"}',
            "2026-09-04 10:00:00+08",
            "2026-09-04 10:00:01+08",
        ]
    )
    monkeypatch.setattr(
        core_loader,
        "_run_psql",
        lambda config, **kwargs: output,
    )

    result = core_loader._read_sync_result(CoreConfig(), "pgload_test")

    assert result.rows_applied == 2
    assert result.attempt_count == 4
    assert result.validation_summary == {"status": "success"}


def test_failure_record_only_replaces_running_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(config: CoreConfig, **kwargs: object) -> str:
        del config
        captured.append(str(kwargs["sql"]))
        return ""

    monkeypatch.setattr(core_loader, "_run_psql", fake_run)
    core_loader._record_failed_sync(
        CoreConfig(),
        "pgload_test",
        CoreSyncError("failure detail"),
    )

    assert "status = 'failed'" in captured[0]
    assert "WHERE sync_run.status = 'running'" in captured[0]
    assert "failure detail" in captured[0]


def test_load_batch_id_validation_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        sync_core_batch(
            CoreConfig(apply_ddl=False),
            " pgload_test ",
        )
