"""從命令列依序執行 PostgreSQL `staging`、`core` 與 `analytics`。"""

from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.moi_releases import (
    MoiReleaseError,
    read_release,
    update_release_publication,
)

from .pipeline import PipelineConfig, ROOT, run_pipeline


DEFAULT_RELEASE_ROOT = ROOT / "data" / "raw" / "moi" / "releases"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    """解析共同輸入；不在這個階段讀取 Parquet 或連線資料庫。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=ROOT / "data" / "processed" / "transactions_clean.parquet",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("PGDATABASE", "real_estate_dashboard"),
        help="PostgreSQL 資料庫名稱或連線字串；其他設定使用 PG* 環境變數。",
    )
    parser.add_argument(
        "--psql",
        default=shutil.which("psql") or "psql",
    )
    parser.add_argument(
        "--skip-ddl",
        action="store_true",
        help="載入前不要套用可重複執行的 staging、core 與 analytics DDL。",
    )
    parser.add_argument(
        "--release-id",
        help="已登錄的 MOI 發布批次 ID；必須搭配 --publication-target。",
    )
    parser.add_argument(
        "--publication-target",
        choices=("test", "production"),
        help="將成功的 SQL 發布結果寫回發布紀錄。",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        default=DEFAULT_RELEASE_ROOT,
    )
    return parser.parse_args()


def _registered_release(args: argparse.Namespace) -> dict[str, object] | None:
    if bool(args.release_id) != bool(args.publication_target):
        raise MoiReleaseError(
            "--release-id and --publication-target must be provided together"
        )
    if not args.release_id:
        return None
    release = read_release(args.release_root, args.release_id)
    cleaning = release.get("cleaning", {})
    if cleaning.get("status") != "success":
        raise MoiReleaseError(
            f"release cleaning has not succeeded: {args.release_id}"
        )
    recorded_path = cleaning.get("clean_path")
    if not recorded_path or Path(str(recorded_path)).resolve() != args.parquet.resolve():
        raise MoiReleaseError(
            "--parquet does not match the clean output registered for this release"
        )
    return release


def main() -> None:
    """執行完整資料流程，並輸出三層已提交的對帳結果。"""
    args = parse_args()
    release = _registered_release(args)
    if release is not None:
        publication = release.get("publications", {}).get(
            args.publication_target,
            {},
        )
        if publication.get("status") == "success":
            print(f"release_id={args.release_id}")
            print(f"publication_target={args.publication_target}")
            print("publication=noop_already_successful")
            print(f"load_batch_id={publication.get('load_batch_id')}")
            return

    try:
        result = run_pipeline(
            PipelineConfig(
                parquet_path=args.parquet,
                database=args.database,
                psql_path=args.psql,
                apply_ddl=not args.skip_ddl,
            )
        )
    except Exception as error:
        if release is not None:
            update_release_publication(
                args.release_root,
                args.release_id,
                args.publication_target,
                {
                    "status": "failed",
                    "database": args.database,
                    "completed_at_utc": _utc_now(),
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
        raise

    if release is not None:
        update_release_publication(
            args.release_root,
            args.release_id,
            args.publication_target,
            {
                "status": "success",
                "database": args.database,
                "completed_at_utc": _utc_now(),
                "load_batch_id": result.staging.load_batch_id,
                "staging_rows_inserted": result.staging.rows_inserted,
                "staging_rows_updated": result.staging.rows_updated,
                "staging_rows_unchanged": result.staging.rows_unchanged,
                "core_rows_inserted": result.core.rows_inserted,
                "core_rows_updated": result.core.rows_updated,
                "core_rows_unchanged": result.core.rows_unchanged,
                "analytics_run_id": result.analytics.analytics_run_id,
                "analytics_status": result.analytics.status,
            },
        )

    print(f"load_batch_id={result.staging.load_batch_id}")
    print(f"staging_rows_inserted={result.staging.rows_inserted}")
    print(f"staging_rows_updated={result.staging.rows_updated}")
    print(f"staging_rows_unchanged={result.staging.rows_unchanged}")
    print(f"staging_rows_deleted={result.staging.rows_deleted}")
    print(f"core_rows_inserted={result.core.rows_inserted}")
    print(f"core_rows_updated={result.core.rows_updated}")
    print(f"core_rows_unchanged={result.core.rows_unchanged}")
    print(f"core_rows_applied={result.core.rows_applied}")
    print(f"core_attempt_count={result.core.attempt_count}")
    print(f"analytics_run_id={result.analytics.analytics_run_id}")
    print(f"analytics_status={result.analytics.status}")
    print(f"analytics_attempt_count={result.analytics.attempt_count}")
    print(f"recovered_core_batches={len(result.recovered_core_batches)}")
    print(
        "recovered_analytics_batches="
        f"{len(result.recovered_analytics_batches)}"
    )


if __name__ == "__main__":
    main()
