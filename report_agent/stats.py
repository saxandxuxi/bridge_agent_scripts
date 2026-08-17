# -*- coding: utf-8 -*-
"""统计计算：最大值、最小值、平均值、中位数、标准差等。"""

import datetime as dt
import statistics
from typing import Dict, List, Optional, Sequence


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def compute_stats(
    records: List[Dict],
    value_columns: Optional[Sequence[str]] = None,
    thresholds: Sequence[float] = (),
) -> Dict:
    """计算每个数值列的统计量。

    返回结构::
        {
          "days": 数据天数,
          "temperature": {
              "max": .., "min": .., "avg": .., "median": .., "std": ..,
              "p25": .., "p75": .., "range": ..,
              "max_date": "2026-07-30", "min_date": "2026-07-28",
              "days_above_30": .., "days_above_35": ..
          },
          ...
        }
    """
    stats: Dict = {"days": len(records)}
    if not records:
        return stats

    cols = value_columns or [c for c in records[0] if c != "date"]
    for col in cols:
        values = [float(r[col]) for r in records if r.get(col) is not None]
        if not values:
            continue
        s: Dict = {
            "max": max(values),
            "min": min(values),
            "avg": sum(values) / len(values),
            "median": statistics.median(values),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "p25": _percentile(values, 0.25),
            "p75": _percentile(values, 0.75),
            "range": max(values) - min(values),
        }

        max_val, min_val = s["max"], s["min"]
        s["max_date"] = next(
            (r["date"] for r in records if r.get(col) is not None and float(r[col]) == max_val),
            None,
        )
        s["min_date"] = next(
            (r["date"] for r in records if r.get(col) is not None and float(r[col]) == min_val),
            None,
        )

        for th in thresholds:
            s[f"days_above_{th:g}"] = sum(1 for v in values if v > th)

        stats[col] = s
    return stats


def format_date(value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value)
