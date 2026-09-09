"""從命令列初始化 PostgreSQL core，或同步一個 staging batch。"""

from __future__ import annotations

import argparse
import os
import shutil

from .postgres_loader import CoreConfig, initialize_core, sync_core_batch


def parse_args() -> argparse.Namespace:
    """解析操作模式與連線參數；這個階段不連線資料庫。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default=os.environ.get("PGDATABASE", "real_estate_dashboard"),
        help="PostgreSQL database name or conninfo; other settings use PG* env vars.",
    )
    parser.add_argument(
        "--psql",
        default=shutil.which("psql") or "psql",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync one explicit successful staging load batch.",
    )
    sync_parser.add_argument("--load-batch-id", required=True)
    sync_parser.add_argument(
        "--skip-ddl",
        action="store_true",
        help="Do not apply the idempotent core table DDL before syncing.",
    )
    subparsers.add_parser(
        "initialize",
        help="Explicitly initialize dimensions and fully UPSERT staging into core.",
    )
    return parser.parse_args()


def main() -> None:
    """執行明確選定的 core 模式並輸出可讀、可擷取的結果。"""
    args = parse_args()
    config = CoreConfig(
        database=args.database,
        psql_path=args.psql,
        apply_ddl=not getattr(args, "skip_ddl", False),
    )

    if args.command == "initialize":
        applied = initialize_core(config)
        print("mode=initialize")
        print(f"sql_files={','.join(applied)}")
        return

    result = sync_core_batch(config, args.load_batch_id)
    print("mode=incremental")
    print(f"load_batch_id={result.load_batch_id}")
    print(f"status={result.status}")
    print(f"source_rows={result.source_rows}")
    print(f"rows_inserted={result.rows_inserted}")
    print(f"rows_updated={result.rows_updated}")
    print(f"rows_unchanged={result.rows_unchanged}")
    print(f"rows_applied={result.rows_applied}")
    print(f"attempt_count={result.attempt_count}")
    print(f"validation={result.validation_summary}")


if __name__ == "__main__":
    main()
