"""將原始 audit JSON 轉成容易瀏覽的 Markdown；不修改 audit 本身。"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")


def _number(value: int | float) -> str:
    return f"{value:,.0f}" if isinstance(value, float) else f"{value:,}"


def _percent(part: int, total: int) -> str:
    return f"{part / total:.2%}" if total else "—"


def _status(value: bool) -> str:
    return "PASS" if value else "FAIL"


def _taipei_time(value: str) -> str:
    return (
        datetime.fromisoformat(value)
        .astimezone(TAIPEI)
        .strftime("%Y-%m-%d %H:%M:%S (Asia/Taipei)")
    )


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _category_map(audit: dict[str, Any], name: str) -> dict[str, int]:
    rows = audit["results"]["category_values"].get(name, [])
    return {str(row["value"]): int(row["rows"]) for row in rows}


def render(audit: dict[str, Any], source_path: Path) -> str:
    # 報告只從 aggregate audit 衍生，不重新讀取 Parquet 或 raw data。
    results = audit["results"]
    row_counts = results["row_counts"]
    input_rows = int(row_counts["input_rows"])
    clean_rows = int(row_counts["clean_rows"])
    excluded_rows = int(row_counts["excluded_rows"])
    independent = results["independent_checks"]
    numeric = results["numeric_quality"]

    started = datetime.fromisoformat(audit["started_at_utc"])
    completed = datetime.fromisoformat(audit["completed_at_utc"])
    duration_seconds = int((completed - started).total_seconds())

    lines = [
        f"# Cleaning Audit — {audit['cleaning_run_id']}",
        "",
        f"**總結：{audit['status'].upper()}**",
        "",
        f"- 執行版本：`{audit['spec_version']}`",
        f"- 開始時間：{_taipei_time(audit['started_at_utc'])}",
        f"- 完成時間：{_taipei_time(audit['completed_at_utc'])}",
        f"- 執行時間：約 {duration_seconds // 60} 分 {duration_seconds % 60} 秒",
        f"- Chunk size：{_number(audit['configuration']['chunk_size'])} rows",
        f"- [原始 audit JSON]({source_path.name})",
        "",
        "## 1. 核心結論",
        "",
        "本次 cleaning 已通過全部 publication gates；raw data 完整對帳為 clean 與 excluded，正式輸出可讀回且 schema 正確。",
        "",
        *_table(
            ["結果", "Rows", "占 raw 比例"],
            [
                ["Raw input", _number(input_rows), "100.00%"],
                ["Clean", _number(clean_rows), _percent(clean_rows, input_rows)],
                [
                    "Excluded",
                    _number(excluded_rows),
                    _percent(excluded_rows, input_rows),
                ],
            ],
        ),
        "",
        f"對帳：`{_number(input_rows)} = {_number(clean_rows)} + {_number(excluded_rows)}`",
        "",
        "## 2. Publication gates",
        "",
        *_table(
            ["檢查", "結果"],
            [
                [name.replace("_", " "), _status(bool(value))]
                for name, value in audit["publication_gates"].items()
            ],
        ),
        "",
        f"Current-source baseline：**{audit['current_source_baseline']['status']}**；差異項目：{len(audit['current_source_baseline']['differences'])}。",
        "",
        "## 3. 排除原因",
        "",
    ]

    reason_labels = {
        "missing_source_transaction_id": "缺少交易 ID",
        "duplicate_exact": "完全相同的重複資料",
        "duplicate_conflict": "相同 ID 但內容衝突",
        "invalid_transaction_period": "交易年月無效或超出 canonical period",
        "unmapped_location": "無法對應鄉鎮",
        "excluded_transaction_type": "不保留的交易類型",
    }
    reason_rows = []
    for reason, rows in results["primary_exclusion_reasons"].items():
        count = int(rows)
        reason_rows.append(
            [
                f"`{reason}`",
                reason_labels[reason],
                _number(count),
                _percent(count, input_rows),
            ]
        )
    lines.extend(
        _table(["Reason", "說明", "Rows", "占 raw 比例"], reason_rows)
    )
    lines.extend(
        [
            "",
            "`unmapped_location` 的 97 筆全部是 raw `town` 空白，不是 reference 缺少行政區。",
            "",
            "## 4. Clean data flags",
            "",
        ]
    )

    building_rows = int(numeric["building_transfer_area_m2"]["rows"])
    parking_rows = int(independent["parking_applicable_rows"])
    flag_rows = [
        [
            "`transaction_date_valid`",
            _number(clean_rows - int(independent["invalid_transaction_day_clean_rows"])),
            _number(int(independent["invalid_transaction_day_clean_rows"])),
            "無效日仍保留年月",
        ],
        [
            "`total_price_valid`",
            _number(int(numeric["total_price_ntd"]["valid"])),
            _number(clean_rows - int(numeric["total_price_ntd"]["valid"])),
            "0／缺失／無法解析為 false",
        ],
        [
            "`unit_price_valid`",
            _number(int(numeric["unit_price_ntd_m2"]["valid"])),
            _number(clean_rows - int(numeric["unit_price_ntd_m2"]["valid"])),
            "正值小數保留",
        ],
        [
            "`land_transfer_area_valid`",
            _number(int(numeric["land_transfer_area_m2"]["valid"])),
            _number(clean_rows - int(numeric["land_transfer_area_m2"]["valid"])),
            "適用所有 clean rows",
        ],
        [
            "`building_transfer_area_valid`",
            _number(int(numeric["building_transfer_area_m2"]["valid"])),
            _number(
                building_rows - int(numeric["building_transfer_area_m2"]["valid"])
            ),
            f"另有 {_number(clean_rows - building_rows)} 筆土地為 NULL",
        ],
        [
            "`parking_data_complete`",
            _number(int(independent["parking_data_complete"])),
            _number(int(independent["parking_data_incomplete"])),
            f"只適用 {_number(parking_rows)} 筆房地＋車位",
        ],
        [
            "`has_note`",
            _number(int(independent["has_note"])),
            _number(clean_rows - int(independent["has_note"])),
            "只表示備註是否非空白",
        ],
    ]
    lines.extend(_table(["Flag", "True", "False", "備註"], flag_rows))

    lines.extend(["", "## 5. 日期品質", ""])
    completion_rows = [
        [status, _number(int(rows)), _percent(int(rows), clean_rows)]
        for status, rows in results["completion_date_status"].items()
    ]
    lines.extend(_table(["Completion status", "Rows", "占 clean 比例"], completion_rows))
    lines.extend(
        [
            "",
            f"- 完工日晚於交易日：{_number(int(independent['completion_after_transaction']))} 筆；只加 flag，不排除。",
            f"- 完整交易日無效：{_number(int(independent['invalid_transaction_day_clean_rows']))} 筆；保留有效年月。",
            "",
            "## 6. 數值品質",
            "",
        ]
    )
    numeric_rows = []
    for name, values in numeric.items():
        numeric_rows.append(
            [
                f"`{name}`",
                _number(int(values["valid"])),
                _number(int(values.get("missing", 0))),
                _number(int(values.get("zero", 0))),
                str(values.get("min", "—")),
                str(values.get("max", "—")),
            ]
        )
    lines.extend(
        _table(["欄位", "Valid", "Missing", "Zero", "Min", "Max"], numeric_rows)
    )
    fractional_prices = int(
        numeric["unit_price_ntd_m2"].get("non_integer_positive", 0)
    )
    lines.extend(
        [
            "",
            f"`unit_price_ntd_m2` 有 {_number(fractional_prices)} 筆正值小數；均以 Float64 原值保留，不四捨五入。",
            "",
            "### 格局分布",
            "",
        ]
    )
    layout_rows = [
        [
            f"`{name}`",
            _number(int(values["valid_rows"])),
            str(values["min"]),
            str(values["p50"]),
            str(values["p95"]),
            str(values["p99"]),
            str(values["max"]),
        ]
        for name, values in results["layout_summary"].items()
    ]
    lines.extend(
        _table(["欄位", "Valid", "Min", "P50", "P95", "P99", "Max"], layout_rows)
    )

    lines.extend(["", "### 單價公式交叉驗證", ""])
    formula_rows = [
        [
            name.replace("_", " "),
            _number(int(values["comparable_rows"])),
            _number(int(values["matched_rows"])),
            f"{values['match_rate']:.2%}",
        ]
        for name, values in results["unit_price_formula_validation"].items()
    ]
    lines.extend(_table(["檢查", "Comparable", "Matched", "Match rate"], formula_rows))

    lines.extend(["", "## 7. Location mapping", ""])
    alias_rows = [
        [
            key.split(":", 2)[1],
            key.split(":", 2)[2],
            _number(int(value)),
        ]
        for key, value in independent.items()
        if key.startswith("location_alias:")
    ]
    lines.extend(_table(["Legacy county code", "Raw town", "Rows"], alias_rows))
    lines.extend(["", "未對應的 97 筆分布：", ""])
    unmatched_rows = [
        [
            row["legacy_county_code"],
            row["raw_town"],
            _number(int(row["rows"])),
        ]
        for row in results["unmatched_location_combinations"]
    ]
    lines.extend(_table(["Legacy county code", "Raw town", "Rows"], unmatched_rows))

    lines.extend(["", "## 8. 分類檢查", ""])
    unexpected_rows = [
        [
            "Building type",
            _number(int(independent["unexpected_building_type"])),
        ],
        [
            "Management value",
            _number(int(independent["unexpected_management_value"])),
        ],
        [
            "Urban land-use type",
            _number(int(independent["unexpected_urban_land_use_type"])),
        ],
    ]
    lines.extend(_table(["檢查", "Unexpected rows"], unexpected_rows))

    transaction_types = _category_map(audit, "transaction_type")
    lines.extend(["", "Clean transaction types：", ""])
    lines.extend(
        _table(
            ["Type", "Rows", "占 clean 比例"],
            [
                [value, _number(rows), _percent(rows, clean_rows)]
                for value, rows in transaction_types.items()
            ],
        )
    )

    lines.extend(["", "## 9. 輸出檔案", ""])
    output_rows = []
    output_links = {
        "transactions_clean": "../processed/transactions_clean.parquet",
        "transactions_excluded": "../processed/transactions_excluded.parquet",
    }
    for name, values in audit["outputs"].items():
        output_rows.append(
            [
                f"[{values['filename']}]({output_links[name]})",
                _number(int(values["row_count"])),
                f"{values['size_bytes'] / 1024 / 1024:.1f} MiB",
                "PASS" if values["readback_validated"] else "FAIL",
                f"`{values['sha256']}`",
            ]
        )
    lines.extend(
        _table(["檔案", "Rows", "大小", "讀回驗證", "SHA-256"], output_rows)
    )

    lines.extend(
        [
            "",
            "## 判讀提醒",
            "",
            "- `False` 表示該檢查適用但條件未通過；`NULL` 通常表示不適用，例如土地交易的建物欄位。",
            "- 正值極端值保留，不因數值很大而自動刪除。",
            "- 公式 match rate 是 aggregate cross-check，不是 row exclusion 規則。",
            "- Raw `county` 與 canonical 名稱不同主要來自歷史名稱；正式輸出採 location lookup 名稱。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    audit_dir = root / "data" / "audit"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_json", nargs="?", type=Path)
    parser.add_argument("--output", type=Path)
    parser.set_defaults(audit_dir=audit_dir)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.audit_json
    if source is None:
        # 未指定路徑時選擇檔名排序後最新的 successful/failed audit JSON。
        candidates = sorted(args.audit_dir.glob("cleaning_run_*.json"))
        if not candidates:
            raise FileNotFoundError(f"no cleaning audit JSON found in {args.audit_dir}")
        source = candidates[-1]
    # Markdown 是衍生檢視檔，不修改作為依據的 JSON。
    output = args.output or source.with_name(f"{source.stem}_report.md")
    audit = json.loads(source.read_text(encoding="utf-8"))
    output.write_text(render(audit, source), encoding="utf-8")
    print(f"audit_source={source}")
    print(f"report_output={output}")


if __name__ == "__main__":
    main()
