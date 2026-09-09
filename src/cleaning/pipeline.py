"""協調完整清理流程，所有發布檢核通過後才更新正式輸出。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .audit import AuditAccumulator
from .contract import (
    CLEAN_SCHEMA,
    CURRENT_SOURCE_BASELINE,
    CURRENT_SOURCE_SHA256,
    DEFAULT_CHUNK_SIZE,
    EXCLUDED_SCHEMA,
    EXPECTED_ALIAS_ROWS,
    EXPECTED_LOCATION_ROWS,
    LOCATION_ALIAS_COLUMNS,
    LOCATION_LOOKUP_COLUMNS,
    PRIMARY_EXCLUSION_REASONS,
    REQUIRED_RAW_COLUMNS,
    SPEC_VERSION,
)
from .transforms import normalize_text, process_chunk


@dataclass(frozen=True)
class CleaningConfig:
    """一次清理執行所需的輸入、輸出與記憶體邊界。"""

    raw_path: Path
    lookup_path: Path
    aliases_path: Path
    output_dir: Path
    audit_dir: Path
    chunk_size: int = DEFAULT_CHUNK_SIZE


@dataclass(frozen=True)
class CleaningResult:
    """成功發布後回傳的檔案位置與核心筆數。"""

    run_id: str
    clean_path: Path
    excluded_path: Path
    audit_path: Path
    input_rows: int
    clean_rows: int
    excluded_rows: int


def file_sha256(path: Path) -> str:
    # 分段計算雜湊，避免處理 14 GB 原始檔校驗碼時占用大量記憶體。
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _raw_columns(path: Path) -> list[str]:
    # 只讀 Stata metadata，正式 rows 由後面的 chunk reader 處理。
    reader = pd.io.stata.StataReader(
        path,
        convert_categoricals=False,
        convert_dates=False,
        convert_missing=False,
        preserve_dtypes=True,
    )
    return list(reader.variable_labels())


def _validate_raw_schema(path: Path) -> list[str]:
    # 缺少必要欄位屬於結構問題，必須在寫出任何資料前 hard fail。
    if not path.is_file():
        raise FileNotFoundError(f"raw source not found: {path}")
    columns = _raw_columns(path)
    missing = sorted(set(REQUIRED_RAW_COLUMNS) - set(columns))
    if missing:
        raise ValueError(f"raw source missing required columns: {missing}")
    return columns


def _require_unique(frame: pd.DataFrame, columns: list[str], label: str) -> None:
    duplicates = frame.duplicated(columns, keep=False)
    if bool(duplicates.any()):
        raise ValueError(
            f"{label} is not unique on {columns}; rows={int(duplicates.sum())}"
        )


def _load_location_references(
    lookup_path: Path,
    aliases_path: Path,
) -> tuple[pd.DataFrame, dict[tuple[str, str], str]]:
    # 參照資料預檢同時驗證欄位、368 筆、ID 格式、唯一性與別名目標。
    if not lookup_path.is_file():
        raise FileNotFoundError(f"location lookup not found: {lookup_path}")
    if not aliases_path.is_file():
        raise FileNotFoundError(f"location aliases not found: {aliases_path}")

    lookup = pd.read_csv(lookup_path, dtype="string")
    aliases = pd.read_csv(aliases_path, dtype="string")
    missing_lookup = sorted(set(LOCATION_LOOKUP_COLUMNS) - set(lookup.columns))
    missing_aliases = sorted(set(LOCATION_ALIAS_COLUMNS) - set(aliases.columns))
    if missing_lookup:
        raise ValueError(f"location lookup missing columns: {missing_lookup}")
    if missing_aliases:
        raise ValueError(f"location aliases missing columns: {missing_aliases}")

    lookup = lookup[list(LOCATION_LOOKUP_COLUMNS)].copy()
    aliases = aliases[list(LOCATION_ALIAS_COLUMNS)].copy()
    for column in lookup.columns:
        lookup[column] = normalize_text(lookup[column])
    for column in aliases.columns:
        aliases[column] = normalize_text(aliases[column])

    if len(lookup) != EXPECTED_LOCATION_ROWS:
        raise ValueError(
            f"location lookup rows={len(lookup)}; expected {EXPECTED_LOCATION_ROWS}"
        )
    if len(aliases) != EXPECTED_ALIAS_ROWS:
        raise ValueError(
            f"location aliases rows={len(aliases)}; expected {EXPECTED_ALIAS_ROWS}"
        )
    if bool(lookup.isna().any().any()):
        raise ValueError("location lookup contains null values")
    if not lookup["county_id"].str.fullmatch(r"\d{5}", na=False).all():
        raise ValueError("location county_id must be a five-character numeric string")
    if not lookup["town_id"].str.fullmatch(r"\d{8}", na=False).all():
        raise ValueError("location town_id must be an eight-character numeric string")
    if not lookup["legacy_town_code"].str.fullmatch(r"\d{2}", na=False).all():
        raise ValueError("legacy_town_code must be a two-character numeric string")

    _require_unique(
        lookup,
        ["legacy_county_code", "district"],
        "location lookup join key",
    )
    _require_unique(lookup, ["county_id", "town_id"], "official location IDs")
    _require_unique(
        aliases,
        ["legacy_county_code", "raw_town"],
        "location aliases",
    )

    alias_targets = aliases.merge(
        lookup,
        left_on=["legacy_county_code", "normalized_town"],
        right_on=["legacy_county_code", "district"],
        how="left",
        validate="many_to_one",
    )
    if bool(alias_targets["town_id"].isna().any()):
        raise ValueError("one or more aliases do not resolve to the location lookup")

    # 主迴圈只保留連接與標準輸出需要的參照欄位。
    join_lookup = lookup[
        [
            "legacy_county_code",
            "district",
            "county_id",
            "town_id",
            "city",
        ]
    ].copy()
    alias_map = {
        (row.legacy_county_code, row.raw_town): row.normalized_town
        for row in aliases.itertuples(index=False)
    }
    return join_lookup, alias_map


def _open_duplicate_database(path: Path) -> sqlite3.Connection:
    # 438 萬個 ID 使用 disk-backed SQLite，避免全部塞進 Python set。
    # 資料庫位於本次執行的臨時目錄，完成或失敗後都會刪除。
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-262144")
    connection.execute(
        """
        CREATE TABLE id_counts (
            source_id TEXT PRIMARY KEY,
            occurrences INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    return connection


def _scan_source_ids(
    raw_path: Path,
    chunk_size: int,
    connection: sqlite3.Connection,
) -> tuple[int, int, dict[str, int]]:
    # 第一個 pass 只讀 no，精確找出跨 chunk duplicates 與 missing IDs。
    total_rows = 0
    missing_rows = 0
    with pd.read_stata(
        raw_path,
        columns=["no"],
        chunksize=chunk_size,
        convert_categoricals=False,
        convert_dates=False,
        convert_missing=False,
        preserve_dtypes=True,
    ) as reader:
        for chunk_number, chunk in enumerate(reader, start=1):
            total_rows += len(chunk)
            source_ids = normalize_text(chunk["no"])
            missing_rows += int(source_ids.isna().sum())
            valid_ids = source_ids.dropna().astype(str)
            connection.executemany(
                """
                INSERT INTO id_counts (source_id, occurrences)
                VALUES (?, 1)
                ON CONFLICT(source_id)
                DO UPDATE SET occurrences = occurrences + 1
                """,
                ((source_id,) for source_id in valid_ids),
            )
            connection.commit()
            print(
                f"id_scan chunk={chunk_number} rows={total_rows} "
                f"missing_ids={missing_rows}",
                flush=True,
            )

    duplicate_counts = {
        str(source_id): int(occurrences)
        for source_id, occurrences in connection.execute(
            "SELECT source_id, occurrences FROM id_counts WHERE occurrences > 1"
        )
    }
    return total_rows, missing_rows, duplicate_counts


def _canonical_cell(value: Any) -> tuple[str, Any]:
    # 將 NA 與浮點值轉成可穩定比較的形式，用來判斷 duplicate 是否衝突。
    if value is None or pd.isna(value):
        return ("null", None)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return ("float", value.hex())
    return (type(value).__name__, value)


def _classify_duplicate_ids(
    raw_path: Path,
    raw_columns: list[str],
    chunk_size: int,
    duplicate_counts: dict[str, int],
) -> dict[str, str]:
    # 常見路徑沒有 duplicates，因此不需要再掃 63 欄。
    # 只有真的重複時才比較同 ID 的其餘 raw 欄位，分成 exact 或 conflict。
    if not duplicate_counts:
        return {}

    duplicate_ids = set(duplicate_counts)
    comparison_columns = [column for column in raw_columns if column != "no"]
    first_rows: dict[str, tuple[tuple[str, Any], ...]] = {}
    observed = {source_id: 0 for source_id in duplicate_ids}
    conflicts: set[str] = set()

    with pd.read_stata(
        raw_path,
        columns=["no", *comparison_columns],
        chunksize=chunk_size,
        convert_categoricals=False,
        convert_dates=False,
        convert_missing=False,
        preserve_dtypes=True,
    ) as reader:
        for chunk_number, chunk in enumerate(reader, start=1):
            source_ids = normalize_text(chunk["no"])
            duplicate_mask = source_ids.isin(duplicate_ids)
            if not bool(duplicate_mask.any()):
                continue
            selected = chunk.loc[duplicate_mask, comparison_columns]
            selected_ids = source_ids.loc[duplicate_mask]
            for source_id, values in zip(
                selected_ids.astype(str),
                selected.itertuples(index=False, name=None),
                strict=True,
            ):
                observed[source_id] += 1
                canonical = tuple(_canonical_cell(value) for value in values)
                first = first_rows.setdefault(source_id, canonical)
                if canonical != first:
                    conflicts.add(source_id)
            print(
                f"duplicate_classification chunk={chunk_number} "
                f"observed_rows={sum(observed.values())}",
                flush=True,
            )

    if observed != duplicate_counts:
        raise ValueError(
            "duplicate classification did not reproduce the ID occurrence counts"
        )
    return {
        source_id: (
            "duplicate_conflict" if source_id in conflicts else "duplicate_exact"
        )
        for source_id in duplicate_ids
    }


class _ParquetWriters:
    """將每批資料寫成同一份 Parquet 的資料列群組，不產生多個檔案。"""

    def __init__(self, clean_path: Path, excluded_path: Path) -> None:
        self.clean_rows = 0
        self.excluded_rows = 0
        self.clean_writer = pq.ParquetWriter(
            clean_path,
            CLEAN_SCHEMA,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
        )
        self.excluded_writer = pq.ParquetWriter(
            excluded_path,
            EXCLUDED_SCHEMA,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
        )

    @staticmethod
    def _table(frame: pd.DataFrame, schema: pa.Schema) -> pa.Table:
        # 由 Arrow schema 統一型別，並再次阻止 required output 欄位出現 NULL。
        table = pa.Table.from_pandas(
            frame,
            schema=schema,
            preserve_index=False,
            safe=True,
        )
        for field, column in zip(schema, table.columns, strict=True):
            if not field.nullable and column.null_count:
                raise ValueError(
                    f"non-null output field {field.name} contains "
                    f"{column.null_count} nulls"
                )
        return table

    def write_clean(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        table = self._table(frame, CLEAN_SCHEMA)
        self.clean_writer.write_table(table, row_group_size=len(table))
        self.clean_rows += len(table)

    def write_excluded(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        table = self._table(frame, EXCLUDED_SCHEMA)
        self.excluded_writer.write_table(table, row_group_size=len(table))
        self.excluded_rows += len(table)

    def close(self) -> None:
        self.clean_writer.close()
        self.excluded_writer.close()


def _validate_parquet(
    path: Path,
    expected_schema: pa.Schema,
    expected_rows: int,
) -> dict[str, Any]:
    # 以 batches 完整讀回，驗證 schema、row count 與 non-null contract。
    parquet = pq.ParquetFile(path)
    actual_schema = parquet.schema_arrow
    if not actual_schema.equals(expected_schema, check_metadata=False):
        raise ValueError(
            f"output schema differs for {path.name}: actual={actual_schema}"
        )
    metadata_rows = int(parquet.metadata.num_rows)
    if metadata_rows != expected_rows:
        raise ValueError(
            f"output rows differ for {path.name}: "
            f"metadata={metadata_rows}, expected={expected_rows}"
        )

    readback_rows = 0
    for batch in parquet.iter_batches(batch_size=100_000):
        readback_rows += len(batch)
        for field, column in zip(expected_schema, batch.columns, strict=True):
            if not field.nullable and column.null_count:
                raise ValueError(
                    f"readback field {field.name} contains nulls in {path.name}"
                )
    if readback_rows != expected_rows:
        raise ValueError(
            f"readback rows differ for {path.name}: "
            f"read={readback_rows}, expected={expected_rows}"
        )
    return {
        "filename": path.name,
        "row_count": expected_rows,
        "schema": str(expected_schema),
        "size_bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "readback_validated": True,
    }


def _validate_current_baseline(
    source_sha256: str,
    audit: AuditAccumulator,
) -> dict[str, Any]:
    # 新來源仍需通過通用 gates；只有已知 checksum 才要求精確重現歷史 counts。
    actual = {
        "input_rows": audit.input_rows,
        **{
            reason: int(audit.primary_reasons.get(reason, 0))
            for reason in PRIMARY_EXCLUSION_REASONS
        },
        "excluded_rows": audit.excluded_rows,
        "clean_rows": audit.clean_rows,
    }
    applies = source_sha256 == CURRENT_SOURCE_SHA256
    differences = {
        key: {"expected": expected, "actual": actual.get(key)}
        for key, expected in CURRENT_SOURCE_BASELINE.items()
        if actual.get(key) != expected
    }
    if applies and differences:
        raise ValueError(f"current-source baseline differs: {differences}")
    return {
        "applies": applies,
        "expected": CURRENT_SOURCE_BASELINE,
        "actual": actual,
        "differences": differences,
        "status": "PASS" if not applies or not differences else "FAIL",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    # 先寫同目錄 temporary file，再 replace，避免留下半份 JSON。
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _publish_files(pairs: list[tuple[Path, Path]], backup_dir: Path) -> None:
    # 發布前暫時保留上一版；若任一 replace 失敗就完整 rollback。
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for _, destination in pairs:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                backup = backup_dir / f"previous-{destination.name}"
                os.replace(destination, backup)
                backups.append((backup, destination))
        for source, destination in pairs:
            os.replace(source, destination)
            published.append(destination)
    except Exception:
        for destination in published:
            destination.unlink(missing_ok=True)
        for backup, destination in backups:
            os.replace(backup, destination)
        raise
    for backup, _ in backups:
        backup.unlink(missing_ok=True)


def run_cleaning(config: CleaningConfig) -> CleaningResult:
    """執行完整清理，全部發布檢核通過後才更新正式輸出。"""

    if config.chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    # 從一開始就建立 run ID，成功與失敗 audit 都能追溯同一次執行。
    run_id = _new_run_id()
    started_at = _utc_now()
    audit = AuditAccumulator()
    failed_audit_path = config.audit_dir / f"cleaning_run_{run_id}.json"
    source_sha256: str | None = None
    lookup_sha256: str | None = None
    aliases_sha256: str | None = None
    raw_columns: list[str] = []

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.audit_dir.mkdir(parents=True, exist_ok=True)
    # 所有 Parquet 先寫在 output filesystem 的 temporary directory，便於安全 rename。
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".cleaning-{run_id}-",
            dir=config.output_dir,
        )
    )
    temp_clean = temp_dir / "transactions_clean.parquet"
    temp_excluded = temp_dir / "transactions_excluded.parquet"
    temp_audit = temp_dir / f"cleaning_run_{run_id}.json"
    duplicate_db = temp_dir / "duplicate_registry.sqlite"

    try:
        # 階段 1：掃描資料列前，完成結構、參照資料與校驗碼預檢。
        print(f"run_id={run_id}", flush=True)
        raw_columns = _validate_raw_schema(config.raw_path)
        lookup, alias_map = _load_location_references(
            config.lookup_path,
            config.aliases_path,
        )
        print("preflight=PASS", flush=True)

        print("checksum raw source", flush=True)
        source_sha256 = file_sha256(config.raw_path)
        lookup_sha256 = file_sha256(config.lookup_path)
        aliases_sha256 = file_sha256(config.aliases_path)
        print(f"raw_sha256={source_sha256}", flush=True)

        # 階段 2：掃描整次執行的 ID，避免遺漏跨批次重複值。
        connection = _open_duplicate_database(duplicate_db)
        try:
            input_rows, missing_ids, duplicate_counts = _scan_source_ids(
                config.raw_path,
                config.chunk_size,
                connection,
            )
        finally:
            connection.close()
        duplicate_kinds = _classify_duplicate_ids(
            config.raw_path,
            raw_columns,
            config.chunk_size,
            duplicate_counts,
        )
        duplicate_summary = {
            "missing_rows": missing_ids,
            "duplicate_ids": len(duplicate_counts),
            "duplicate_exact_ids": sum(
                kind == "duplicate_exact" for kind in duplicate_kinds.values()
            ),
            "duplicate_conflict_ids": sum(
                kind == "duplicate_conflict" for kind in duplicate_kinds.values()
            ),
        }
        print(f"duplicate_scan={duplicate_summary}", flush=True)

        # 階段 3：逐批排除、轉換與稽核，持續寫入同一組臨時 Parquet。
        columns_to_read = list(REQUIRED_RAW_COLUMNS)
        writers = _ParquetWriters(temp_clean, temp_excluded)
        exact_seen: set[str] = set()
        processed_rows = 0
        try:
            with pd.read_stata(
                config.raw_path,
                columns=columns_to_read,
                chunksize=config.chunk_size,
                convert_categoricals=False,
                convert_dates=False,
                convert_missing=False,
                preserve_dtypes=True,
            ) as reader:
                for chunk_number, chunk in enumerate(reader, start=1):
                    result = process_chunk(
                        chunk,
                        run_id=run_id,
                        global_start=processed_rows,
                        lookup=lookup,
                        alias_map=alias_map,
                        duplicate_kinds=duplicate_kinds,
                        exact_seen=exact_seen,
                        audit=audit,
                    )
                    writers.write_clean(result.clean)
                    writers.write_excluded(result.excluded)
                    processed_rows += len(chunk)
                    print(
                        f"cleaning chunk={chunk_number} input={processed_rows} "
                        f"clean={audit.clean_rows} excluded={audit.excluded_rows}",
                        flush=True,
                    )
        finally:
            writers.close()

        # 階段 4：先完成筆數對帳與目前來源基準，再完整讀回檔案。
        if processed_rows != input_rows:
            raise ValueError(
                f"ID scan rows={input_rows}; cleaning scan rows={processed_rows}"
            )
        if writers.clean_rows != audit.clean_rows:
            raise ValueError("clean writer row count differs from audit")
        if writers.excluded_rows != audit.excluded_rows:
            raise ValueError("excluded writer row count differs from audit")
        if audit.input_rows != audit.clean_rows + audit.excluded_rows:
            raise ValueError("input rows do not reconcile to clean plus excluded")
        if audit.clean_rows == 0:
            raise ValueError("clean output is empty")

        baseline = _validate_current_baseline(source_sha256, audit)
        print("validating Parquet readback", flush=True)
        clean_output = _validate_parquet(temp_clean, CLEAN_SCHEMA, audit.clean_rows)
        excluded_output = _validate_parquet(
            temp_excluded,
            EXCLUDED_SCHEMA,
            audit.excluded_rows,
        )

        # 只有全部發布檢核通過，稽核結果才標記為成功。
        completed_at = _utc_now()
        audit_payload = {
            "cleaning_run_id": run_id,
            "spec_version": SPEC_VERSION,
            "status": "success",
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "configuration": {"chunk_size": config.chunk_size},
            "inputs": {
                "raw": {
                    "filename": config.raw_path.name,
                    "size_bytes": config.raw_path.stat().st_size,
                    "columns": len(raw_columns),
                    "sha256": source_sha256,
                },
                "location_lookup": {
                    "filename": config.lookup_path.name,
                    "sha256": lookup_sha256,
                },
                "location_aliases": {
                    "filename": config.aliases_path.name,
                    "sha256": aliases_sha256,
                },
            },
            "duplicate_registry": duplicate_summary,
            "results": audit.as_dict(),
            "current_source_baseline": baseline,
            "publication_gates": {
                "required_raw_schema_valid": True,
                "location_reference_valid": True,
                "clean_source_transaction_id_unique": True,
                "row_counts_reconciled": True,
                "clean_nonempty": True,
                "outputs_readback_validated": True,
                "current_source_baseline_reproduced": (
                    not baseline["applies"] or not baseline["differences"]
                ),
            },
            "outputs": {
                "transactions_clean": clean_output,
                "transactions_excluded": excluded_output,
            },
        }
        _write_json(temp_audit, audit_payload)

        # 階段 5：三份檔案一起發布；失敗時復原上一版成功輸出。
        final_clean = config.output_dir / "transactions_clean.parquet"
        final_excluded = config.output_dir / "transactions_excluded.parquet"
        final_audit = config.audit_dir / temp_audit.name
        _publish_files(
            [
                (temp_clean, final_clean),
                (temp_excluded, final_excluded),
                (temp_audit, final_audit),
            ],
            temp_dir,
        )
        print("publication=PASS", flush=True)
        return CleaningResult(
            run_id=run_id,
            clean_path=final_clean,
            excluded_path=final_excluded,
            audit_path=final_audit,
            input_rows=audit.input_rows,
            clean_rows=audit.clean_rows,
            excluded_rows=audit.excluded_rows,
        )
    except Exception as error:
        # 失敗的執行不發布 Parquet，但保留小型稽核檔供除錯與追蹤。
        failure_payload = {
            "cleaning_run_id": run_id,
            "spec_version": SPEC_VERSION,
            "status": "failed",
            "started_at_utc": started_at,
            "completed_at_utc": _utc_now(),
            "inputs": {
                "raw_filename": config.raw_path.name,
                "raw_sha256": source_sha256,
                "location_lookup_sha256": lookup_sha256,
                "location_aliases_sha256": aliases_sha256,
            },
            "results_before_failure": audit.as_dict(),
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
        _write_json(failed_audit_path, failure_payload)
        raise
    finally:
        # SQLite、臨時 Parquet 與備份都只屬於本次執行。
        shutil.rmtree(temp_dir, ignore_errors=True)
