#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分析一份成品报告（DOCX / PDF），识别可替换的图片与数字，生成标注模板草稿。

用法：
  python analyze_report.py --input 报告.docx --annotate 模板草稿.docx
  python analyze_report.py --input 报告.docx --out outputs/analysis.json
  python analyze_report.py --input 报告.docx --config config.json

--annotate 会把识别为"动态"的数字改成 {{stats.*}} / {{data.N}} 占位符，
把动态图表段落改成 {{chart.<ID>}}（尽量带上监测位置，与下方表格关联），
表格中同一位置的多个测点行会用 #N 索引区分（{{cell.crack.5#塔底部.avg#2}}）。
固定项（CAD 图、设计常量、"第6、7、8跨跨中布设2个GNSS测点" 等）保持不变。

--config 指定配置文件；LLM 辅助识别默认启用（llm.enabled=true 时），
API Key 优先级：config.llm.api_key > 环境变量 QWEN_API_KEY > DASHSCOPE_API_KEY。

运行日志会同时输出到控制台和 outputs/analyze_report.log。
"""

import argparse
import logging
import os
import re
import sys

from report_agent.config import load_config
from report_agent.llm_classifier import ENV_KEY_VARS
from report_agent.recognizer import annotate_docx, print_summary, recognize, save_analysis


def _setup_logging(log_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def _resolve_llm_cfg(cfg: dict, force_llm: bool = False) -> dict:
    """读取 LLM 配置；api_key 为空时从环境变量（QWEN_API_KEY 优先）补充。"""
    llm_cfg = dict(cfg.get("llm", {}) or {})
    if force_llm:
        llm_cfg["enabled"] = True
    api_key = str(llm_cfg.get("api_key") or "").strip()
    if not api_key:
        for var in ENV_KEY_VARS:
            val = os.environ.get(var, "").strip()
            if val:
                api_key = val
                llm_cfg["api_key"] = val
                log.info("LLM API Key 从环境变量 %s 获取", var)
                break
    if not llm_cfg.get("enabled"):
        log.warning("LLM 未启用（config.llm.enabled=false）；仅使用关键词启发式识别")
    elif not api_key:
        log.warning("LLM 已启用但未找到 API Key（config.llm.api_key 或环境变量 %s）",
                    " / ".join(ENV_KEY_VARS))
    else:
        log.info("LLM 辅助识别已启用：model=%s, api_base=%s",
                 llm_cfg.get("model") or "qwen-plus",
                 llm_cfg.get("api_base") or "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return llm_cfg


def _ensure_bridge_config(config_path: str, input_path: str) -> str:
    """config 的桥数据未配置或名称对照缺失时，按输入报告自动生成 config_<桥>.json。

    图库/统计值/名称对照均从 preprocess 目录自动定位（setup_bridge.build_config）。
    """
    if not config_path or not os.path.isfile(config_path):
        return config_path
    try:
        with open(config_path, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return config_path
    bd = c.get("bridge_data") or {}
    nd = bd.get("name_dict", "")
    if bd.get("enabled") and nd:
        nd_abs = nd if os.path.isabs(nd) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), nd)
        if os.path.isfile(nd_abs):
            return config_path
    try:
        from setup_bridge import build_config, register_bridge, _bridge_id
        bridge_name = os.path.splitext(os.path.basename(input_path))[0]
        bridge_name = re.sub(r"[_\- ]*成品报告$", "", bridge_name)
        bid = _bridge_id(bridge_name)
        cfg = build_config(bridge_name, input_path)
        new_path = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                                f"config_{bid}.json")
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        register_bridge(bid, bridge_name, new_path)
        log.info("自动生成桥配置: %s", new_path)
        return new_path
    except Exception as exc:  # noqa: BLE001
        log.warning("自动生成桥配置失败: %s", exc)
        return config_path


def _log_analysis_summary(analysis: dict) -> None:
    s = analysis["summary"]
    log.info("识别汇总：图片 替换%d/保留%d/待确认%d；数字 替换%d/保留%d/待确认%d；"
             "图表占位 %d 处",
             s["images"]["replace"], s["images"]["keep"], s["images"]["review"],
             s["numbers"]["replace"], s["numbers"]["keep"], s["numbers"]["review"],
             s.get("chart_texts", 0))
    llm = s.get("llm", {})
    if llm:
        log.info("LLM 二次筛选：可用=%s，完整=%s，补漏 %d，纠错 %d，文本替换 %d",
                 llm.get("enabled"), llm.get("complete"),
                 llm.get("missed", 0), llm.get("wrong", 0),
                 llm.get("text_replacements", 0))
    cell_refs = [ct for ct in analysis.get("chart_texts", []) if ct.get("source") == "cell_ref"]
    chart_ids = [ct.get("_unique_chart_id") or ct.get("chart_id")
                 for ct in analysis.get("chart_texts", []) if ct.get("_unique_chart_id")]
    located = [c for c in chart_ids if c and "_" in c and any(
        kw in c for kw in ("塔", "墩", "跨", "桥面", "断面", "锚固")
    )]
    log.info("表格单元格占位符 %d 处；带位置的图表占位符 %d/%d 个",
             len(cell_refs), len(located), len(chart_ids))
    # 同一位置多测点行索引统计
    seq_used = sum(1 for ct in analysis.get("chart_texts", [])
                   if ct.get("source") == "cell_ref" and ct.get("_cell_seq", 0) > 1)
    if seq_used:
        log.info("同一位置多测点行（已加 #N 索引）涉及 %d 个表格单元格", seq_used)


def main() -> int:
    parser = argparse.ArgumentParser(description="成品报告解析识别")
    parser.add_argument("--input", required=True, help="输入报告 .docx / .pdf")
    parser.add_argument("--out", default=None, help="识别结果 JSON 输出路径")
    parser.add_argument("--annotate", default=None,
                        help="(DOCX) 生成标注草稿 .docx 的路径")
    parser.add_argument("--config", default="config/config.json",
                        help="配置文件路径（若 llm.enabled=true 则启用 LLM 辅助识别）")
    parser.add_argument("--llm", action="store_true",
                        help="强制启用 LLM 辅助识别（忽略 config 中的 llm.enabled 设置）")
    parser.add_argument("--log", default=os.path.join("outputs", "logs", "analyze_report.log"),
                        help="运行日志文件路径（默认 outputs/logs/analyze_report.log）")
    args = parser.parse_args()

    _setup_logging(args.log)

    if args.config:
        args.config = _ensure_bridge_config(args.config, args.input)

    if not os.path.isfile(args.input):
        log.error("文件不存在: %s", args.input)
        return 1

    # 加载 LLM 配置（api_key 为空时从环境变量 QWEN_API_KEY / DASHSCOPE_API_KEY 补充）
    llm_cfg = None
    try:
        cfg = load_config(args.config)
        llm_cfg = _resolve_llm_cfg(cfg, force_llm=args.llm)
    except FileNotFoundError:
        if args.llm:
            log.warning("配置文件未找到，LLM 使用默认配置")
            llm_cfg = _resolve_llm_cfg({"llm": {"enabled": True}}, force_llm=True)
        else:
            log.warning("配置文件未找到: %s（不使用 LLM）", args.config)

    log.info("开始解析报告: %s", args.input)
    # 加载传感器对照表（用于“编号(特征)_图型”行反查监测部位，
    # 生成位置化图表占位符，避免 chart_sensor_<编号>_<图型> 难匹配）
    sensor_map = {}
    try:
        cfg_for_map = load_config(args.config)
        sm_path = (cfg_for_map.get("bridge_data") or {}).get("sensor_map", "")
        if not sm_path:
            # 默认固定产物位置: preprocess/传感器对照/传感器编号名称.json
            cand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "preprocess", "传感器对照",
                                "传感器编号名称.json")
            if os.path.isfile(cand):
                sm_path = cand
        if sm_path:
            if not os.path.isabs(sm_path):
                sm_path = os.path.join(
                    os.path.dirname(os.path.abspath(args.config)), sm_path)
            if os.path.isfile(sm_path):
                with open(sm_path, encoding="utf-8") as f:
                    sensor_map = (json.load(f) or {}).get("传感器", {}) or {}
                log.info("传感器对照表已加载: %s（%d 个）",
                         sm_path, len(sensor_map))
    except Exception:  # noqa: BLE001
        sensor_map = {}
    analysis = recognize(args.input, llm_cfg=llm_cfg, sensor_map=sensor_map)
    log.info("解析完成: 图片 %d 张，数字 %d 个，图表占位 %d 处",
             len(analysis["images"]), len(analysis["numbers"]),
             len(analysis.get("chart_texts", [])))

    out = args.out or os.path.join(
        "outputs", "analysis",
        "analysis_" + os.path.splitext(os.path.basename(args.input))[0] + ".json",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if args.annotate:
        if not args.input.lower().endswith(".docx"):
            log.warning("--annotate 仅支持 DOCX 输入，跳过标注")
        else:
            log.info("生成标注草稿: %s", args.annotate)
            result = annotate_docx(args.input, args.annotate, llm_cfg=llm_cfg,
                                   analysis=analysis, sensor_map=sensor_map)
            log.info("标注草稿已生成: %s", result["output"])
            log.info("替换数字 %d 个（跨格式跳过 %d 个），替换图片 %d 张，"
                     "图表文本 %d 处，文本 %d 处，data 占位符 %d 个",
                     result["replaced_numbers"], result["skipped_numbers_split_runs"],
                     result["replaced_images"], result.get("replaced_chart_texts", 0),
                     result.get("replaced_texts", 0), len(analysis.get("data_values", {})))

    # 在 annotate 之后保存，确保 data_values 被写入 JSON
    save_analysis(analysis, out)
    log.info("识别结果已保存: %s", out)
    _log_analysis_summary(analysis)

    print_summary(analysis)
    log.info("全部完成。日志: %s", os.path.abspath(args.log))
    return 0


log = logging.getLogger("analyze-report")


if __name__ == "__main__":
    sys.exit(main())
