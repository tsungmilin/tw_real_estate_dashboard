"""從命令列依序執行 PostgreSQL staging、core 與 Analytics。"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from .pipeline import PipelineConfig, ROOT, run_pipeline


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
        help="PostgreSQL database name or conninfo; other settings use PG* env vars.",
    )
    parser.add_argument(
        "--psql",
        default=shutil.which("psql") or "psql",
    )
    parser.add_argument(
        "--skip-ddl",
        action="store_true",
        help="Do not apply idempotent staging/core/Analytics DDL before loading.",
    )
    return parser.parse_args()


def main() -> None:
    """執行完整資料流程，並輸出三層已提交的對帳結果。"""
    args = parse_args()
    result = run_pipeline(
        PipelineConfig(
            parquet_path=args.parquet,
            database=args.database,
            psql_path=args.psql,
            apply_ddl=not args.skip_ddl,
        )
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
