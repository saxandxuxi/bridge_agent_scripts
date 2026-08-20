# -*- coding: utf-8 -*-
"""数据分析报告智能体：数据 -> 统计 -> 出图 -> 填充模板 -> 输出 Word 报告。"""

import calendar
import datetime as dt
import json
import logging
import os
import re
from typing import Dict, List, Optional

from . import chart_generator, data_loader, report_builder, stats
from .config import load_config
from .bridge_source import _norm

log = logging.getLogger("report-agent.agent")

from .period_utils import last_completed_quarter, quarter_range  # noqa: E402


def resolve_period(
    mode: str,
    report_date: Optional[dt.date] = None,
    period_cfg: Optional[Dict] = None,
) -> Dict:
    """根据模式确定报告数据区间。

    weekly    : 最近 7 天（含报告日）
    monthly   : 最近 30 天（含报告日）
    quarterly : 报告日所在自然季度（如 2026-01-01 ~ 2026-03-31）
    yearly    : 报告日所在自然年（如 2026-01-01 ~ 2026-12-31）
    manual    : 最近 7 天，或由 --date 指定结束日

    返回的 period 包含 label / label_cn 两个展示字段：
      label    — 用于文件名/标题，如 "2026.1~3"、"2026.03"、"2026.08.05"
      label_cn — 用于正文表述，如 "2026年第一季度"、"2026年3月"、"2026年8月"
    """
    period_cfg = period_cfg or {}
    end = report_date or dt.date.today()
    mode = mode or "weekly"

    if mode == "yearly":
        start = dt.date(end.year, 1, 1)
        end = dt.date(end.year, 12, 31)
        label = f"{end.year}年"
        label_cn = f"{end.year}年度"
    elif mode == "quarterly":
        # 自然季度：1-3 / 4-6 / 7-9 / 10-12 月
        q = (end.month - 1) // 3 + 1
        q_start_month = (q - 1) * 3 + 1
        q_end_month = q * 3
        start = dt.date(end.year, q_start_month, 1)
        end = dt.date(end.year, q_end_month, calendar.monthrange(end.year, q_end_month)[1])
        label = f"{end.year}.{q_start_month}~{q_end_month}"
        label_cn = f"{end.year}年第{q}季度"
    elif mode == "monthly":
        days = int(period_cfg.get("monthly_days", 30))
        start = end - dt.timedelta(days=days - 1)
        label = f"{end.year}.{end.month:02d}"
        label_cn = f"{end.year}年{end.month}月"
    else:
        days = int(period_cfg.get("weekly_days", 7))
        start = end - dt.timedelta(days=days - 1)
        label = f"{end.year}.{end.month:02d}.{end.day:02d}"
        week_of_month = (end.day - 1) // 7 + 1
        label_cn = f"{end.year}年{end.month}月第{week_of_month}周"

    return {
        "mode": mode,
        "start": start,
        "end": end,
        "generated_at": dt.datetime.now(),
        "label": label,
        "label_cn": label_cn,
    }


def build_daily_records(records: List[Dict], value_column: str) -> List[Dict]:
    """构造逐日明细行：date / 数值列 / 较均值偏差。"""
    values = [float(r[value_column]) for r in records if r.get(value_column) is not None]
    avg = sum(values) / len(values) if values else 0.0
    rows = []
    for r in records:
        if r.get(value_column) is None:
            continue
        rows.append(
            {
                "date": r["date"],
                value_column: r[value_column],
                "deviation": round(float(r[value_column]) - avg, 1),
            }
        )
    return rows


def _bridge_key(s) -> str:
    """桥名归一化：去空白、去“大桥/特大桥”后缀、转小写（与 bridges 一致）。"""
    s = str(s or "").strip().lower().replace(" ", "")
    for suffix in ("特大桥", "大桥"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _is_other_registered_bridge_name(name: str, current: str) -> bool:
    """name_prefix 是否属于“其他已注册桥”的桥名（如配置残留 赤石大桥，
    但当前桥是 洣水河特大桥）。是则报告名必须跟随当前桥。"""
    if not name:
        return False
    try:
        from .bridges import list_bridges
        bridges = list_bridges()
    except Exception:  # noqa: BLE001
        return False
    nk = _bridge_key(name)
    ck = _bridge_key(current)
    if not nk or nk == ck:
        return False
    return any(_bridge_key(b.get("name")) == nk for b in bridges)


class ReportAgent:
    def __init__(self, config: Dict):
        self.cfg = config

    def run(
        self,
        mode: Optional[str] = None,
        report_date: Optional[dt.date] = None,
        engine: Optional[str] = None,
        inspect_only: bool = False,
    ) -> Dict:
        """执行一次完整报告生成，返回结果摘要。"""
        mode = mode or self.cfg.get("schedule", {}).get("mode", "weekly")
        if mode not in ("weekly", "monthly", "quarterly", "yearly", "manual"):
            raise ValueError(f"未知模式: {mode}（支持 weekly / monthly / quarterly / yearly / manual）")

        period = resolve_period(mode, report_date, self.cfg.get("period"))
        data_cfg = self.cfg.get("data", {})

        # 0. 真实监测数据适配器（桥数据预处理产物）
        bridge = None
        bridge_status = {}
        pending_charts: List[Dict] = []
        missing_sinks: List[str] = []
        bridge_cfg = self.cfg.get("bridge_data", {}) or {}
        if bridge_cfg.get("enabled", False):
            from .bridge_source import BridgeData
            base_dir = os.path.dirname(os.path.abspath(self.cfg.get("_config_path", "config.json")))
            bridge = BridgeData(bridge_cfg, base_dir=base_dir)
            bridge_status = bridge.load()
            if not bridge_status.get("loaded"):
                log.error("桥数据加载失败: %s", bridge_status.get("error"))

        # 1. 读取并过滤数据（兼容单数据源；桥模式下 CSV 可缺失）
        records: List[Dict] = []
        load_stats: Dict = {"total_rows": 0, "loaded_rows": 0, "skipped_rows": 0, "none_value_counts": {}}
        csv_available = False
        if data_cfg.get("file"):
            try:
                data_strict = data_cfg.get("strict_mode", False)
                all_records, load_stats = data_loader.load_csv(
                    data_cfg.get("file", ""),
                    date_column=data_cfg.get("date_column", "date"),
                    value_columns=data_cfg.get("value_columns"),
                    strict=data_strict,
                    return_stats=True,
                )
                records = data_loader.filter_period(all_records, period["start"], period["end"])
                csv_available = len(records) > 0
            except Exception as exc:  # noqa: BLE001
                if bridge is None:
                    raise
                log.warning("CSV 数据不可用，桥模式下继续（%s）: %s",
                            data_cfg.get("file"), exc)
        if not records and bridge is None:
            raise ValueError(
                f"数据区间 {period['start']} 至 {period['end']} 内没有数据，"
                f"请检查数据文件 {data_cfg.get('file')}"
            )

        # 1b. 加载多数据源注册表（按指标路由）
        base_dir = os.path.dirname(os.path.abspath(self.cfg.get("_config_path", "config.json")))
        data_registry = data_loader.DataSourceRegistry(
            self.cfg.get("data_sources", {}),
            base_dir=base_dir,
        )
        available = data_registry.available_metrics()
        if available:
            log.info("已加载多数据源: %s", ", ".join(available))
        else:
            log.info("未配置 data_sources，使用单数据源模式（data.file）")

        # 2. 统计
        if records:
            computed = stats.compute_stats(
                records,
                value_columns=data_cfg.get("value_columns"),
                thresholds=data_cfg.get("thresholds", []),
            )
        else:
            computed = {"days": bridge.estimate_days(period) if bridge is not None else 0}
        log.info(
            "数据区间: %s ~ %s  (%s 天)",
            period["start"], period["end"], computed.get("days", 0),
        )
        for col in data_cfg.get("value_columns", []):
            if col in computed:
                s = computed[col]
                log.info(
                    f"  {col}: 最大 {s['max']:.1f}（{s.get('max_date')}）  "
                    f"最小 {s['min']:.1f}（{s.get('min_date')}）  "
                    f"平均 {s['avg']:.1f}  标准差 {s['std']:.2f}"
                )

        # 3. 图表（MATLAB 优先，Python 兜底）
        # 3a. 用户在 config.charts.definitions 里写死的图表
        # 3b. 从 chart_texts 自动生成的图表（如有）
        charts_cfg = self.cfg.get("charts", {})
        user_chart_defs = list(charts_cfg.get("definitions", []) or [])

        chart_texts_for_runtime = self.cfg.get("_chart_texts", [])
        chart_images: Dict[str, str] = {}
        chart_captions: Dict[str, str] = {}
        chart_sensors: Dict[str, str] = {}
        chart_kinds: Dict[str, str] = {}
        chart_para: Dict[str, int] = {}
        extra_charts: Dict[str, List[Dict]] = {}
        chart_gaps: List[Dict] = []
        if bridge is not None:
            # 桥模式：图表优先从图库解析；解析不到生成占位图并记入待补清单
            out_dir = charts_cfg.get("output_dir", "outputs/charts")
            os.makedirs(out_dir, exist_ok=True)
            resolved_bridge = 0
            # 只保留真正的图表（跳过表格单元格引用 + bare_caption 图题段）
            chart_items = [
                ct for ct in chart_texts_for_runtime
                if ct.get("source") != "bare_caption"
                and not (str(ct.get("_unique_chart_id") or ct.get("chart_id") or "").startswith("cell_")
                        or str(ct.get("kind", "")).startswith("cell"))
            ]
            for i, ct in enumerate(chart_items):
                cid = ct.get("_unique_chart_id") or ct.get("chart_id")
                if not cid:
                    continue
                # 上下文 = 前 3 个正文段落（如“第6、7跨…如下图所示：”这句）
                #          + 按距离排序的邻近图注（用于“倾角1_time_series”裸图注继承位置）
                n = len(chart_items)
                texts = self.cfg.get("_texts", []) or []
                ctx_texts = []
                para = ct.get("paragraph")
                if isinstance(para, int):
                    # 前 4 段正文，从最近往前（“…如下图所示：”这句优先）
                    for pno in range(para - 1, max(para - 5, -1), -1):
                        if 0 <= pno < len(texts) and texts[pno]:
                            t = str(texts[pno]).strip()
                            if t and t not in ctx_texts:
                                ctx_texts.append(t)
                order = [0, 1, -1, 2, -2]
                for off in order:
                    j = i + off
                    if 0 <= j < n and j != i:
                        t = chart_items[j].get("text", "")
                        if t and t not in ctx_texts:
                            ctx_texts.append(t)
                # 指标提示：只用图注 + 节前正文判断（避免相邻节图注串扰）
                metric_hint = ""
                if isinstance(para, int):
                    body_texts = []
                    for pno in range(para - 1, max(para - 5, -1), -1):
                        if 0 <= pno < len(texts) and texts[pno]:
                            body_texts.append(str(texts[pno]))
                    # 指标判断：节上下文(正文)优先于图注原文（图注原文可能有笔误，如结构温度节写成“环境温度”）
                    metric_hint = (
                        bridge._metric_alias_hit(" ".join(body_texts))
                        or bridge._metric_alias_hit(ct.get("text", ""))
                        or ""
                    )
                info = bridge.resolve_chart_info(cid, ct.get("text", ""), context=ctx_texts,
                                                 metric_hint=metric_hint,
                                                 sensor_hint=str(ct.get("sensor_id") or ""),
                                                 feature_hint=str(ct.get("feature") or ""))
                if info:
                    chart_images[cid] = info["path"]
                    chart_captions[cid] = info["display"]
                    chart_sensors[cid] = info["sensor_id"]
                    chart_kinds[cid] = info["kind"]
                    chart_para[cid] = ct.get("paragraph") or 0
                    resolved_bridge += 1
                else:
                    reason = f"未匹配到图库图片（图注: {ct.get('text', '') or '无'}）"
                    placeholder = bridge.make_placeholder_chart(cid, reason, out_dir)
                    if placeholder:
                        chart_images[cid] = placeholder
                    chart_captions[cid] = ct.get("text", "") or cid
                    pending_charts.append({"chart_id": cid, "caption": ct.get("text", ""), "reason": reason})
            for d in user_chart_defs:
                cid = d.get("id")
                if cid and cid not in chart_images:
                    info = bridge.resolve_chart_info(cid, d.get("title", ""))
                    if info:
                        chart_images[cid] = info["path"]
                        chart_captions[cid] = info["display"]
                        resolved_bridge += 1
            log.info("桥模式图表解析完成：命中图库 %d 张，待补 %d 张",
                     resolved_bridge, len(pending_charts))

            # ---- 缺图推断：按本节约应有的监测部位数 vs 实际图表数 ----
            chart_gaps = self._detect_chart_gaps(
                bridge, chart_items, chart_sensors, chart_kinds, chart_para
            )
            extra_charts: Dict[str, List[Dict]] = {}
            if chart_gaps and (bridge_cfg.get("auto_fill_missing_charts", True)):
                for gap in chart_gaps:
                    anchor = gap.get("anchor_cid")
                    if not anchor:
                        continue
                    for item in gap.get("missing", []):
                        sid = item["sensor_id"]
                        kind = item["kind"]
                        png = bridge.chart_png_for(sid, kind, metric=item.get("metric", ""))
                        if not png:
                            continue
                        metric = item.get("metric", "")
                        caption = bridge.display_name_for(
                            sid, f"{metric}_{kind}_1" if metric else f"{kind}_1",
                            kind, metric_for_label=metric or "")
                        extra_charts.setdefault(anchor, []).append({
                            "path": png,
                            "caption": caption,
                            "sensor_id": sid,
                            "kind": kind,
                        })
            log.info("缺图推断：%d 节存在缺图（自动补齐 %d 张）",
                     len(chart_gaps), sum(len(v) for v in extra_charts.values()))
            # 拆分的多面板合并图(时间序列图_2.png / _3.png ...)：
            # 在对应图表位置后一并插入，避免只有第一张进报告
            for cid, png in list(chart_images.items()):
                if not png:
                    continue
                for _p in bridge.chart_siblings(png):
                    extra_charts.setdefault(cid, []).append({
                        "path": _p,
                        "caption": "",
                        "sensor_id": chart_sensors.get(cid, ""),
                        "kind": chart_kinds.get(cid, ""),
                    })
            # 模板中额外的位置化图表占位符（如特殊应变 4#/5#墩底部），
            # 未出现在 analysis chart_texts 时按位置直接解析，避免“有占位无图”
            try:
                from docx import Document as _Doc
                from .report_builder import _paragraph_text, _walk_paragraphs
                tpl_doc = _Doc(self.cfg.get("template", ""))
                extra_ids = []
                recent = []  # 图表占位符前面的最近正文（用于左右幅等方位定向）
                for para in _walk_paragraphs(tpl_doc):
                    t = _paragraph_text(para).strip()
                    m = re.fullmatch(r"\{\{chart\.([^}]+)\}\}", t)
                    if not m:
                        if t:
                            recent.append(t)
                            if len(recent) > 8:
                                recent = recent[-8:]
                        continue
                    cid = m.group(1)
                    if cid not in chart_images:
                        extra_ids.append((cid, list(recent)))
                for cid, ctx in extra_ids:
                    # 传最近正文作上下文，让“右幅”节能定向到右幅传感器
                    info = bridge.resolve_chart_info(cid, cid, context=ctx)
                    if not info:
                        # 匹配不到也生成占位图并记入待补清单，避免整个报告
                        # 因个别图表占位符匹配失败而中断（报告仍可正常生成）
                        reason = f"未匹配到图库图片（模板占位符: {cid}）"
                        placeholder = bridge.make_placeholder_chart(
                            cid, reason, out_dir)
                        if placeholder:
                            chart_images[cid] = placeholder
                        chart_captions[cid] = cid
                        pending_charts.append({
                            "chart_id": cid, "caption": cid, "reason": reason,
                        })
                        continue
                    chart_images[cid] = info["path"]
                    chart_captions[cid] = info["display"]
                    chart_sensors[cid] = info["sensor_id"]
                    chart_kinds[cid] = info["kind"]
                    log.info("模板位置化图表 %s -> %s", cid, info["path"])
            except Exception as exc:  # noqa: BLE001
                log.warning("扫描模板额外图表占位符失败: %s", exc)
            # CSV 可用时，剩余的 user_chart_defs 仍可走 matplotlib 兜底
            leftover_defs = [d for d in user_chart_defs if d.get("id") not in chart_images]
            if records and leftover_defs:
                chart_images.update(chart_generator.generate_charts(
                    leftover_defs, records,
                    charts_cfg.get("output_dir", "outputs/charts"),
                    engine=engine or charts_cfg.get("engine", "auto"),
                    matlab_cfg=charts_cfg.get("matlab", {}),
                    data_registry=data_registry,
                    period=period,
                ))
        else:
            auto_defs = []
            if chart_texts_for_runtime and data_registry is not None:
                from .chart_generator import auto_chart_defs_from_texts
                auto_defs = auto_chart_defs_from_texts(chart_texts_for_runtime)
                log.info("从 %d 个 chart_text 自动生成 %d 个图表定义",
                         len(chart_texts_for_runtime), len(auto_defs))
            all_chart_defs = user_chart_defs + auto_defs
            chart_images = chart_generator.generate_charts(
                all_chart_defs,
                records,
                charts_cfg.get("output_dir", "outputs/charts"),
                engine=engine or charts_cfg.get("engine", "auto"),
                matlab_cfg=charts_cfg.get("matlab", {}),
                data_registry=data_registry,
                period=period,
            )
        for cid, png in chart_images.items():
            log.info("  图表 %s: %s", cid, png)

        # 4. 逐日明细
        row_datasets: Dict[str, List[Dict]] = {}
        if records:
            value_col = (data_cfg.get("value_columns") or ["temperature"])[0]
            daily_rows = build_daily_records(records, value_col)
            row_datasets = {"daily_records": daily_rows}

        # 5. 填充模板
        lineage: List[Dict] = []
        resolver = report_builder.build_value_resolver(
            computed, period,
            data_registry=data_registry,
            data_values=self.cfg.get("_data_values", {}),
            bridge=bridge,
            missing_sink=missing_sinks,
            lineage=lineage,
            data_meta=self.cfg.get("_data_number_meta", {}),
            llm_cfg=self.cfg.get("llm"),
        )

        # 输出文件名：优先使用 config.report_name_prefix；
        # 若未配置则尝试从 source_report（成品报告）文件名推导；
        # 最终兜底用模板文件名。始终附加日期+时间后缀。
        name_cfg = self.cfg.get("report", {})
        name_prefix = name_cfg.get("name_prefix", "")
        # 桥模式下报告名必须跟随当前桥：若配置里的 name_prefix 是其他已注册
        # 桥的桥名（历史模板/源报告上传残留，如 赤石大桥），自动纠正为当前桥，
        # 避免“生成洣水河却输出 赤石大桥.docx”。
        if bridge is not None and bridge.bridge_name:
            bn = bridge.bridge_name
            if (not name_prefix
                    or _is_other_registered_bridge_name(name_prefix, bn)):
                name_prefix = bn
        if not name_prefix:
            source_report = self.cfg.get("source_report", "")
            if source_report:
                name_prefix = os.path.splitext(os.path.basename(source_report))[0]
            else:
                name_prefix = os.path.splitext(os.path.basename(self.cfg.get("template", "")))[0]
            # 去掉 _template / _模板 等后缀
            name_prefix = re.sub(r"[_-]template$|[_-]模板$", "", name_prefix, flags=re.IGNORECASE)

        # 报告名带上模板版本（如 _template_v17），便于区分不同模板生成的报告
        if "_template_v" not in name_prefix:
            m = re.search(r"_v(\d+)(?:\.docx)?$",
                          os.path.basename(self.cfg.get("template", "")))
            if m:
                name_prefix = f"{name_prefix}_template_v{m.group(1)}"

        with_ts = name_cfg.get("with_timestamp", True)
        if mode == "quarterly":
            # 季度命名：洞庭湖大桥2026.1~3.docx（用户指定格式）
            out_name = f"{name_prefix}{period['label']}.docx"
        elif mode == "yearly":
            out_name = f"{name_prefix}{period['label']}.docx"
        elif with_ts:
            out_name = (
                f"{name_prefix}_{period['start'].strftime('%Y%m%d')}_"
                f"{period['end'].strftime('%Y%m%d')}_"
                f"{dt.datetime.now().strftime('%H%M%S')}.docx"
            )
        else:
            out_name = (
                f"{name_prefix}_{period['start'].strftime('%Y%m%d')}_"
                f"{period['end'].strftime('%Y%m%d')}.docx"
            )
        out_path = os.path.join(self.cfg.get("output_dir", "outputs"), out_name)
        repair_stats = {}
        unfilled = report_builder.build_report(
            template_path=self.cfg.get("template", ""),
            output_path=out_path,
            resolver=resolver,
            chart_images=chart_images,
            row_datasets=row_datasets,
            chart_width_inches=float(charts_cfg.get("width_inches", 5.8)),
            strict=True,
            period=period,
            chart_captions=chart_captions,
            extra_charts=extra_charts,
            text_replace=bridge_cfg.get("text_replace") or None,
            repair_stats=repair_stats,
        )

        # 数据链路日志：每个填入数值的来源与计算链（找不到的会标“未找到”）
        verify_warns = []
        if lineage:
            out_dir_abs = os.path.abspath(self.cfg.get("output_dir", "outputs"))
            logs_dir = os.path.join(
                os.path.dirname(out_dir_abs),
                "logs",
            )
            lineage_path = report_builder._write_data_lineage(
                lineage, logs_dir, period)
            log.info("数据链路日志已写出: %s（%d 条，未找到 %d 条）",
                     lineage_path, len(lineage),
                     sum(1 for e in lineage if e.get("结果") == "未找到"))
            # 填表校验：整列未解析 / 整列同值 告警
            try:
                verify_warns = report_builder.verify_table_columns(
                    out_path, lineage=lineage, logs_dir=logs_dir,
                    label=period.get("label") or "report")
                if verify_warns:
                    log.warning("填表校验发现问题 %d 处（详见 verify_tables_%s.log）",
                                len(verify_warns), period.get("label"))
            except Exception as exc:  # noqa: BLE001
                log.warning("填表校验失败: %s", exc)
        else:
            lineage_path = ""

        # LLM 报告审查：对照成品原文，检查表格/索引/图片/统计逻辑/总结段落
        report_review = None
        try:
            from .reviewer import ReportReviewer, _read_docx_text
            reviewer = ReportReviewer(self.cfg.get("llm"))
            if reviewer.available():
                # 数据链路摘要：只给“未找到/回退”项，避免把整份链路塞给 LLM
                missed = [e for e in lineage if e.get("结果") in ("未找到", "回退")]
                lineage_digest = json.dumps(
                    missed[:200], ensure_ascii=False, default=str)
                table_warnings = json.dumps(
                    verify_warns[:100], ensure_ascii=False, default=str)
                report_review = reviewer.review_report(
                    _read_docx_text(self.cfg.get("source_report", "")),
                    _read_docx_text(out_path),
                    lineage_digest=lineage_digest,
                    table_warnings=table_warnings,
                )
                n_issues = len(report_review.get("issues", []))
                if n_issues:
                    log.warning("报告审查发现 %d 处问题", n_issues)
                    for iss in report_review.get("issues", []):
                        log.warning("  - [%s] %s",
                                    iss.get("type", "other"),
                                    iss.get("detail", ""))
                else:
                    log.info("报告审查完成：未发现问题")
                out_dir_abs = os.path.abspath(self.cfg.get("output_dir", "outputs"))
                logs_dir = os.path.join(os.path.dirname(out_dir_abs), "logs")
                os.makedirs(logs_dir, exist_ok=True)
                review_path = os.path.join(
                    logs_dir, f"review_report_{period.get('label') or 'report'}.json")
                with open(review_path, "w", encoding="utf-8") as f:
                    json.dump(report_review, f, ensure_ascii=False, indent=2)
                log.info("报告审查结果已保存: %s", review_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("报告审查异常（不影响报告生成）: %s", exc)

        # 确定性体检（不依赖 LLM，兜住数值逻辑/重复行/单位空格等硬错误）
        self_check = []
        try:
            from .reviewer import self_check_report
            self_check = self_check_report(out_path)
            if self_check:
                log.warning("确定性体检发现 %d 处问题", len(self_check))
                for iss in self_check:
                    log.warning("  - [%s] %s",
                                iss.get("type", "other"), iss.get("detail", ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("确定性体检失败: %s", exc)

        repair = {
            "auto_fixed": repair_stats.get("spaces_collapsed", 0),
            "manual_needed": ((len(report_review.get("issues", []))
                               if report_review else 0) + len(self_check)),
            "llm_called": report_review is not None,
            "llm_ok": bool(report_review and report_review.get("raw")),
        }

        summary = {
            "output": out_path,
            "period": {
                "mode": mode,
                "start": period["start"].isoformat(),
                "end": period["end"].isoformat(),
                "label": period.get("label", ""),
                "label_cn": period.get("label_cn", ""),
            },
            "days": computed.get("days", 0),
            "charts": {cid: png for cid, png in chart_images.items()},
            "unfilled": unfilled,
            "data_load_stats": load_stats,
            "csv_available": csv_available,
            "bridge": bridge_status,
            "pending_charts": pending_charts,
            "missing_cells": missing_sinks[:500],
            "chart_gaps": chart_gaps if bridge is not None else [],
            "review": report_review,
            "self_check": self_check,
            "repair": repair,
        }
        # 生成结束后刷新桥数据状态，让 match_stats 反映本次运行的实际命中情况
        if bridge is not None:
            summary["bridge"] = bridge.status()
        if inspect_only:
            summary["stats"] = {k: v for k, v in computed.items() if k != "days"}
        log.info("报告已生成: %s", out_path)
        return summary

    # ------------------------------------------------------------------
    # 缺图推断：按本节约应有的监测部位数 vs 实际图表数
    # ------------------------------------------------------------------

    @staticmethod
    def _metric_for_chart(cid: str, bridge) -> str:
        """从图表 ID 推导指标名（temperature_trend_1 -> temperature）。"""
        parsed = bridge._parse_chart_id(cid)
        return parsed[0] if parsed and parsed[0] else ""

    def _section_plan(self, bridge, cluster, texts) -> Optional[Dict]:
        """推断一个图表集群（节）应出的（指标, 监测部位列表, 图型集合）。

        返回 {"metric", "positions": [(位置, [传感器编号])], "kinds": [...]} 或 None。
        位置来源：
          - 温湿度/结构温度/应变/振动等有映射表的，用表格映射/测点映射；
          - 其余（风荷载/挠度/位移/倾角/索力/裂缝等）用该节表格 cell_ref 的行标签。
        """
        paras = [c.get("paragraph") for c in cluster if isinstance(c.get("paragraph"), int)]
        if not paras:
            return None
        p0 = min(paras)
        section_title = ""
        heading_pno = None
        for pno in range(p0 - 1, max(p0 - 120, -1), -1):
            if 0 <= pno < len(texts):
                t = str(texts[pno]).strip()
                if not t:
                    continue
                if re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                    section_title = t
                    heading_pno = pno
                    break
        if not section_title or heading_pno is None:
            return None
        metric, mkey = None, ""
        if "结构温度" in section_title:
            metric, mkey = "structure_temperature", "结构温度表"
        elif "环境温度" in section_title:
            metric, mkey = "temperature", "温湿度表"
        elif "环境湿度" in section_title:
            metric, mkey = "humidity", "温湿度表"
        elif "风速" in section_title or "风向" in section_title or "风荷载" in section_title:
            metric = "wind_speed"
        elif "挠度" in section_title:
            metric = "deflection"
        elif "应变" in section_title:
            metric, mkey = "strain", "结构应变监测表"
        elif "位移" in section_title:
            metric = "displacement"
        elif "倾角" in section_title or "转角" in section_title:
            metric = "rotation"
        elif "索力" in section_title:
            metric = "cable_force"
        elif "裂缝" in section_title:
            metric, mkey = "crack", "裂缝监测表"
        elif "振动" in section_title:
            metric, mkey = "vibration", "结构振动监测表"
        if not metric:
            return None
        # 图型集合：节标题 + 该节说明句（“……时程曲线图、频率分布直方图如下图所示”）
        kinds = set()
        _win = [section_title]
        for _pno in range(p0 - 1, max(p0 - 6, -1), -1):
            if 0 <= _pno < len(texts) and str(texts[_pno]).strip():
                _win.append(str(texts[_pno]))
        _joined = "".join(_win)
        if "直方图" in _joined or "频率分布" in _joined:
            kinds.add("histogram")
        if "时程" in _joined or "时间序列" in _joined:
            kinds.add("trend")
        if not kinds:
            kinds = {"trend"}
        # 位置集合
        positions = []
        if mkey and (bridge.table_map or {}).get(mkey):
            for pos, sids in (bridge.table_map[mkey] or {}).items():
                positions.append((pos, [str(x) for x in sids]))
        elif mkey and mkey in (bridge.point_map or {}):
            for pl in bridge.point_map[mkey]:
                sids = [str(x) for x in ((pl.get("测点") or {}).values())]
                positions.append((pl.get("断面位置", ""), sids))
        else:
            positions = self._cell_ref_positions(bridge, heading_pno)
        if not positions:
            return None
        positions = self._filter_positions(positions, section_title)
        if not positions:
            return None
        return {"metric": metric, "positions": positions, "kinds": sorted(kinds)}

    @staticmethod
    def _filter_positions(positions, section_title) -> List[tuple]:
        """按节标题里的位置词过滤监测部位，避免补图时把同指标其它节的位置也补进来。"""
        body = re.sub(r"^\d+(\.\d+){1,3}\s*", "", section_title)
        for w in ("环境温度", "环境湿度", "结构温度", "风速", "风向", "风荷载", "挠度", "应变",
                  "位移", "倾角", "转角", "索力", "裂缝", "振动", "监测", "统计",
                  "数据分析", "结构", "主梁"):
            body = body.replace(w, "")
        body = body.strip()
        if not body:
            return positions  # 节标题没有位置词（如“风荷载监测数据分析”）-> 全量
        norm_body = _norm(body)
        positions_norm = {_norm(p): p for p, _ in positions}
        out = []
        for np, pos in positions_norm.items():
            # 位置词必须是完整片段（前后是顿号/逗号/开头/结尾），
            # 避免 “4#、5#墩底部” 里只把 “5#墩底部” 当整段匹配
            if norm_body == np or norm_body in np or re.search(
                    rf"(^|[、，,和及]){re.escape(np)}([、，,和及]|$)", norm_body):
                out.append(pos)
        # 列表式墩号位置（如 “4#、5#墩底部”）-> 展开为 4#墩底部、5#墩底部
        m_list = re.match(r"^(\d+#(?:[、，,和及]\d+#)+)(.+)$", norm_body)
        if m_list:
            nums = re.findall(r"(\d+)#", m_list.group(1))
            suffix = m_list.group(2)
            for n in nums:
                cand = f"{n}#{suffix}"
                for np, pos in positions_norm.items():
                    if cand == np or cand in np or np in cand:
                        out.append(pos)
        # 跨号展开（如 “第6、7跨跨中断面”），要求含“跨中”时位置也含“跨中”
        spans = re.findall(r"\d+", "".join(re.findall(r"第([\d、，,和及]+)跨", body)))
        if not out and spans:
            need_mid = "跨中" in body
            for pos, _ in positions:
                np = _norm(pos)
                ok = any(f"第{s}跨" in np for s in spans)
                if ok and need_mid and "跨中" not in np:
                    ok = False
                if ok:
                    out.append(pos)
        seen = set()
        dedup = []
        for p in out:
            if p not in seen:
                seen.add(p)
                dedup.append(p)
        return [(p, sids) for p, sids in positions if p in seen]

    def _cell_ref_positions(self, bridge, heading_pno: int) -> List[tuple]:
        """从 analysis cell_ref 收集该节表格的监测部位（位置 -> 该位置传感器）。

        范围取“小节标题之后、下一个小节标题之前”，避免表格行距图表占位符较远时漏采。
        """
        texts = self.cfg.get("_texts", []) or []
        if not (0 <= heading_pno < len(texts)):
            return []
        lo = heading_pno
        hi = min(heading_pno + 250, len(texts))
        for pno in range(heading_pno + 1, hi):
            t = str(texts[pno]).strip()
            if t and re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                hi = pno
                break
        out = {}
        for ct in (self.cfg.get("_chart_texts", []) or []):
            if ct.get("source") != "cell_ref":
                continue
            p = ct.get("paragraph")
            if not isinstance(p, int) or not (lo < p < hi):
                continue
            row = str(ct.get("row_label") or "").strip()
            if not row or row.startswith("测点"):
                continue
            metric = str(ct.get("metric") or "")
            sids = bridge._sensors_at_position(row, metric) if metric else []
            if sids:
                out.setdefault(row, sids)
        return [(pos, sids) for pos, sids in out.items()]

    def _metric_for_cluster(self, cl, texts) -> str:
        """按最近节标题推断指标。"""
        paras = [c.get("paragraph") for c in cl if isinstance(c.get("paragraph"), int)]
        if not paras:
            return ""
        p0 = min(paras)
        for pno in range(p0 - 1, max(p0 - 120, -1), -1):
            if 0 <= pno < len(texts):
                t = str(texts[pno]).strip()
                if re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                    for kw, m in (("结构温度", "structure_temperature"),
                                  ("环境温度", "temperature"),
                                  ("环境湿度", "humidity"),
                                  ("风速", "wind_speed"), ("风向", "wind_speed"),
                                  ("挠度", "deflection"), ("应变", "strain"),
                                  ("位移", "displacement"), ("倾角", "rotation"),
                                  ("转角", "rotation"), ("索力", "cable_force"),
                                  ("裂缝", "crack"), ("振动", "vibration")):
                        if kw in t:
                            return m
                    break
        return ""

    def _detect_chart_gaps(self, bridge, chart_items, chart_sensors,
                           chart_kinds, chart_para) -> List[Dict]:
        """按“该节表格的监测部位 × 图型”检测缺图，并生成补齐清单。"""
        texts = self.cfg.get("_texts", []) or []
        # 聚类：按节标题切分（每出现一个数字编号标题就开新簇），
        # 避免相邻节图表离得近时被并到同一簇、锚点选到下一节
        heading_idx = []
        for pno, t in enumerate(texts):
            ts = str(t).strip()
            if ts and len(ts) <= 60 and re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", ts):
                heading_idx.append(pno)

        def _section_of(p: int):
            h = None
            for hh in heading_idx:
                if hh < p:
                    h = hh
                else:
                    break
            return h

        clusters: Dict[int, List[Dict]] = {}
        for ct in chart_items:
            p = ct.get("paragraph") or 0
            clusters.setdefault(_section_of(p), []).append(ct)
        clusters = [v for k, v in sorted(clusters.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]

        gaps = []
        for cl in clusters:
            cids = [str(ct.get("_unique_chart_id") or ct.get("chart_id"))
                    for ct in cl if ct.get("_unique_chart_id") or ct.get("chart_id")]
            if not cids:
                continue
            used = sorted({s for c in cids if (s := chart_sensors.get(c))})
            if not used:
                continue
            plan = self._section_plan(bridge, cl, texts)
            if not plan:
                continue
            metric = plan["metric"]
            target_kinds = plan["kinds"]
            # 已插图组合 (监测部位, 图型)
            charted = set()
            for c in cids:
                s = chart_sensors.get(c)
                k = chart_kinds.get(c)
                if not s or not k:
                    continue
                p = bridge._position_for_sensor(s)
                if p:
                    charted.add((_norm(p), k))
            missing_items = []
            for pos, sids in plan["positions"]:
                for k in target_kinds:
                    if (_norm(pos), k) in charted:
                        continue
                    sid = str(sids[0]) if sids else ""
                    if not sid:
                        sids2 = bridge._sensors_at_position(pos, metric)
                        sid = str(sids2[0]) if sids2 else ""
                    if not sid:
                        continue
                    missing_items.append({
                        "sensor_id": sid, "position": pos,
                        "kind": k, "metric": metric,
                    })
            if not missing_items:
                continue
            section = ""
            if cl and isinstance(cl[0].get("paragraph"), int):
                p0 = cl[0]["paragraph"]
                section = str(texts[p0 - 2])[:40] if 0 <= p0 - 2 < len(texts) else ""
            gaps.append({
                "section": section,
                "metric": metric,
                "positions": [p for p, _ in plan["positions"]],
                "kinds": target_kinds,
                "charted_sensors": used,
                "anchor_cid": cids[-1],
                "missing": missing_items,
            })
        return gaps


def run_once(
    config_path: str = None,
    mode: str = None,
    report_date: str = None,
    engine: str = None,
    inspect_only: bool = False,
    template_override: str = None,
) -> Dict:
    cfg = load_config(config_path)
    if template_override:
        tpl = template_override
        if not os.path.isabs(tpl):
            # 相对路径统一相对项目根目录解析（config 文件已移到 config/ 下）
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tpl = os.path.normpath(os.path.join(base, tpl))
        if os.path.isfile(tpl):
            cfg["template"] = tpl
        else:
            import logging as _lg
            _lg.getLogger("report-agent.agent").warning(
                "指定的模板不存在，继续使用配置模板: %s", tpl)

    # 统一日志：与 scheduler 一致的 handler 风格
    output_dir = cfg.get("output_dir", "outputs")
    os.makedirs(output_dir, exist_ok=True)
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "outputs", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                os.path.join(logs_dir, "agent.log"),
                encoding="utf-8",
            ),
        ],
        force=True,  # 覆盖已有配置（scheduler 调用时也统一格式）
    )

    date = None
    if report_date:
        date = dt.date.fromisoformat(report_date)
    return ReportAgent(cfg).run(
        mode=mode,
        report_date=date,
        engine=engine,
        inspect_only=inspect_only,
    )


def save_summary(summary: Dict, output_dir: str) -> str:
    """把本次生成摘要保存为 JSON，便于后续归档/通知。"""
    path = os.path.join(output_dir, "last_run.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return path
