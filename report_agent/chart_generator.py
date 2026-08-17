# -*- coding: utf-8 -*-
"""图表生成：统一使用 matplotlib 出图（已移除 MATLAB 路径）。

支持两种数据源使用方式：
  1. 传统方式：传入 records（单数据源已过滤数据）
  2. 多数据源方式：传入 data_registry + period（按 chart.metric 字段路由到对应数据源）
"""

import logging
import os
from typing import Dict, List, Optional

log = logging.getLogger("report-agent.charts")


def _round_list(values, ndigits=2):
    return [round(float(v), ndigits) for v in values if v is not None]


def _records_for_chart(d: Dict, records: List[Dict],
                       data_registry=None, period=None) -> List[Dict]:
    """根据 chart_def 获取对应的 records。

    优先使用 chart_def.column 直接从 records 取；
    否则按 chart_def.metric 从 data_registry 取（如果提供）。
    """
    if data_registry is not None and period is not None and d.get("metric"):
        try:
            from .data_loader import load_metrics
            return load_metrics(data_registry, d["metric"], period["start"], period["end"])
        except Exception as exc:  # noqa: BLE001
            log.warning("[chart %s] 从数据源加载 %s 失败: %s，回退到默认 records",
                       d.get("id"), d.get("metric"), exc)
    return records


def _pie_aggregate(values: list, bins: int, labels: list = None):
    """把连续数值按区间分箱，返回 (sizes, labels) 用于饼图。"""
    values = [v for v in values if v is not None]
    if not values:
        return [], []
    vmin, vmax = min(values), max(values)
    if vmin == vmax:
        return [len(values)], [f"{vmin:.1f}"]
    step = (vmax - vmin) / bins
    edges = [vmin + i * step for i in range(bins + 1)]
    sizes = [0] * bins
    for v in values:
        idx = min(int((v - vmin) / step), bins - 1)
        sizes[idx] += 1
    if labels and len(labels) == bins:
        return sizes, labels
    auto_labels = [
        f"{edges[i]:.1f}~{edges[i + 1]:.1f}" for i in range(bins)
    ]
    return sizes, auto_labels


def generate_charts(
    chart_defs: List[Dict],
    records: List[Dict],
    out_dir: str,
    engine: str = "auto",
    matlab_cfg: Dict = None,
    data_registry=None,
    period=None,
) -> Dict[str, str]:
    """生成所有图表，返回 {chart_id: png_path}。

    参数:
        chart_defs: 图表定义列表
        records: 默认记录（兼容旧 API）
        out_dir: 输出目录
        engine: 保留参数（统一使用 Python 出图）
        data_registry: 多数据源注册表（用于按 chart.metric 路由）
        period: 时间区间（用于从数据源取数据）
    """
    os.makedirs(out_dir, exist_ok=True)
    return _generate_with_matplotlib(chart_defs, records, out_dir,
                                      data_registry, period)


def _generate_with_matplotlib(
    chart_defs: List[Dict],
    records: List[Dict],
    out_dir: str,
    data_registry=None,
    period=None,
) -> Dict[str, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 中文字体支持
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "WenQuanYi Zen Hei",
        "Arial Unicode MS",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    # 多系列配色
    PALETTE = [
        "#2E74B5", "#ED7D31", "#A5A5A5", "#FFC000", "#5B9BD5",
        "#70AD47", "#264478", "#9B1C1C", "#6366F1", "#EC4899",
    ]

    result = {}
    for d in chart_defs:
        ctype = d.get("type", "line")
        chart_records = _records_for_chart(d, records, data_registry, period)

        # ---- 散点图 ----
        if ctype == "scatter":
            x_col = d.get("x_column", d.get("column", ""))
            y_col = d.get("y_column", "")
            xs = [float(r[x_col]) for r in chart_records if r.get(x_col) is not None and r.get(y_col) is not None]
            ys = [float(r[y_col]) for r in chart_records if r.get(x_col) is not None and r.get(y_col) is not None]
            fig, ax = plt.subplots(figsize=(8.5, 5.2), dpi=150)
            ax.scatter(xs, ys, c=PALETTE[0], alpha=0.6, s=30, edgecolors="white", linewidths=0.5)
            ax.set_xlabel(d.get("x_label", ""))
            ax.set_ylabel(d.get("y_label", ""))
            ax.set_title(d.get("title", ""), fontsize=13)
            ax.grid(True, linestyle="--", alpha=0.35)
            fig.tight_layout()
            png = os.path.join(out_dir, f"{d['id']}.png")
            fig.savefig(png, facecolor="white")
            plt.close(fig)
            result[d["id"]] = png
            continue

        # ---- 饼图 ----
        if ctype == "pie":
            col = d.get("column", "")
            if not col and d.get("metric") and data_registry is not None:
                src = data_registry.get(d["metric"])
                if src is not None and src.value_columns:
                    col = src.value_columns[0]
            values = [float(r[col]) for r in chart_records if r.get(col) is not None]
            bins = d.get("bins", 5)
            sizes, labels = _pie_aggregate(values, bins, d.get("labels"))
            fig, ax = plt.subplots(figsize=(7.5, 5.5), dpi=150)
            colors = PALETTE[:len(sizes)]
            wedges, texts, autotexts = ax.pie(
                sizes, labels=labels, colors=colors,
                autopct="%1.1f%%", startangle=90,
                wedgeprops=dict(edgecolor="white", linewidth=1.5),
            )
            for t in autotexts:
                t.set_fontsize(9)
            ax.set_title(d.get("title", ""), fontsize=13)
            ax.axis("equal")
            fig.tight_layout()
            png = os.path.join(out_dir, f"{d['id']}.png")
            fig.savefig(png, facecolor="white")
            plt.close(fig)
            result[d["id"]] = png
            continue

        # ---- 多系列折线图 ----
        if ctype == "multi_line":
            columns = d.get("columns", [])
            series_labels = d.get("series_labels", columns)
            if not columns and d.get("metric") and data_registry is not None:
                src = data_registry.get(d["metric"])
                if src is not None:
                    columns = list(src.value_columns)
                    series_labels = d.get("series_labels", columns)
            dates = [r["date"] for r in chart_records]
            fig, ax = plt.subplots(figsize=(9.5, 5.2), dpi=150)
            for si, col in enumerate(columns):
                vals = [float(r[col]) for r in chart_records if r.get(col) is not None]
                color = PALETTE[si % len(PALETTE)]
                ax.plot(dates[:len(vals)], vals, "-o", color=color,
                        linewidth=1.5, markersize=3, label=series_labels[si])
            ax.set_xlabel(d.get("x_label", "日期"))
            ax.set_ylabel(d.get("y_label", ""))
            ax.set_title(d.get("title", ""), fontsize=13)
            ax.legend(loc="best", fontsize=9)
            ax.grid(True, linestyle="--", alpha=0.35)
            fig.autofmt_xdate(rotation=30)
            fig.tight_layout()
            png = os.path.join(out_dir, f"{d['id']}.png")
            fig.savefig(png, facecolor="white")
            plt.close(fig)
            result[d["id"]] = png
            continue

        # ---- 单系列图表（line / histogram / bar / boxplot / area） ----
        col = d.get("column")
        if not col and d.get("metric") and data_registry is not None:
            src = data_registry.get(d["metric"])
            if src is not None and src.value_columns:
                col = src.value_columns[0]
        values = [float(r[col]) for r in chart_records if r.get(col) is not None] if col else []
        dates = [r["date"] for r in chart_records if r.get(col) is not None] if col else []

        fig, ax = plt.subplots(figsize=(9.5, 5.2), dpi=150)
        blue = "#2E74B5"

        if ctype == "line":
            ax.plot(dates, values, "-o", color=blue, linewidth=1.5, markersize=4)
            ax.set_xlabel(d.get("x_label", "日期"))
            ax.set_ylabel(d.get("y_label", ""))
            fig.autofmt_xdate(rotation=30)
        elif ctype == "area":
            ax.fill_between(dates, values, alpha=0.25, color=blue)
            ax.plot(dates, values, "-o", color=blue, linewidth=1.5, markersize=3)
            ax.set_xlabel(d.get("x_label", "日期"))
            ax.set_ylabel(d.get("y_label", ""))
            fig.autofmt_xdate(rotation=30)
        elif ctype == "histogram":
            bins = d.get("bins") or 10
            ax.hist(values, bins=bins, color=blue, edgecolor="white")
            ax.set_xlabel(d.get("x_label", ""))
            ax.set_ylabel(d.get("y_label", "天数"))
        elif ctype == "bar":
            ax.bar(dates, values, color=blue, width=0.6)
            ax.set_xlabel(d.get("x_label", "日期"))
            ax.set_ylabel(d.get("y_label", ""))
            fig.autofmt_xdate(rotation=30)
        elif ctype == "boxplot":
            ax.boxplot(values, vert=True, patch_artist=True,
                       boxprops=dict(facecolor="#DCE6F1", color=blue),
                       whiskerprops=dict(color=blue), capprops=dict(color=blue),
                       medianprops=dict(color="#9B1C1C"))
            ax.set_ylabel(d.get("y_label", ""))
            ax.set_xticks([])
        else:
            raise ValueError(f"未知图表类型: {ctype}")

        ax.set_title(d.get("title", ""), fontsize=13)
        ax.grid(True, linestyle="--", alpha=0.35)
        fig.tight_layout()
        png = os.path.join(out_dir, f"{d['id']}.png")
        fig.savefig(png, facecolor="white")
        plt.close(fig)
        result[d["id"]] = png

    return result


# ---------------------------------------------------------------------------
# 自动从 chart_texts 列表生成图表定义
# ---------------------------------------------------------------------------

def auto_chart_defs_from_texts(chart_texts: List[Dict]) -> List[Dict]:
    """从识别出的 chart_texts 自动构建 chart_defs。

    输入 chart_texts 项结构：
      {paragraph, kind, chart_id, metric, text, source, ...}

    输出 chart_defs 项结构：
      {id, type, title, metric, x_label, y_label}

    chart_id 命名规则必须与 annotate_docx 中的占位符命名完全一致：
      {{chart.<metric>_<chart_id>_<per_metric_counter>}}
    """
    # 按出现顺序遍历，维护 per-metric 计数器（与 annotate_docx 一致）
    chart_text_counter: Dict[str, int] = {}
    out: List[Dict] = []

    # kind → chart type 映射
    KIND_TO_TYPE = {
        "time_series": "line",
        "curve": "line",
        "trend": "line",
        "histogram": "histogram",
        "bar": "bar",
        "scatter": "scatter",
        "boxplot": "boxplot",
        "area": "area",
    }

    for ct in chart_texts:
        if ct.get("source") == "cell_ref":
            continue  # 单元格引用不算图
        if ct.get("source") == "bare_caption":
            continue  # 图题段不是图位，原 caption 已在模板中清空

        metric = ct.get("metric", "chart")
        cid = ct.get("chart_id", "trend")
        kind = ct.get("kind", "trend")

        # 与 annotate_docx 完全一致的命名逻辑
        chart_text_counter[metric] = chart_text_counter.get(metric, 0) + 1
        chart_id = f"{metric}_{cid}_{chart_text_counter[metric]}"

        title = ct.get("text", "").strip()
        chart_type = KIND_TO_TYPE.get(kind, "line")

        if chart_type == "bar":
            out.append({
                "id": chart_id,
                "type": "bar",
                "title": title,
                "metric": metric,
                "x_label": "类别",
                "y_label": "数值",
            })
        elif chart_type == "scatter":
            out.append({
                "id": chart_id,
                "type": "scatter",
                "title": title,
                "metric": metric,
                "x_column": None,
                "y_column": None,
            })
        else:
            out.append({
                "id": chart_id,
                "type": chart_type,
                "title": title,
                "metric": metric,
                "x_label": "日期",
                "y_label": "数值",
            })
    return out
