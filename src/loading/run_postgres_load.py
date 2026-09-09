"""從命令列啟動 PostgreSQL staging 載入流程。"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from .postgres_loader import LoadConfig, ROOT, load_postgres_staging


def parse_args() -> argparse.Namespace:
    """解析命令列參數，不在這個階段連線資料庫或讀取 Parquet。"""
    parser = argparse.ArgumentParser(description=__doc__)

    # 預設使用 cleaning pipeline 發布的正式 clean Parquet；需要驗證其他檔案時，
    # 可透過 --parquet 明確指定路徑。
    parser.add_argument(
        "--parquet",
        type=Path,
        default=ROOT / "data" / "processed" / "transactions_clean.parquet",
    )

    # 主機、連接埠、帳號與密碼沿用標準 PG* 環境變數；這裡只處理資料庫名稱。
    parser.add_argument(
        "--database",
        default=os.environ.get("PGDATABASE", "real_estate_dashboard"),
        help="PostgreSQL database name or conninfo; other connection settings use PG* env vars.",
    )

    # 保留替換 psql 執行檔的能力，方便不同本機環境或測試使用。
    parser.add_argument(
        "--psql",
        default=shutil.which("psql") or "psql",
    )

    # 正常載入會先套用可重跑的 staging DDL；只有已確定 schema 存在時才跳過。
    parser.add_argument(
        "--skip-ddl",
        action="store_true",
        help="Do not apply the idempotent sql/staging DDL before loading.",
    )
    return parser.parse_args()


def main() -> None:
    """組合載入設定、執行 staging 載入，並輸出批次對帳結果。"""
    args = parse_args()
    result = load_postgres_staging(
        LoadConfig(
            parquet_path=args.parquet,
            database=args.database,
            psql_path=args.psql,
            apply_ddl=not args.skip_ddl,
        )
    )

    # 使用 key=value 逐行輸出，方便人在終端閱讀，也方便其他指令擷取結果。
    print(f"load_batch_id={result.load_batch_id}")
    print(f"cleaning_run_id={result.cleaning_run_id}")
    print(f"parquet_rows={result.parquet_rows}")
    print(f"rows_inserted={result.rows_inserted}")
    print(f"rows_updated={result.rows_updated}")
    print(f"rows_unchanged={result.rows_unchanged}")
    print(f"rows_deleted={result.rows_deleted}")
    print(f"target_rows={result.target_rows}")
    print(f"validation={result.validation_summary}")


if __name__ == "__main__":
    # 只有以 `python -m src.loading.run_postgres_load` 執行時才啟動載入；
    # 單純 import 此模組不會改動資料庫。
    main()
