"""從命令列執行資料清理 v1。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .contract import DEFAULT_CHUNK_SIZE
from .pipeline import CleaningConfig, run_cleaning


def parse_args() -> argparse.Namespace:
    # 預設路徑全部相對 project root，避免依賴特定執行環境的絕對路徑。
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw",
        type=Path,
        default=root / "data" / "raw" / "house_preowned_data_2.0.dta",
    )
    parser.add_argument(
        "--lookup",
        type=Path,
        default=root / "data" / "reference" / "location_lookup.csv",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=root / "data" / "reference" / "location_aliases.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=root / "data" / "processed",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=root / "data" / "audit",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    return parser.parse_args()


def main() -> None:
    # 命令列介面只負責組態與顯示結果；清理與發布規則集中在 pipeline.py。
    args = parse_args()
    result = run_cleaning(
        CleaningConfig(
            raw_path=args.raw,
            lookup_path=args.lookup,
            aliases_path=args.aliases,
            output_dir=args.output_dir,
            audit_dir=args.audit_dir,
            chunk_size=args.chunk_size,
        )
    )
    print(f"run_id={result.run_id}")
    print(f"input_rows={result.input_rows}")
    print(f"clean_rows={result.clean_rows}")
    print(f"excluded_rows={result.excluded_rows}")
    print(f"clean_output={result.clean_path}")
    print(f"excluded_output={result.excluded_path}")
    print(f"audit_output={result.audit_path}")


if __name__ == "__main__":
    main()
