from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import src.orchestration.pipeline as pipeline
from src.analytics.postgres_loader import AnalyticsRefreshResult
from src.core.postgres_loader import CoreSyncResult
from src.loading.postgres_loader import LoadResult
from src.orchestration.pipeline import (
    PipelineBlockedError,
    PipelineConfig,
    run_pipeline,
)


def _staging_result(batch_id: str = "pgload_test") -> LoadResult:
    return LoadResult(
        load_batch_id=batch_id,
        cleaning_run_id="cleaning-test",
        parquet_rows=3,
        rows_inserted=1,
        rows_updated=1,
        rows_unchanged=1,
        rows_deleted=0,
        target_rows=10,
        validation_summary={"status": "success"},
    )


def _core_result(batch_id: str = "pgload_test") -> CoreSyncResult:
    return CoreSyncResult(
        load_batch_id=batch_id,
        status="success",
        source_rows=2,
        rows_inserted=1,
        rows_updated=1,
        rows_unchanged=0,
        rows_applied=2,
        attempt_count=1,
        validation_summary={"status": "success"},
        started_at="2026-09-04 10:00:00+08",
        completed_at="2026-09-04 10:00:01+08",
    )


def _analytics_result(batch_id: str = "pgload_test") -> AnalyticsRefreshResult:
    return AnalyticsRefreshResult(
        analytics_run_id="00000000-0000-0000-0000-000000000001",
        refresh_mode="incremental",
        load_batch_id=batch_id,
        status="success",
        attempt_count=1,
        refresh_summary={"national": {"affected_base_month_count": 0}},
        validation_summary={"national": {"mismatch_count": 0}},
        started_at="2026-09-04 10:00:01+08",
        completed_at="2026-09-04 10:00:02+08",
    )


@contextmanager
def _fake_lock(events: list[str]) -> Iterator[None]:
    events.append("lock_acquired")
    try:
        yield
    finally:
        events.append("lock_released")


def test_pipeline_runs_staging_then_same_batch_in_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    staging_result = _staging_result()
    core_result = _core_result()
    analytics_result = _analytics_result()

    monkeypatch.setattr(
        pipeline,
        "hold_advisory_lock",
        lambda config, name: _fake_lock(events),
    )
    monkeypatch.setattr(
        pipeline,
        "apply_staging_ddl",
        lambda config: events.append("staging_ddl"),
    )
    monkeypatch.setattr(
        pipeline,
        "apply_core_ddl",
        lambda config: events.append("core_ddl"),
    )
    monkeypatch.setattr(
        pipeline,
        "apply_analytics_ddl",
        lambda config: events.append("analytics_ddl"),
    )
    monkeypatch.setattr(
        pipeline,
        "_assert_core_dimensions_ready",
        lambda config: events.append("dimensions_ready"),
    )
    monkeypatch.setattr(
        pipeline,
        "_recover_failed_core_batch",
        lambda config: (),
    )
    monkeypatch.setattr(
        pipeline,
        "_recover_failed_analytics_batch",
        lambda config: (),
    )
    monkeypatch.setattr(
        pipeline,
        "load_postgres_staging",
        lambda config: events.append("staging_load") or staging_result,
    )

    def fake_core_sync(config: object, batch_id: str) -> CoreSyncResult:
        assert batch_id == staging_result.load_batch_id
        events.append("core_sync")
        return core_result

    monkeypatch.setattr(pipeline, "sync_core_batch", fake_core_sync)

    def fake_analytics_refresh(
        config: object,
        batch_id: str,
        *,
        lock_held: bool,
    ) -> AnalyticsRefreshResult:
        assert batch_id == staging_result.load_batch_id
        assert lock_held is True
        events.append("analytics_refresh")
        return analytics_result

    monkeypatch.setattr(
        pipeline,
        "refresh_analytics_batch",
        fake_analytics_refresh,
    )
    result = run_pipeline(
        PipelineConfig(parquet_path=tmp_path / "clean.parquet")
    )

    assert result.staging is staging_result
    assert result.core is core_result
    assert result.analytics is analytics_result
    assert events == [
        "lock_acquired",
        "staging_ddl",
        "core_ddl",
        "analytics_ddl",
        "dimensions_ready",
        "staging_load",
        "core_sync",
        "analytics_refresh",
        "lock_released",
    ]


def test_staging_failure_never_calls_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "hold_advisory_lock",
        lambda config, name: _fake_lock(events),
    )
    monkeypatch.setattr(
        pipeline,
        "_assert_core_dimensions_ready",
        lambda config: None,
    )
    monkeypatch.setattr(
        pipeline,
        "_recover_failed_core_batch",
        lambda config: (),
    )
    monkeypatch.setattr(
        pipeline,
        "_recover_failed_analytics_batch",
        lambda config: (),
    )
    monkeypatch.setattr(
        pipeline,
        "load_postgres_staging",
        lambda config: (_ for _ in ()).throw(RuntimeError("staging failed")),
    )
    monkeypatch.setattr(
        pipeline,
        "sync_core_batch",
        lambda config, batch_id: pytest.fail("core must not run"),
    )

    with pytest.raises(RuntimeError, match="staging failed"):
        run_pipeline(PipelineConfig(apply_ddl=False))

    assert events == ["lock_acquired", "lock_released"]


def test_single_failed_analytics_batch_is_retried_before_new_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered = _analytics_result("pgload_failed")
    monkeypatch.setattr(
        pipeline,
        "_unfinished_analytics_runs",
        lambda config: [("incremental", "pgload_failed", "failed")],
    )
    monkeypatch.setattr(
        pipeline,
        "refresh_analytics_batch",
        lambda config, batch_id, *, lock_held: recovered,
    )

    assert pipeline._recover_failed_analytics_batch(PipelineConfig()) == (
        recovered,
    )


@pytest.mark.parametrize(
    "unfinished",
    [
        [("incremental", "pgload_running", "running")],
        [("full", "", "failed")],
        [
            ("incremental", "pgload_1", "failed"),
            ("incremental", "pgload_2", "failed"),
        ],
    ],
)
def test_unsafe_analytics_state_blocks_new_staging(
    unfinished: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_unfinished_analytics_runs",
        lambda config: unfinished,
    )

    with pytest.raises(PipelineBlockedError):
        pipeline._recover_failed_analytics_batch(PipelineConfig())


def test_single_failed_core_batch_is_retried_before_new_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered = _core_result("pgload_failed")
    monkeypatch.setattr(
        pipeline,
        "_unfinished_core_runs",
        lambda config: [("pgload_failed", "failed")],
    )
    monkeypatch.setattr(
        pipeline,
        "sync_core_batch",
        lambda config, batch_id: recovered,
    )

    assert pipeline._recover_failed_core_batch(PipelineConfig()) == (recovered,)


@pytest.mark.parametrize(
    "unfinished",
    [
        [("pgload_running", "running")],
        [("pgload_1", "failed"), ("pgload_2", "failed")],
    ],
)
def test_ambiguous_unfinished_core_state_blocks_new_staging(
    unfinished: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "_unfinished_core_runs",
        lambda config: unfinished,
    )

    with pytest.raises(PipelineBlockedError):
        pipeline._recover_failed_core_batch(PipelineConfig())


def test_incomplete_dimensions_block_before_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "run_psql",
        lambda config, **kwargs: "367\t14",
    )

    with pytest.raises(PipelineBlockedError, match="not initialized"):
        pipeline._assert_core_dimensions_ready(PipelineConfig())
