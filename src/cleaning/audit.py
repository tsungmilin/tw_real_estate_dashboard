"""清理執行的彙總稽核；不保存逐筆原始資料。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .contract import PRIMARY_EXCLUSION_REASONS


def _counter_rows(counter: Counter[Any], limit: int = 50) -> list[dict[str, Any]]:
    # 只輸出最常見值，避免高基數分類讓 audit JSON 膨脹。
    return [
        {"value": str(value), "rows": int(rows)}
        for value, rows in counter.most_common(limit)
    ]


def _nearest_rank_percentile(counter: Counter[int], percentile: float) -> int | None:
    # 格局值基數很低，可直接從 value counts 算 exact nearest-rank percentile。
    total = sum(counter.values())
    if total == 0:
        return None
    target = max(1, int(np.ceil(percentile * total)))
    cumulative = 0
    for value, count in sorted(counter.items()):
        cumulative += count
        if cumulative >= target:
            return int(value)
    return int(max(counter))


@dataclass
class AuditAccumulator:
    """跨批次累加統計，但不保留原始資料列或交易 ID。"""

    input_rows: int = 0
    clean_rows: int = 0
    excluded_rows: int = 0
    primary_reasons: Counter[str] = field(default_factory=Counter)
    independent_checks: Counter[str] = field(default_factory=Counter)
    unmatched_locations: Counter[tuple[str, str]] = field(default_factory=Counter)
    completion_status: Counter[str] = field(default_factory=Counter)
    categories: dict[str, Counter[str]] = field(
        default_factory=lambda: {
            "transaction_type": Counter(),
            "building_type": Counter(),
            "primary_use": Counter(),
            "manage": Counter(),
            "urban_land_use_type": Counter(),
            "nonurban_land_use_zone": Counter(),
        }
    )
    numeric_quality: dict[str, Counter[str]] = field(default_factory=dict)
    numeric_min_max: dict[str, dict[str, float | int | None]] = field(
        default_factory=dict
    )
    layout_values: dict[str, Counter[int]] = field(
        default_factory=lambda: {
            "room_count": Counter(),
            "hall_count": Counter(),
            "bathroom_count": Counter(),
        }
    )
    formula_validation: dict[str, Counter[str]] = field(
        default_factory=lambda: {
            "house_unit_price": Counter(),
            "parking_adjusted_unit_price": Counter(),
        }
    )

    def update_primary(self, reasons: pd.Series) -> None:
        # 每筆 excluded row 只會有一個 primary reason。
        counts = reasons.dropna().value_counts()
        self.primary_reasons.update(
            {str(reason): int(rows) for reason, rows in counts.items()}
        )

    def update_unmatched(
        self,
        county_codes: pd.Series,
        towns: pd.Series,
        mask: pd.Series,
    ) -> None:
        # 只保存無法對應的 county/town 組合與筆數，不保存 row-level 內容。
        if not bool(mask.any()):
            return
        county = county_codes.loc[mask].fillna("<BLANK>").astype(str)
        town = towns.loc[mask].fillna("<BLANK>").astype(str)
        self.unmatched_locations.update(zip(county, town, strict=True))

    def update_category(self, name: str, values: pd.Series) -> None:
        normalized = values.fillna("<NULL>").astype(str)
        self.categories[name].update(normalized.tolist())

    def update_numeric(
        self,
        name: str,
        numeric: pd.Series,
        valid: pd.Series,
        *,
        non_integer: pd.Series | None = None,
    ) -> None:
        # 有效性、缺值與極值集中在稽核結果；極端正值本身不會被刪除。
        stats = self.numeric_quality.setdefault(name, Counter())
        stats["rows"] += len(numeric)
        stats["missing"] += int(numeric.isna().sum())
        stats["zero"] += int(numeric.eq(0).sum())
        stats["negative"] += int(numeric.lt(0).sum())
        stats["valid"] += int(valid.sum())
        if non_integer is not None:
            stats["non_integer_positive"] += int(non_integer.sum())

        valid_values = numeric.loc[valid]
        if valid_values.empty:
            return
        observed_min = valid_values.min()
        observed_max = valid_values.max()
        bounds = self.numeric_min_max.setdefault(name, {"min": None, "max": None})
        if bounds["min"] is None or observed_min < bounds["min"]:
            bounds["min"] = float(observed_min)
        if bounds["max"] is None or observed_max > bounds["max"]:
            bounds["max"] = float(observed_max)

    def update_layout(self, name: str, values: pd.Series) -> None:
        parsed = values.dropna().astype(int)
        self.layout_values[name].update(parsed.tolist())

    def update_formula(self, name: str, comparable: int, matched: int) -> None:
        self.formula_validation[name]["comparable_rows"] += int(comparable)
        self.formula_validation[name]["matched_rows"] += int(matched)

    def as_dict(self) -> dict[str, Any]:
        # 在結束時才轉成 JSON-friendly 結構與比例，chunk processing 保持輕量。
        primary = {
            reason: int(self.primary_reasons.get(reason, 0))
            for reason in PRIMARY_EXCLUSION_REASONS
        }
        numeric = {}
        for name, values in sorted(self.numeric_quality.items()):
            rows = int(values.get("rows", 0))
            numeric[name] = {
                key: int(value)
                for key, value in sorted(values.items())
            }
            numeric[name].update(self.numeric_min_max.get(name, {}))
            numeric[name]["valid_rate"] = (
                float(values.get("valid", 0) / rows) if rows else None
            )

        layouts = {}
        for name, values in self.layout_values.items():
            layouts[name] = {
                "valid_rows": int(sum(values.values())),
                "min": int(min(values)) if values else None,
                "p50": _nearest_rank_percentile(values, 0.50),
                "p95": _nearest_rank_percentile(values, 0.95),
                "p99": _nearest_rank_percentile(values, 0.99),
                "max": int(max(values)) if values else None,
            }

        formulas = {}
        for name, values in self.formula_validation.items():
            comparable = int(values.get("comparable_rows", 0))
            matched = int(values.get("matched_rows", 0))
            formulas[name] = {
                "comparable_rows": comparable,
                "matched_rows": matched,
                "match_rate": float(matched / comparable) if comparable else None,
            }

        return {
            "row_counts": {
                "input_rows": int(self.input_rows),
                "clean_rows": int(self.clean_rows),
                "excluded_rows": int(self.excluded_rows),
                "reconciled": self.input_rows == self.clean_rows + self.excluded_rows,
            },
            "primary_exclusion_reasons": primary,
            "independent_checks": {
                key: int(value)
                for key, value in sorted(self.independent_checks.items())
            },
            "unmatched_location_combinations": [
                {
                    "legacy_county_code": county_code,
                    "raw_town": town,
                    "rows": int(rows),
                }
                for (county_code, town), rows in self.unmatched_locations.most_common()
            ],
            "completion_date_status": {
                key: int(value)
                for key, value in sorted(self.completion_status.items())
            },
            "numeric_quality": numeric,
            "layout_summary": layouts,
            "unit_price_formula_validation": formulas,
            "category_values": {
                name: _counter_rows(values)
                for name, values in self.categories.items()
            },
        }
