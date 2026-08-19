#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按一份成品报告自动生成该桥的 config_<桥>.json。

用法：
  python setup_bridge.py --input inputs/矮寨大桥.docx
  python setup_bridge.py --input 报告.docx --id aizhai --name 矮寨

自动完成：
  1. 从文件名推断桥名（矮寨大桥.docx -> 矮寨）；
  2. 传感器名称对照取 preprocess/传感器对照/传感器名称对照/<桥名>.json
     （兼容 统计值_*/传感器名称对照/）；
  3. 传感器编号名称表取 preprocess/传感器对照/传感器编号名称.json；
  4. 图库/统计值取 preprocess 下最新的 图库_* / 统计值_* 目录（优先 status.json 的 dirs）；
  5. 生成 config_<桥ID>.json（LLM 默认 qwen-plus，指标用通用默认集）；
  6. 登记到 bridges/registry.json。
"""

import argparse
import glob
import json
import logging
import os
import re
import sys


log = logging.getLogger("setup-bridge")

BASE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_METRICS = {
    "temperature": {"feature": "WSD(temp)", "unit": "℃", "label": "环境温度"},
    "structure_temperature": {"feature": "WD(temp)", "unit": "℃", "label": "结构温度"},
    "humidity": {"feature": "WSD(rh)", "unit": "%", "label": "环境湿度"},
    "wind_speed": {"feature": "FSFX2(spfs)", "unit": "m/s", "label": "风速"},
    "cable_force": {"feature": "SL(sl)", "unit": "kN", "label": "索力"},
    "displacement": {"feature": "GNSS(Δx)", "unit": "mm", "label": "空间变位"},
    "bearing_displacement": {"feature": "WY(Δx)", "unit": "mm", "label": "支座位移"},
    "deflection": {"feature": "ND(nd)", "unit": "mm", "label": "挠度"},
    "strain": {"feature": "YB(rsg)", "unit": "με", "label": "应变"},
    "vibration": {"feature": "", "unit": "m/s²", "label": "振动"},
    "rotation": {"feature": "EZJD(xJd)", "unit": "°", "label": "倾角",
                 "aliases": ["倾角"]},
    "earthquake_load": {"feature": "DZJSD(xJsd)", "unit": "m/s²", "label": "地震"},
    "crack": {"feature": "LF(Δx)", "unit": "mm", "label": "裂缝"},
    "vehicle_count": {"feature": "", "unit": "辆", "label": "交通荷载"},
}


def _name_match(a: str, b: str) -> bool:
    """桥名变体匹配：洣水河特大桥 <-> 洣水河 <-> 洣水。"""
    if not a or not b:
        return False

    def _strip(x):
        for s in ("特大桥", "大桥"):
            if x.endswith(s):
                return x[: -len(s)]
        return x

    x, y = _strip(str(a)), _strip(str(b))
    return x in y or y in x


def _latest_dir(pattern: str, bridge_name: str = "") -> str:
    """取 preprocess 下最新的 图库_* / 统计值_* 目录(含桥名子目录)。
    只接受与桥名变体匹配的子目录，避免取到其他桥的子目录。"""
    dirs = [d for d in glob.glob(os.path.join(BASE, "preprocess", pattern))
            if os.path.isdir(d)]
    if not dirs:
        return ""
    top = max(dirs, key=os.path.getmtime)
    subs = [os.path.join(top, s) for s in os.listdir(top)
            if os.path.isdir(os.path.join(top, s))]
    if subs:
        if bridge_name:
            hit = next((p for p in subs
                        if _name_match(bridge_name, os.path.basename(p))),
                       None)
            if hit:
                return hit
        return ""   # 没有匹配的桥子目录时返回空，不取别的桥
    return top


def find_bridge_assets(bridge_name: str) -> dict:
    """定位 名称对照 / 编号名称表 / 统计值 / 图库。"""
    assets = {"name_dict": "", "sensor_map": "", "stats_dir": "", "charts_dir": ""}
    cands = []
    nd_names = [f"{bridge_name}大桥.json", f"{bridge_name}.json"]
    for suffix in ("特大桥", "大桥"):
        if bridge_name.endswith(suffix):
            core = bridge_name[: -len(suffix)]
            nd_names += [f"{core}大桥.json", f"{core}.json"]
    for root in ("preprocess/传感器对照/传感器名称对照",
                 "preprocess/统计值_2026.1~3/传感器名称对照"):
        for fn in nd_names:
            p = os.path.join(BASE, root, fn)
            if os.path.isfile(p):
                cands.append(p)
    # 没有精确匹配时，用 名称对照 文件里“桥名”字段匹配
    if not cands:
        base_dir = os.path.join(BASE, "preprocess/传感器对照/传感器名称对照")
        for fn in os.listdir(base_dir) if os.path.isdir(base_dir) else []:
            try:
                with open(os.path.join(base_dir, fn), encoding="utf-8") as f:
                    data = json.load(f)
                if bridge_name in str(data.get("桥名", "")):
                    cands.append(os.path.join(base_dir, fn))
            except Exception:
                continue
    assets["name_dict"] = cands[0] if cands else ""

    sm = os.path.join(BASE, "preprocess/传感器对照/传感器编号名称.json")
    if os.path.isfile(sm):
        assets["sensor_map"] = sm

    # status.json 优先；否则取最新年月目录
    try:
        with open(os.path.join(BASE, "preprocess", "status.json"), encoding="utf-8") as f:
            status = json.load(f)
        dirs = status.get("dirs") or {}
        st = dirs.get("stats", "") or ""
        ch = dirs.get("charts", "") or ""
        # status.json 的目录可能属于最近一次运行的其他桥，必须先做桥名匹配
        assets["stats_dir"] = (
            st if (_name_match(bridge_name, os.path.basename(os.path.normpath(st))))
            else "") or _latest_dir("统计值_*", bridge_name)
        assets["charts_dir"] = (
            ch if (_name_match(bridge_name, os.path.basename(os.path.normpath(ch))))
            else "") or _latest_dir("图库_*", bridge_name)
    except Exception:
        assets["stats_dir"] = _latest_dir("统计值_*", bridge_name)
        assets["charts_dir"] = _latest_dir("图库_*", bridge_name)
    return assets


def _bridge_id(name: str) -> str:
    """桥名 -> 小写拼音风格 id（矮寨大桥 -> aizhai）。"""
    core = re.sub(r"(?:特)?大桥$", "", name or "")
    m = re.match(r"^([\u4e00-\u9fa5]+)", core)
    table = {
        "赤石": "chishi", "洞庭湖": "dongtinghu", "洣水河": "mishuihe",
        "湘江": "xiangjiang", "矮寨": "aizhai",
    }
    return table.get(m.group(1) if m else "", (m.group(1) if m else core) or "bridge")


def _latest_template(bridge_name: str) -> str:
    """取 templates/ 下某桥最新的模板文件（相对路径，取不到返回基础名）。

    排序规则：
      1. 文件名里的版本号 vN 大的优先（v9 > v8 > ... > 无版本号）；
      2. 版本号相同(含都无版本号)时，取最后修改时间最新的。
    """
    tpl_dir = os.path.join(BASE, "templates")
    prefix = bridge_name + "_template"
    hits = []
    if os.path.isdir(tpl_dir):
        for fn in os.listdir(tpl_dir):
            if not fn.lower().endswith(".docx"):
                continue
            stem = fn[:-5]
            if stem == prefix:
                ver = 0
            else:
                m = re.match(re.escape(prefix) + r"_v(\d+)$", stem)
                if not m:
                    continue
                ver = int(m.group(1))
            p = os.path.join(tpl_dir, fn)
            hits.append((ver, os.path.getmtime(p), fn))
    if not hits:
        return f"templates/{bridge_name}_template.docx"
    _, _, best = max(hits, key=lambda h: (h[0], h[1]))
    return "templates/" + best


def build_config(bridge_name: str, input_path: str) -> dict:
    assets = find_bridge_assets(bridge_name)
    bid = _bridge_id(bridge_name)
    rel = lambda p: os.path.relpath(p, BASE).replace("\\", "/") if p else ""
    return {
        "template": _latest_template(bridge_name),
        "output_dir": ".\\outputs\\reports",
        "source_report": rel(os.path.abspath(input_path)),
        "bridge_data": {
            "enabled": True,
            "bridge_name": bridge_name,
            "stats_dir": rel(assets["stats_dir"]),
            "charts_dir": rel(assets["charts_dir"]),
            "sensor_map": rel(assets["sensor_map"]),
            "overview": os.path.join(rel(assets["stats_dir"]), "总览.json") if assets["stats_dir"] else "",
            "name_dict": rel(assets["name_dict"]),
            "fuzzy_threshold": 0.7,
            "period_aggregate": True,
            "auto_fill_missing_charts": True,
            "text_replace": {"堡": "墩"},
            "metrics": DEFAULT_METRICS,
            "sensor_exclude": [],
            "sensor_aliases": {},
            "chart_map": {},
        },
        "report": {"name_prefix": bridge_name, "with_timestamp": False},
        "llm": {
            "enabled": True,
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "",
            "model": "qwen-plus",
            "review_low": 0.38,
            "review_high": 0.68,
            "timeout": 300,
            "batch_size": 20,
        },
        "charts": {"engine": "python", "output_dir": "outputs\\charts",
                   "width_inches": 5.8, "definitions": [], "matlab": {}},
        "schedule": {"mode": "quarterly", "weekday": 1, "day_of_month": 1,
                     "hour": 8, "minute": 0, "use_apscheduler": True},
        "period": {"weekly_days": 7, "monthly_days": 30},
    }


def register_bridge(bid: str, bridge_name: str, config_path: str) -> None:
    reg_path = os.path.join(BASE, "bridges", "registry.json")
    if not os.path.isfile(reg_path):
        return
    with open(reg_path, encoding="utf-8") as f:
        reg = json.load(f)
    bridges = reg.setdefault("bridges", [])
    for b in bridges:
        if b.get("id") == bid:
            b["name"] = bridge_name
            b["config"] = os.path.relpath(config_path, BASE).replace("\\", "/")
            break
    else:
        bridges.append({"id": bid, "name": bridge_name,
                        "config": os.path.relpath(config_path, BASE).replace("\\", "/"),
                        "host": "", "port": 8456})
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="自动生成桥梁报告配置文件")
    parser.add_argument("--input", required=True, help="成品报告 .docx 路径")
    parser.add_argument("--id", default=None, help="桥 ID（默认按桥名拼音映射）")
    parser.add_argument("--name", default=None, help="桥名（默认取文件名，如 矮寨大桥.docx -> 矮寨）")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        log.error("文件不存在: %s", input_path)
        return 1
    bridge_name = args.name or re.sub(r"\.docx$", "", os.path.basename(input_path))
    bridge_name = re.sub(r"[_\- ]*成品报告$", "", bridge_name)
    bid = args.id or _bridge_id(bridge_name)
    cfg = build_config(bridge_name, input_path)
    if not cfg["bridge_data"]["name_dict"]:
        log.warning("未找到 %s 的传感器名称对照（preprocess/传感器对照/传感器名称对照/），"
                    "配置文件仍会生成，运行解析可能不完整", bridge_name)
    cfg_dir = os.path.join(BASE, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, f"config_{bid}.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    register_bridge(bid, bridge_name, cfg_path)
    log.info("已生成配置文件: %s", cfg_path)
    log.info("  桥名=%s 名称对照=%s", bridge_name, cfg["bridge_data"]["name_dict"] or "（未找到）")
    log.info("  统计值=%s 图库=%s", cfg["bridge_data"]["stats_dir"], cfg["bridge_data"]["charts_dir"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
