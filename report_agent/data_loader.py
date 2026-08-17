# -*- coding: utf-8 -*-
"""数据加载：从 CSV 读取并按时间区间过滤。

支持两种使用方式：
  1. 单数据源（兼容旧调用）：load_csv(file, date_column, value_columns, ...)
  2. 多数据源（推荐）：load_metrics(sources, period) 一次加载所有监测指标

数据源配置格式（config.json 中）：
  "data_sources": {
    "temperature": {
      "file": "data/temperature.csv",
      "date_column": "date",
      "value_columns": ["temp_a", "temp_b"]      # 多测点 / 多列
    },
    "cable_clamp": {
      "file": "data/cable_clamp.csv",
      "date_column": "date",
      "value_columns": ["h_87L_1", "h_87R_1", "h_88L_1", "h_88R_1"]
    },
    ...
  }

支持 strict 模式（日期解析失败直接报错）和宽松模式（默认：跳过异常行但
记录 warning 日志并计数）。宽松模式下，调用方可通过 return_stats=True
获取跳过统计摘要，便于在报告中体现数据完整性信息。
"""

import csv
import datetime as dt
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("report-agent.data_loader")


# ---------------------------------------------------------------------------
# 单文件加载（兼容旧 API）
# ---------------------------------------------------------------------------

def load_csv(
    file_path: str,
    date_column: str = "date",
    value_columns: Optional[List[str]] = None,
    strict: bool = False,
    return_stats: bool = False,
) -> "List[Dict] | Tuple[List[Dict], Dict]":
    """读取 CSV 文件，返回记录列表。

    日期列会被解析为 datetime.date；数值列会转换为 float，无法解析的记为 None。

    参数:
        file_path: CSV 文件路径
        date_column: 日期列名
        value_columns: 数值列名列表（None 则自动取非日期列）
        strict: 严格模式——日期解析失败时直接抛出 ValueError，而非静默跳过
        return_stats: 是否返回加载统计（跳过行数、None 值计数等）

    返回:
        return_stats=False → List[Dict]（兼容旧调用方）
        return_stats=True  → (records, stats_dict)
    """
    if not file_path or not os.path.isfile(file_path):
        raise FileNotFoundError(f"数据文件不存在: {file_path}")

    records = []
    skipped_rows = 0
    none_value_counts: Dict[str, int] = {}
    total_rows = 0

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"数据文件为空或缺少表头: {file_path}")

        value_cols = value_columns or [
            c for c in reader.fieldnames if c != date_column
        ]
        for col in value_cols:
            none_value_counts[col] = 0

        for row_idx, row in enumerate(reader, 2):
            total_rows += 1
            rec = _parse_row(row, date_column, value_cols, row_idx, strict)
            if rec is None:
                skipped_rows += 1
                continue
            for col in value_cols:
                if rec.get(col) is None:
                    none_value_counts[col] += 1
            records.append(rec)

    records.sort(key=lambda r: r["date"])

    if skipped_rows > 0:
        log.warning(
            "数据加载完成: 共 %d 行，跳过 %d 行（日期解析失败）",
            total_rows, skipped_rows,
        )
    else:
        log.info("数据加载完成: 共 %d 行，无跳过", total_rows)

    if return_stats:
        stats = {
            "total_rows": total_rows,
            "loaded_rows": len(records),
            "skipped_rows": skipped_rows,
            "none_value_counts": none_value_counts,
        }
        return records, stats

    return records


def _parse_row(row, date_column, value_cols, row_idx, strict):
    """解析单行 CSV。失败时返回 None（strict 模式抛异常）。"""
    rec = {}
    raw_date = (row.get(date_column) or "").strip()
    try:
        rec["date"] = dt.date.fromisoformat(raw_date)
    except ValueError:
        try:
            rec["date"] = dt.datetime.strptime(raw_date, "%Y/%m/%d").date()
        except ValueError:
            if strict:
                raise ValueError(
                    f"第 {row_idx} 行日期解析失败: '{raw_date}'"
                    f"（列 {date_column}），strict 模式下不跳过"
                )
            log.warning(
                "第 %d 行日期解析失败: '%s'，跳过该行",
                row_idx, raw_date,
            )
            return None

    for col in value_cols:
        raw = (row.get(col) or "").strip()
        try:
            rec[col] = float(raw)
        except ValueError:
            rec[col] = None
            if strict:
                raise ValueError(
                    f"第 {row_idx} 行数值解析失败: '{raw}'"
                    f"（列 {col}），strict 模式下不跳过"
                )
            log.warning(
                "第 %d 行列 '%s' 值 '%s' 无法解析为数值，记为 None",
                row_idx, col, raw,
            )
    return rec


def filter_period(records: List[Dict], start: dt.date, end: dt.date) -> List[Dict]:
    """按 [start, end] 闭区间过滤记录。"""
    return [r for r in records if start <= r["date"] <= end]


# ---------------------------------------------------------------------------
# 多数据源加载（新 API，支持按指标路由）
# ---------------------------------------------------------------------------

class DataSource:
    """单个监测指标的数据源。"""

    def __init__(self, name: str, cfg: Dict, base_dir: str = ""):
        self.name = name
        self.cfg = cfg or {}
        self.base_dir = base_dir
        self.file = self._resolve_path(cfg.get("file", ""))
        self.date_column = cfg.get("date_column", "date")
        # value_columns: 监测列名（多测点/多指标 → 多列）
        self.value_columns = cfg.get("value_columns", [])
        # 描述：用于日志和报告
        self.label = cfg.get("label", name)
        self.unit = cfg.get("unit", "")
        # 缓存：按文件内容懒加载
        self._records: Optional[List[Dict]] = None
        self._load_stats: Optional[Dict] = None

    def _resolve_path(self, p: str) -> str:
        if not p:
            return ""
        if os.path.isabs(p):
            return p
        if self.base_dir:
            return os.path.join(self.base_dir, p)
        return p

    def available(self) -> bool:
        return bool(self.file) and os.path.isfile(self.file)

    def load(self, strict: bool = False) -> List[Dict]:
        if self._records is not None:
            return self._records
        if not self.available():
            log.warning("[%s] 数据文件不存在: %s（将返回空数据）", self.name, self.file)
            self._records = []
            self._load_stats = {"total_rows": 0, "loaded_rows": 0, "skipped_rows": 0}
            return self._records
        self._records, self._load_stats = load_csv(
            self.file,
            date_column=self.date_column,
            value_columns=self.value_columns or None,
            strict=strict,
            return_stats=True,
        )
        return self._records

    def filter_period(self, start: dt.date, end: dt.date) -> List[Dict]:
        return filter_period(self.load(), start, end)

    def load_stats(self) -> Dict:
        if self._load_stats is None:
            self.load()
        return self._load_stats or {}


class DataSourceRegistry:
    """多数据源注册表：按指标名（metric）路由到对应数据源。"""

    def __init__(self, sources_cfg: Optional[Dict] = None, base_dir: str = ""):
        self.sources: Dict[str, DataSource] = {}
        for name, cfg in (sources_cfg or {}).items():
            self.sources[name] = DataSource(name, cfg, base_dir)
        # 别名映射（如 "wind" → "wind_speed"、"temp" → "temperature"）
        self.aliases = {
            "temp": "temperature",
            "humid": "humidity",
            "wind": "wind_speed",
            "vehicle": "vehicle_count",
            "load": "vehicle_load",
        }

    def get(self, metric: str) -> Optional[DataSource]:
        """根据指标名获取数据源（支持别名）。"""
        if metric in self.sources:
            return self.sources[metric]
        if metric in self.aliases and self.aliases[metric] in self.sources:
            return self.sources[self.aliases[metric]]
        return None

    def available_metrics(self) -> List[str]:
        return [n for n, s in self.sources.items() if s.available()]

    def all_metrics(self) -> List[str]:
        return list(self.sources.keys())

    def summary(self) -> Dict[str, Dict]:
        """返回每个数据源的加载摘要。"""
        out = {}
        for name, src in self.sources.items():
            try:
                stats = src.load_stats()
            except Exception as exc:  # noqa: BLE001
                stats = {"error": str(exc)}
            out[name] = {
                "file": src.file,
                "available": src.available(),
                "label": src.label,
                "unit": src.unit,
                **stats,
            }
        return out


def load_metrics(
    registry: DataSourceRegistry,
    metric: str,
    start: dt.date,
    end: dt.date,
) -> List[Dict]:
    """根据指标名加载数据源并按时间区间过滤。

    返回空 list 表示该指标无数据源/数据。
    """
    src = registry.get(metric)
    if src is None:
        log.debug("指标 '%s' 没有注册数据源", metric)
        return []
    return src.filter_period(start, end)


# ---------------------------------------------------------------------------
# 占位符 → 数据源 → 统计值/单元格值的解析
# ---------------------------------------------------------------------------

STAT_FUNCTIONS = {
    "max": "max", "min": "min", "avg": "avg", "mean": "avg",
    "median": "median", "std": "std", "range": "range",
    "sum": "sum", "count": "count",
    # 桥梁统计语义
    "绝对最大值": "abs_max", "绝对最大": "abs_max",
    "均方根": "rms", "差值": "range",
}


def _column_stats(values: List[float], stat: str):
    """对一列数值做指定统计。返回 float 或 None。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if stat in ("max", "min", "avg", "sum", "count"):
        if stat == "max": return max(vals)
        if stat == "min": return min(vals)
        if stat == "avg": return sum(vals) / len(vals)
        if stat == "sum": return sum(vals)
        if stat == "count": return float(len(vals))
    if stat == "abs_max":
        return max(vals, key=abs)
    if stat == "median":
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    if stat == "std":
        avg = sum(vals) / len(vals)
        return (sum((v - avg) ** 2 for v in vals) / len(vals)) ** 0.5
    if stat == "rms":
        return (sum(v ** 2 for v in vals) / len(vals)) ** 0.5
    if stat == "range":
        return max(vals) - min(vals)
    return None


def resolve_cell(
    registry: DataSourceRegistry,
    metric: str,
    column: str,
    stat: str,
    period: Dict,
) -> Optional[float]:
    """根据 metric/column/stat 计算一个单元格值。

    用法:
        resolve_cell(reg, "cable_clamp", "h_87L_1", "avg", period)
        → 索夹 87-L_1 测点的平均值

    当 column 不在数据源的 value_columns 中时（如 row_label 描述而非列名），
    自动降级为该 metric 在报告期内的整体统计值。
    """
    actual_stat = STAT_FUNCTIONS.get(stat, stat)
    src = registry.get(metric)
    if src is None:
        return None
    records = load_metrics(registry, metric, period["start"], period["end"])
    if not records:
        return None
    # 列匹配 → 使用该列的数据；否则降级为 metric 整体统计
    if column and column in src.value_columns:
        values = [r.get(column) for r in records]
        result = _column_stats([v for v in values if v is not None], actual_stat)
        if result is not None:
            return result
    # 降级：使用所有 value_columns 合并计算
    return resolve_metric_stat(registry, metric, stat, period)


def resolve_metric_stat(
    registry: DataSourceRegistry,
    metric: str,
    stat: str,
    period: Dict,
    column: Optional[str] = None,
) -> Optional[float]:
    """根据 metric/stat 计算一个指标级统计值。

    用法:
        resolve_metric_stat(reg, "temperature", "max", period)
        → 该季度温度最大值
        resolve_metric_stat(reg, "temperature", "max", period, column="env_temp")
        → 该季度环境温度最大值
    """
    actual_stat = STAT_FUNCTIONS.get(stat, stat)
    records = load_metrics(registry, metric, period["start"], period["end"])
    if not records:
        return None
    src = registry.get(metric)
    cols = [column] if column else (src.value_columns if src else [])
    all_vals = []
    for col in cols:
        for r in records:
            v = r.get(col)
            if v is not None:
                all_vals.append(v)
    return _column_stats(all_vals, actual_stat)


# ---------------------------------------------------------------------------
# 旧 API 兼容
# ---------------------------------------------------------------------------

def filter_period_multi(
    sources: Dict[str, List[Dict]],
    start: dt.date,
    end: dt.date,
) -> Dict[str, List[Dict]]:
    """对多数据源记录按时间区间过滤。"""
    return {
        metric: filter_period(records, start, end)
        for metric, records in sources.items()
    }
