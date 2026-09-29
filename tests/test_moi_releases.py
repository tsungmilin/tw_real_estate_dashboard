from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.ingestion.moi_releases as releases
from src.ingestion.moi_releases import (
    MoiReleaseError,
    read_release,
    register_moi_release,
    update_release_cleaning,
    update_release_publication,
)


def _source(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _register(
    source: Path,
    release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(releases, "inspect_moi_zip", lambda path: object())
    return register_moi_release(
        source,
        official_release_date="2026-09-21",
        period_end=11509,
        release_root=release_root,
    )


def test_first_release_is_archived_and_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "lvr_landAcsv.zip", b"first")
    result = _register(source, tmp_path / "releases", monkeypatch)

    assert result.created is True
    assert result.release_id == "2026-09-21"
    assert result.revision == 1
    assert result.archive_path.read_bytes() == b"first"
    record = read_release(tmp_path / "releases", result.release_id)
    assert record["source_sha256"] == result.source_sha256
    assert record["cleaning"] == {"status": "pending"}


def test_same_date_and_checksum_is_a_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "lvr_landAcsv.zip", b"same")
    first = _register(source, tmp_path / "releases", monkeypatch)
    second = _register(source, tmp_path / "releases", monkeypatch)

    assert first.created is True
    assert second.created is False
    assert second.release_id == first.release_id
    manifest = json.loads(second.registry_path.read_text(encoding="utf-8"))
    assert len(manifest["releases"]) == 1


def test_changed_content_on_same_date_creates_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "releases"
    first_source = _source(tmp_path / "first.zip", b"first")
    second_source = _source(tmp_path / "second.zip", b"second")

    first = _register(first_source, release_root, monkeypatch)
    second = _register(second_source, release_root, monkeypatch)

    assert first.release_id == "2026-09-21"
    assert second.release_id == "2026-09-21-r2"
    assert second.revision == 2
    assert first.archive_path.read_bytes() == b"first"
    assert second.archive_path.read_bytes() == b"second"


def test_period_end_must_match_release_month(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(releases, "inspect_moi_zip", lambda path: object())
    source = _source(tmp_path / "source.zip", b"source")

    with pytest.raises(MoiReleaseError, match="expected 11509"):
        register_moi_release(
            source,
            official_release_date="2026-09-21",
            period_end=11508,
            release_root=tmp_path / "releases",
        )


def test_cleaning_status_can_be_updated_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "source.zip", b"source")
    result = _register(source, tmp_path / "releases", monkeypatch)

    update_release_cleaning(
        tmp_path / "releases",
        result.release_id,
        {"status": "success", "cleaning_run_id": "run-1"},
    )

    record = read_release(tmp_path / "releases", result.release_id)
    assert record["cleaning"] == {
        "status": "success",
        "cleaning_run_id": "run-1",
    }


def test_publication_requires_successful_cleaning_and_valid_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path / "source.zip", b"source")
    result = _register(source, tmp_path / "releases", monkeypatch)

    with pytest.raises(MoiReleaseError, match="cleaning has not succeeded"):
        update_release_publication(
            tmp_path / "releases",
            result.release_id,
            "test",
            {"status": "success"},
        )

    update_release_cleaning(
        tmp_path / "releases",
        result.release_id,
        {"status": "success", "cleaning_run_id": "run-1"},
    )
    update_release_publication(
        tmp_path / "releases",
        result.release_id,
        "test",
        {"status": "success", "load_batch_id": "pgload-1"},
    )
    record = read_release(tmp_path / "releases", result.release_id)
    assert record["publications"]["test"]["load_batch_id"] == "pgload-1"

    with pytest.raises(MoiReleaseError, match="test or production"):
        update_release_publication(
            tmp_path / "releases",
            result.release_id,
            "other",
            {"status": "success"},
        )
