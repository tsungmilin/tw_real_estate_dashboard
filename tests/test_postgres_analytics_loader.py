from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest

import src.analytics.postgres_loader as analytics_loader
from src.analytics.postgres_loader import (
    ANALYTICS_DDL_FILES,
    FULL_REFRESH_FILES,
    INCREMENTAL_REFRESH_FILES,
    VALIDATION_FILES,
    AnalyticsConfig,
    AnalyticsRefreshError,
    AnalyticsRefreshResult,
    apply_analytics_ddl,
    refresh_analytics_batch,
)


def _result(load_batch_id: str = "pgload_test") -> AnalyticsRefreshResult:
    return AnalyticsRefreshResult(
        analytics_run_id="00000000-0000-0000-0000-000000000001",
        refresh_mode="incremental",
        load_batch_id=load_batch_id,
        status="success",
        attempt_count=1,
        refresh_summary={"national": {"affected_base_month_count": 0}},
        validation_summary={"national": {"mismatch_count": 0}},
        started_at="2026-09-05 10:00:00+08",
        completed_at="2026-09-05 10:00:01+08",
    )


def _touch_sql(directory: Path, names: tuple[str, ...]) -> None:
    for name in names:
        (directory / name).touch()


def test_apply_analytics_ddl_uses_explicit_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _touch_sql(tmp_path, ANALYTICS_DDL_FILES)
    applied: list[str] = []

    def fake_run(config: AnalyticsConfig, **kwargs: object) -> str:
        sql_file = kwargs["sql_file"]
        assert isinstance(sql_file, Path)
        applied.append(sql_file.name)
        return ""

    monkeypatch.setattr(analytics_loader, "_run_psql", fake_run)
    apply_analytics_ddl(AnalyticsConfig(sql_dir=tmp_path))

    assert tuple(applied) == ANALYTICS_DDL_FILES
    assert "002_refresh_national_monthly_kpi_full.sql" not in applied
    assert "003_refresh_national_monthly_kpi_incremental.sql" not in applied


def test_successful_incremental_batch_is_returned_without_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _result()
    monkeypatch.setattr(
        analytics_loader,
        "_run_with_lock",
        lambda config, *, lock_held: nullcontext(),
    )
    monkeypatch.setattr(
        analytics_loader,
        "_read_incremental_state",
        lambda config, batch_id: ("success", "success"),
    )
    monkeypatch.setattr(
        analytics_loader,
        "_read_result",
        lambda config, **kwargs: expected,
    )
    monkeypatch.setattr(
        analytics_loader,
        "_claim_incremental_run",
        lambda config, batch_id: pytest.fail("must not reclaim success"),
    )

    result = refresh_analytics_batch(
        AnalyticsConfig(apply_ddl=False),
        expected.load_batch_id or "",
    )
    assert result is expected


def test_execute_incremental_refreshes_all_grains_then_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = tuple(
        filename
        for _, filename in (*INCREMENTAL_REFRESH_FILES, *VALIDATION_FILES)
    )
    _touch_sql(tmp_path, names)
    calls: list[str] = []
    expected = _result()

    refresh_outputs = {
        "003_refresh_national_monthly_kpi_incremental.sql": (
            "pgload_test\t10\t0.75\t2024-07-01\t2024-07-01\t2024-07-01"
            "\t0\t0\t\t"
        ),
        "007_refresh_city_monthly_kpi_incremental.sql": (
            "pgload_test\t2024-07-01\t2024-07-01\t22\t0\t0\t\t"
        ),
        "011_refresh_district_rolling_3m_incremental.sql": (
            "pgload_test\t2024-07-01\t2024-07-01\t368\t0\t0\t\t"
        ),
        "004_validate_national_monthly_kpi.sql": (
            "2012-08-01\t10\t0.75\t7.5\t2024-07-01\t2024-07-01"
            "\t2024-07-01\tPASS_ALIGNED\n144\t144\t0\t0\n"
            "0\t0\t0\t0\t0\t0\n0\t0\t0\t0\t0"
        ),
        "008_validate_city_monthly_kpi.sql": (
            "2012-08-01\t2012-08-01\t2024-07-01\t2012-08-01"
            "\t2024-07-01\t144\t144\t0\tPASS_ALIGNED\n"
            "22\tPASS_MAPPING\n3168\t3168\t144\t144\t22\t22\t0\t0\t0\n"
            "0\t0\t0\t0\t0\t0\t0\n0\t0\t0\t0\t0\n0"
        ),
        "012_validate_district_rolling_3m.sql": (
            "2012-08-01\t2012-10-01\t2012-08-01\t2024-07-01"
            "\t2012-10-01\t2024-07-01\t368\t142\t52256\t52256\t0\t0\t0\n"
            "0\t0\t0\t0\t0\t0\t0\t0\t0\t0\t0\n0"
        ),
    }

    def fake_run(config: AnalyticsConfig, **kwargs: object) -> str:
        sql_file = kwargs["sql_file"]
        assert isinstance(sql_file, Path)
        calls.append(sql_file.name)
        return refresh_outputs[sql_file.name]

    monkeypatch.setattr(analytics_loader, "_run_psql", fake_run)
    monkeypatch.setattr(
        analytics_loader,
        "_record_successful_run",
        lambda config, run_id, refresh, validation: calls.append("success"),
    )
    monkeypatch.setattr(
        analytics_loader,
        "_read_result",
        lambda config, **kwargs: expected,
    )

    result = analytics_loader._execute_refresh(
        AnalyticsConfig(sql_dir=tmp_path, apply_ddl=False),
        analytics_run_id=expected.analytics_run_id,
        mode="incremental",
        load_batch_id="pgload_test",
    )

    assert result is expected
    assert calls == [
        *(filename for _, filename in INCREMENTAL_REFRESH_FILES),
        *(filename for _, filename in VALIDATION_FILES),
        "success",
    ]


def test_validation_failure_is_recorded_and_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = tuple(
        filename for _, filename in (*FULL_REFRESH_FILES, *VALIDATION_FILES)
    )
    _touch_sql(tmp_path, names)
    recorded: list[str] = []

    full_outputs = {
        "002_refresh_national_monthly_kpi_full.sql": (
            "2012-08-01\t10\t0.75\t2024-07-01\t144"
        ),
        "006_refresh_city_monthly_kpi_full.sql": (
            "2012-08-01\t2024-07-01\t144\t22\t3168"
        ),
        "010_refresh_district_rolling_3m_full.sql": (
            "2012-10-01\t2024-07-01\t368\t142\t52256"
        ),
        "004_validate_national_monthly_kpi.sql": (
            "2012-08-01\t10\t0.75\t7.5\t2024-07-01\t2024-07-01"
            "\t2024-07-01\tPASS_ALIGNED\n144\t144\t0\t0\n"
            "0\t1\t0\t0\t0\t0\n0\t0\t0\t0\t0"
        ),
    }

    def fake_run(config: AnalyticsConfig, **kwargs: object) -> str:
        sql_file = kwargs["sql_file"]
        assert isinstance(sql_file, Path)
        return full_outputs[sql_file.name]

    monkeypatch.setattr(analytics_loader, "_run_psql", fake_run)
    monkeypatch.setattr(
        analytics_loader,
        "_record_failed_run",
        lambda config, run_id, error: recorded.append(str(error)),
    )

    with pytest.raises(AnalyticsRefreshError, match="national base KPI"):
        analytics_loader._execute_refresh(
            AnalyticsConfig(sql_dir=tmp_path, apply_ddl=False),
            analytics_run_id="00000000-0000-0000-0000-000000000001",
            mode="full",
            load_batch_id=None,
        )

    assert len(recorded) == 1


def test_incremental_requires_successful_core_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analytics_loader,
        "_run_with_lock",
        lambda config, *, lock_held: nullcontext(),
    )
    monkeypatch.setattr(
        analytics_loader,
        "_read_incremental_state",
        lambda config, batch_id: ("failed", None),
    )

    with pytest.raises(AnalyticsRefreshError, match="expected 'success'"):
        refresh_analytics_batch(
            AnalyticsConfig(apply_ddl=False),
            "pgload_test",
        )
