# -*- coding: utf-8 -*-
"""配置加载：config.json，支持相对路径解析与命令行覆盖。"""

import json
import os
import re
import logging


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config.json")


def _resolve_path(base_dir: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def _resolve_template(base_dir: str, cfg: dict) -> str:
    """解析模板路径；配置里的模板不存在时，自动回退到 templates/ 下
    该桥最新版本(<桥名>_template_vN.docx，版本号最大优先)。"""
    tpl = cfg.get("template", "")
    resolved = _resolve_path(base_dir, tpl) if tpl else ""
    if resolved and os.path.isfile(resolved):
        return resolved
    bname = (cfg.get("bridge_data") or {}).get("bridge_name", "")
    tpl_dir = os.path.join(base_dir, "templates")
    if bname and os.path.isdir(tpl_dir):
        prefix = bname + "_template"
        best, best_key = "", (-1, 0.0)
        for fn in os.listdir(tpl_dir):
            if not fn.lower().endswith(".docx"):
                continue
            stem = fn[:-5]
            ver = 0
            if stem != prefix:
                m = re.match(re.escape(prefix) + r"_v(\d+)$", stem)
                if not m:
                    continue
                ver = int(m.group(1))
            p = os.path.join(tpl_dir, fn)
            key = (ver, os.path.getmtime(p))
            if key > best_key:
                best_key, best = key, p
        if best:
            logging.getLogger("report-agent.config").warning(
                "配置模板不存在(%s)，回退到最新模板: %s", resolved, best)
            return best
    return resolved


def _strip_bridge_suffix(name: str) -> str:
    for s in ("特大桥", "大桥"):
        if name.endswith(s):
            return name[: -len(s)]
    return name


def bridge_dir_match(bridge_name: str, dir_name: str) -> bool:
    """桥名与目录名兼容匹配：湘江特大桥 <-> 湘江特 <-> 湘江 都算匹配。"""
    a = _strip_bridge_suffix(str(bridge_name or ""))
    b = _strip_bridge_suffix(str(dir_name or ""))
    return bool(a and b) and (a in b or b in a)


def name_dict_candidates(bridge_name: str) -> list:
    """生成 传感器名称对照 文件名候选（兼容 湘江特大桥/湘江特/…大桥 写法）。"""
    name = str(bridge_name or "").strip()
    cands = []
    if not name:
        return cands
    cands.append(f"{name}大桥.json")
    cands.append(f"{name}.json")
    for suffix in ("特大桥", "大桥"):
        if name.endswith(suffix):
            core = name[: -len(suffix)]
            cands.append(f"{core}大桥.json")
            cands.append(f"{core}.json")
    return cands


def resolve_bridge_subdir(path: str, bridge_name: str) -> str:
    """配置里的图库/统计值指向期号根目录(如 统计值_2026.1~3)时，
    若其中存在桥名子目录(湘江特 / 湘江特大桥 等变体)，自动下钻到该子目录，
    避免桥名写法不一致导致“缺失”。"""
    if not bridge_name or not path:
        return path
    if os.path.isdir(path):
        # 本身已是桥名目录(如 .../湘江特)时直接用
        if bridge_dir_match(bridge_name, os.path.basename(path)):
            return path
        # 期号根目录下存在桥名子目录时下钻
        try:
            for entry in sorted(os.listdir(path)):
                cand = os.path.join(path, entry)
                if os.path.isdir(cand) and bridge_dir_match(bridge_name, entry):
                    return cand
        except OSError:
            pass
        return path
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return path
    try:
        for entry in sorted(os.listdir(parent)):
            cand = os.path.join(parent, entry)
            if os.path.isdir(cand) and bridge_dir_match(bridge_name, entry):
                return cand
    except OSError:
        pass
    return path


def _apply_latest_bridge_dirs(cfg: dict, base: str) -> None:
    """用 preprocess/status.json 中最近一次生成的 图库/统计值 目录覆盖 bridge_data 路径。

    图库/统计值目录名现在带年月范围（如 图库_2026.1~3），status.json 由
    build_chart_library.py 生成后写回；目录不存在时保持配置原值。
    """
    bridge_data = cfg.get("bridge_data") or {}
    if not bridge_data:
        return
    status_path = os.path.join(base, "preprocess", "status.json")
    if not os.path.isfile(status_path):
        return
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            status = json.load(f)
        dirs = status.get("dirs") or {}
    except Exception:
        return
    stats = dirs.get("stats") or ""
    charts = dirs.get("charts") or ""
    # status.json 里的目录是“最近一次 pipeline 运行”的产物，可能属于其他桥；
    # 只有当目录名与当前桥名兼容（桥名变体匹配）时才允许覆盖，
    # 避免新桥路径还没生成时被上一次其他桥的目录顶替。
    bname = bridge_data.get("bridge_name", "") or ""
    for _key, _val in (("stats", stats), ("charts", charts)):
        if _val and bname:
            _leaf = os.path.basename(os.path.normpath(_val))
            if not bridge_dir_match(bname, _leaf):
                if _key == "stats":
                    stats = ""
                else:
                    charts = ""
    # 配置里已显式指定且路径存在时，不覆盖（模拟/服务器绝对路径优先）
    cur_stats = bridge_data.get("stats_dir", "")
    cur_charts = bridge_data.get("charts_dir", "")
    if cur_stats and os.path.isdir(_resolve_path(base, cur_stats)):
        stats = ""
    if cur_charts and os.path.isdir(_resolve_path(base, cur_charts)):
        charts = ""
    if stats and os.path.isdir(stats):
        bridge_data["stats_dir"] = stats
        # 传感器对照表是固定产物，统一放 preprocess/传感器对照/，
        # 不再从统计值目录读取；旧布局(统计值目录内)仍兼容回退
        map_dir = os.path.join(base, "preprocess", "传感器对照")
        sm = os.path.join(map_dir, "传感器编号名称.json")
        if os.path.isfile(sm):
            bridge_data["sensor_map"] = sm
        nd = os.path.join(map_dir, "传感器名称对照")
        if os.path.isdir(nd):
            for fn in name_dict_candidates(bname):
                nd_file = os.path.join(nd, fn)
                if os.path.isfile(nd_file):
                    bridge_data["name_dict"] = nd_file
                    break
        # 旧布局回退：统计值目录内仍存在对照表时使用
        sm = os.path.join(stats, "传感器编号名称.json")
        if os.path.isfile(sm) and "sensor_map" not in bridge_data:
            bridge_data["sensor_map"] = sm
        nd = os.path.join(stats, "传感器名称对照")
        if os.path.isdir(nd) and not bridge_data.get("name_dict"):
            for fn in name_dict_candidates(bname):
                nd_file = os.path.join(nd, fn)
                if os.path.isfile(nd_file):
                    bridge_data["name_dict"] = nd_file
                    break
    if charts and os.path.isdir(charts):
        bridge_data["charts_dir"] = charts


def load_config(config_path: str = None) -> dict:
    """加载配置文件，并把所有相对路径转换为相对于配置文件目录的绝对路径。"""
    path = config_path or os.environ.get("REPORT_AGENT_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 所有相对路径统一相对项目根目录解析（config 文件统一放 <根>/config/ 下，
    # 与旧版“config 放在项目根目录”行为保持一致）
    base = PROJECT_ROOT
    cfg["_config_path"] = path
    cfg["template"] = _resolve_template(base, cfg)
    cfg["output_dir"] = _resolve_path(base, cfg.get("output_dir", "outputs"))

    # bridge_data 路径（统计值/图库/对照表/名称字典/总览）同样解析为绝对路径，
    # 避免 BridgeData 等按配置文件所在目录解析时因 config/ 目录导致路径错位
    bd = cfg.get("bridge_data") or {}
    for key in ("stats_dir", "charts_dir", "sensor_map", "name_dict",
                "overview"):
        v = bd.get(key, "")
        if v:
            bd[key] = _resolve_path(base, v)

    data = cfg.setdefault("data", {})
    if data.get("file"):
        data["file"] = _resolve_path(base, data["file"])

    charts = cfg.setdefault("charts", {})
    if charts.get("output_dir"):
        charts["output_dir"] = _resolve_path(base, charts["output_dir"])
    matlab = charts.setdefault("matlab", {})
    if matlab.get("script"):
        matlab["script"] = _resolve_path(base, matlab["script"])

    # 成品报告路径（用于 recognizer 和输出文件名推导）
    if cfg.get("source_report"):
        cfg["source_report"] = _resolve_path(base, cfg["source_report"])

    # 加载 chart_texts：从 analysis_file 或自动推导的分析 JSON 中读取
    # chart_texts 是模板识别阶段提取的图表占位信息，运行时需要用它生成图表定义
    analysis_file = cfg.get("analysis_file")
    if analysis_file:
        analysis_file = _resolve_path(base, analysis_file)
    elif cfg.get("source_report"):
        # 自动推导：outputs/analysis_<basename>.json
        basename = os.path.splitext(os.path.basename(cfg["source_report"]))[0]
        auto_path = os.path.join(cfg["output_dir"], f"analysis_{basename}.json")
        if os.path.isfile(auto_path):
            analysis_file = auto_path

    if analysis_file and os.path.isfile(analysis_file):
        try:
            with open(analysis_file, "r", encoding="utf-8") as f:
                analysis = json.load(f)
            chart_texts = analysis.get("chart_texts", [])
            if chart_texts:
                cfg["_chart_texts"] = chart_texts
            # 加载 data_values：annotate_docx 阶段保存的 {{data.N}} -> 原始值映射
            data_values = analysis.get("data_values", {})
            # 以 numbers 中的 {{data.N}} 条目为准重建映射（历史 analysis 的
            # data_values 可能是旧脏值，如车辆数 5/6/6 而非 758279 等）
            data_number_meta = {}
            for _n in analysis.get("numbers", []) or []:
                _ph = _n.get("placeholder") or ""
                if _ph.startswith("{{data.") and _ph.endswith("}}"):
                    _key = _ph.strip("{}")
                    data_values[_key] = str(_n.get("value", ""))
                    data_number_meta[_key] = {
                        "value": str(_n.get("value", "")),
                        "context": str(_n.get("context", "")),
                        "snippet": str(_n.get("snippet", "")),
                        "paragraph": _n.get("paragraph"),
                        "reasons": list(_n.get("reasons", []) or []),
                    }
            if data_values:
                cfg["_data_values"] = data_values
            if data_number_meta:
                cfg["_data_number_meta"] = data_number_meta
            # 全文档文本流（按段落索引），用于图表位置上下文继承
            texts = analysis.get("texts", [])
            if texts:
                cfg["_texts"] = texts
        except (json.JSONDecodeError, KeyError) as exc:
            import logging
            logging.getLogger("report-agent.config").warning(
                "加载 chart_texts / data_values 失败: %s", exc
            )

    _apply_latest_bridge_dirs(cfg, base)
    # 桥名子目录下钻：统计值_2026.1~3 -> 统计值_2026.1~3/湘江特
    bd = cfg.get("bridge_data") or {}
    if bd:
        bname = bd.get("bridge_name", "")
        for key in ("stats_dir", "charts_dir"):
            v = bd.get(key, "")
            if v:
                resolved = resolve_bridge_subdir(
                    _resolve_path(base, v), bname)
                if resolved != _resolve_path(base, v):
                    bd[key] = resolved
    return cfg
