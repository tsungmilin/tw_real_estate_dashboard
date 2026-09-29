"""從命令列封存並清理一個 MOI 發布批次，並更新發布紀錄。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from src.cleaning.contract import CANONICAL_PERIOD_START, DEFAULT_CHUNK_SIZE
from src.cleaning.pipeline import CleaningConfig, run_cleaning

from .moi_releases import (
    read_release,
    register_moi_release,
    update_release_cleaning,
)


ROOT = Path(__file__).resolve().parents[2]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--release-date", required=True, help="發布日期 YYYY-MM-DD")
    parser.add_argument(
        "--period-end",
        type=int,
        required=True,
        help="發布月份的民國年月 YYYMM",
    )
    parser.add_argument(
        "--source-url",
        default="https://plvr.land.moi.gov.tw/DownloadOpenData",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        default=ROOT / "data" / "raw" / "moi" / "releases",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "data" / "processed" / "moi",
    )
    parser.add_argument(
        "--audit-root",
        type=Path,
        default=ROOT / "data" / "audit" / "moi",
    )
    parser.add_argument(
        "--lookup",
        type=Path,
        default=ROOT / "data" / "reference" / "location_lookup.csv",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=ROOT / "data" / "reference" / "location_aliases.csv",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--period-start",
        type=int,
        default=CANONICAL_PERIOD_START,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registration = register_moi_release(
        args.source,
        official_release_date=args.release_date,
        period_end=args.period_end,
        release_root=args.release_root,
        source_url=args.source_url,
    )
    existing = read_release(args.release_root, registration.release_id)
    output_dir = args.output_root / registration.release_id
    audit_dir = args.audit_root / registration.release_id
    clean_path = output_dir / "transactions_clean.parquet"
    excluded_path = output_dir / "transactions_excluded.parquet"
    cleaning = existing.get("cleaning", {})
    if (
        not registration.created
        and cleaning.get("status") == "success"
        and clean_path.is_file()
        and excluded_path.is_file()
    ):
        print(f"release_id={registration.release_id}")
        print("release_registration=noop_same_checksum")
        print("cleaning=noop_already_successful")
        print(f"clean_output={clean_path}")
        return

    update_release_cleaning(
        args.release_root,
        registration.release_id,
        {
            "status": "running",
            "started_at_utc": _utc_now(),
        },
    )
    try:
        result = run_cleaning(
            CleaningConfig(
                raw_path=registration.archive_path,
                lookup_path=args.lookup,
                aliases_path=args.aliases,
                output_dir=output_dir,
                audit_dir=audit_dir,
                chunk_size=args.chunk_size,
                period_start=args.period_start,
                period_end=registration.period_end,
                source_release_id=registration.release_id,
            )
        )
    except Exception as error:
        update_release_cleaning(
            args.release_root,
            registration.release_id,
            {
                "status": "failed",
                "completed_at_utc": _utc_now(),
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        raise

    update_release_cleaning(
        args.release_root,
        registration.release_id,
        {
            "status": "success",
            "completed_at_utc": _utc_now(),
            "cleaning_run_id": result.run_id,
            "input_rows": result.input_rows,
            "clean_rows": result.clean_rows,
            "excluded_rows": result.excluded_rows,
            "clean_path": str(result.clean_path.resolve()),
            "excluded_path": str(result.excluded_path.resolve()),
            "audit_path": str(result.audit_path.resolve()),
        },
    )
    print(f"release_id={registration.release_id}")
    print(
        "release_registration="
        f"{'created' if registration.created else 'resumed_same_checksum'}"
    )
    print(f"source_sha256={registration.source_sha256}")
    print(f"cleaning_run_id={result.run_id}")
    print(f"input_rows={result.input_rows}")
    print(f"clean_rows={result.clean_rows}")
    print(f"excluded_rows={result.excluded_rows}")
    print(f"clean_output={result.clean_path}")
    print(f"audit_output={result.audit_path}")


if __name__ == "__main__":
    main()
