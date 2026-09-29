"""驗證並封存 MOI 發布批次，原子更新可追溯的本機發布紀錄。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .moi_transactions import inspect_moi_zip


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_FILENAME = "release_manifest.json"


class MoiReleaseError(ValueError):
    """發布批次中繼資料、來源內容或登錄狀態不符合契約。"""


@dataclass(frozen=True)
class MoiReleaseRegistration:
    """一次發布批次登錄結果；`created=False` 表示同內容已存在。"""

    release_id: str
    official_release_date: str
    revision: int
    period_end: int
    source_sha256: str
    archive_path: Path
    registry_path: Path
    created: bool


def file_sha256(path: Path) -> str:
    """分段計算來源檔 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_release_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise MoiReleaseError(
            "official release date must use valid YYYY-MM-DD format"
        ) from error
    if parsed.isoformat() != value:
        raise MoiReleaseError(
            "official release date must use valid YYYY-MM-DD format"
        )
    return parsed


def _validate_period_end(value: int, release_date: date) -> None:
    year, month = divmod(value, 100)
    if year < 1 or not 1 <= month <= 12:
        raise MoiReleaseError("period_end must be a valid ROC YYYYMM value")
    expected = (release_date.year - 1911) * 100 + release_date.month
    if value != expected:
        raise MoiReleaseError(
            "period_end must match the official release month: "
            f"expected {expected}, found {value}"
        )


def _empty_registry() -> dict[str, Any]:
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "releases": []}


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_registry()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MoiReleaseError(f"cannot read release registry: {path}") from error
    if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise MoiReleaseError(
            f"unsupported release registry schema: {payload.get('schema_version')!r}"
        )
    if not isinstance(payload.get("releases"), list):
        raise MoiReleaseError("release registry must contain a releases list")
    release_ids = [item.get("release_id") for item in payload["releases"]]
    if any(not isinstance(value, str) for value in release_ids):
        raise MoiReleaseError("release registry contains an invalid release_id")
    if len(release_ids) != len(set(release_ids)):
        raise MoiReleaseError("release registry contains duplicate release_id values")
    return payload


def _write_registry(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_immutable(source: Path, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if file_sha256(destination) == expected_sha256:
            return
        raise MoiReleaseError(
            f"archive destination already contains different content: {destination}"
        )
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        if file_sha256(temporary) != expected_sha256:
            raise MoiReleaseError("archived source checksum changed while copying")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _registration(
    record: dict[str, Any],
    release_root: Path,
    registry_path: Path,
    *,
    created: bool,
) -> MoiReleaseRegistration:
    return MoiReleaseRegistration(
        release_id=record["release_id"],
        official_release_date=record["official_release_date"],
        revision=int(record["revision"]),
        period_end=int(record["period_end"]),
        source_sha256=record["source_sha256"],
        archive_path=release_root / record["archive_path"],
        registry_path=registry_path,
        created=created,
    )


def register_moi_release(
    source_path: Path,
    *,
    official_release_date: str,
    period_end: int,
    release_root: Path,
    source_url: str | None = None,
) -> MoiReleaseRegistration:
    """驗證並不可變封存一個 MOI 發布批次，再寫入發布紀錄。"""

    source_path = source_path.resolve()
    release_root = release_root.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"MOI source not found: {source_path}")
    release_date = _validate_release_date(official_release_date)
    _validate_period_end(period_end, release_date)
    inspect_moi_zip(source_path)
    source_sha256 = file_sha256(source_path)

    registry_path = release_root / REGISTRY_FILENAME
    registry = _load_registry(registry_path)
    same_date = [
        item
        for item in registry["releases"]
        if item.get("official_release_date") == official_release_date
    ]
    for item in same_date:
        if item.get("source_sha256") != source_sha256:
            continue
        if int(item.get("period_end", -1)) != period_end:
            raise MoiReleaseError(
                "same release date and checksum already use a different period_end"
            )
        archived = release_root / item["archive_path"]
        if not archived.is_file() or file_sha256(archived) != source_sha256:
            raise MoiReleaseError(
                f"registered release archive is missing or changed: {archived}"
            )
        return _registration(
            item,
            release_root,
            registry_path,
            created=False,
        )

    revision = max((int(item["revision"]) for item in same_date), default=0) + 1
    release_id = (
        official_release_date
        if revision == 1
        else f"{official_release_date}-r{revision}"
    )
    archive_name = source_path.name
    relative_archive = Path(release_id) / archive_name
    archive_path = release_root / relative_archive
    _copy_immutable(source_path, archive_path, source_sha256)

    record: dict[str, Any] = {
        "release_id": release_id,
        "official_release_date": official_release_date,
        "revision": revision,
        "period_end": period_end,
        "source_sha256": source_sha256,
        "source_filename": source_path.name,
        "source_url": source_url,
        "archive_path": relative_archive.as_posix(),
        "archived_at_utc": _utc_now(),
        "cleaning": {"status": "pending"},
        "publications": {},
    }
    registry["releases"].append(record)
    registry["releases"].sort(
        key=lambda item: (
            item["official_release_date"],
            int(item["revision"]),
        )
    )
    _write_registry(registry_path, registry)
    return _registration(record, release_root, registry_path, created=True)


def read_release(release_root: Path, release_id: str) -> dict[str, Any]:
    """讀取單一發布批次；回傳獨立物件，避免呼叫端改動原內容。"""

    registry = _load_registry(release_root.resolve() / REGISTRY_FILENAME)
    for item in registry["releases"]:
        if item.get("release_id") == release_id:
            return json.loads(json.dumps(item))
    raise MoiReleaseError(f"release_id is not registered: {release_id}")


def update_release_cleaning(
    release_root: Path,
    release_id: str,
    details: dict[str, Any],
) -> None:
    """原子更新發布批次的清理狀態與輸出追蹤資料。"""

    registry_path = release_root.resolve() / REGISTRY_FILENAME
    registry = _load_registry(registry_path)
    for item in registry["releases"]:
        if item.get("release_id") == release_id:
            item["cleaning"] = details
            _write_registry(registry_path, registry)
            return
    raise MoiReleaseError(f"release_id is not registered: {release_id}")


def update_release_publication(
    release_root: Path,
    release_id: str,
    target: str,
    details: dict[str, Any],
) -> None:
    """原子更新測試或正式資料庫的 SQL 發布狀態。"""

    if target not in {"test", "production"}:
        raise MoiReleaseError("publication target must be test or production")
    registry_path = release_root.resolve() / REGISTRY_FILENAME
    registry = _load_registry(registry_path)
    for item in registry["releases"]:
        if item.get("release_id") != release_id:
            continue
        if item.get("cleaning", {}).get("status") != "success":
            raise MoiReleaseError(
                f"release cleaning has not succeeded: {release_id}"
            )
        publications = item.setdefault("publications", {})
        publications[target] = details
        _write_registry(registry_path, registry)
        return
    raise MoiReleaseError(f"release_id is not registered: {release_id}")
