"""從命令列完整重建分析層，或依單一批次增量更新。"""

from __future__ import annotations

import argparse
import os
import shutil

from .postgres_loader import (
    AnalyticsConfig,
    refresh_analytics_batch,
    refresh_analytics_full,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get("PGDATABASE", "real_estate_dashboard"),
        help="PostgreSQL database name or conninfo; other settings use PG* env vars.",
    )
    parser.add_argument("--psql", default=shutil.which("psql") or "psql")
    parser.add_argument(
        "--skip-ddl",
        action="store_true",
        help="Do not apply idempotent Analytics DDL before refreshing.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load-batch-id")
    mode.add_argument("--full", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = AnalyticsConfig(
        database=args.database,
        psql_path=args.psql,
        apply_ddl=not args.skip_ddl,
    )
    result = (
        refresh_analytics_full(config)
        if args.full
        else refresh_analytics_batch(config, args.load_batch_id)
    )
    print(f"analytics_run_id={result.analytics_run_id}")
    print(f"refresh_mode={result.refresh_mode}")
    print(f"load_batch_id={result.load_batch_id or ''}")
    print(f"status={result.status}")
    print(f"attempt_count={result.attempt_count}")
    print(f"refresh_summary={result.refresh_summary}")
    print(f"validation_summary={result.validation_summary}")


if __name__ == "__main__":
    main()
