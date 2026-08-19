#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据分析报告智能体 CLI。

用法示例：
  python run_agent.py                          # 按配置默认模式生成一次
  python run_agent.py --mode weekly            # 生成周报
  python run_agent.py --mode monthly           # 生成月报
  python run_agent.py --mode weekly --date 2026-08-04   # 指定报告结束日
  python run_agent.py --engine python          # 强制使用 Python 出图
  python run_agent.py --inspect-template       # 识别模板中需要替换的数据
  python run_agent.py --bridge chishi --mode quarterly --year 2026 --quarter 1
                                               # 按季度号生成（第1季度）
  python run_agent.py --list-bridges           # 列出所有已注册桥梁
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import traceback

from report_agent.agent import run_once, save_summary
from report_agent.config import load_config
from report_agent.template_analyzer import analyze_template, print_analysis

log = logging.getLogger("report-agent.cli")


def main() -> int:
    # 基础日志配置（run_once 会以 force=True 覆盖为更完整的配置）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="数据分析报告智能体")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 config/config.json）")
    parser.add_argument("--bridge", default=None,
                        help="桥梁 ID（从 bridges/registry.json 解析配置文件）")
    parser.add_argument("--list-bridges", action="store_true",
                        help="列出已注册的桥梁及其服务器")
    parser.add_argument("--mode", choices=["weekly", "monthly", "quarterly", "yearly", "manual"], default=None,
                        help="报告模式：weekly=周报，monthly=月报，quarterly=季度报，yearly=年度报，manual=手动")
    parser.add_argument("--date", default=None, help="报告结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--quarter", type=int, default=None,
                        help="季度号 1~4（仅 --mode quarterly 使用，如 --quarter 1）")
    parser.add_argument("--year", type=int, default=None,
                        help="报告年份（默认今年；配合 --quarter 使用，如 --year 2026 --quarter 1）")
    parser.add_argument("--engine", choices=["auto", "matlab", "python"], default=None,
                        help="图表引擎：auto 自动选择，matlab 强制 MATLAB，python 强制 matplotlib")
    parser.add_argument("--template", default=None,
                        help="本次运行使用的模板 .docx 路径(不修改配置文件)")
    parser.add_argument("--inspect-template", action="store_true",
                        help="只识别模板中的动态内容并输出分析 JSON，不生成报告")
    args = parser.parse_args()

    try:
        if args.list_bridges:
            from report_agent.bridges import list_bridges
            bridges = list_bridges()
            if not bridges:
                print("未找到桥梁注册表（bridges/registry.json）或其中没有桥梁。")
                return 1
            print(f"{'ID':<12}{'名称':<16}{'服务器':<18}{'配置':<32}端口")
            for b in bridges:
                print(f"{b.get('id',''):<12}{b.get('name',''):<16}"
                      f"{b.get('host',''):<18}{b.get('config',''):<32}{b.get('port','')}")
            return 0

        if args.bridge:
            from report_agent.bridges import resolve_bridge_config
            resolved = resolve_bridge_config(args.bridge)
            if not resolved:
                print(f"[错误] 未找到桥梁 '{args.bridge}' 的配置文件"
                      f"（请检查 bridges/registry.json）")
                return 1
            if args.config and args.config != resolved:
                print(f"[提示] --bridge 覆盖 --config：使用 {resolved}")
            args.config = resolved

        if args.inspect_template:
            cfg = load_config(args.config)
            result = analyze_template(cfg["template"])
            print_analysis(result)
            analysis_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "outputs", "analysis")
            os.makedirs(analysis_dir, exist_ok=True)
            out = os.path.join(analysis_dir, "template_analysis.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"\n识别结果已保存: {out}")
            return 0

        report_date = args.date
        if (args.mode == "quarterly" and args.quarter is None
                and not args.date and not args.year):
            # 未指定季度/日期：默认最近一个已完整结束的季度
            from report_agent.agent import last_completed_quarter
            args.year, args.quarter = last_completed_quarter()
        elif (args.mode == "quarterly" and args.quarter is None
                and args.year is not None):
            print("[错误] 季度模式下需要同时提供 --quarter（1~4）", file=sys.stderr)
            return 1
        if args.mode == "quarterly" and args.quarter is not None:
            if not 1 <= args.quarter <= 4:
                print(f"[错误] 季度必须是 1~4，收到: {args.quarter}", file=sys.stderr)
                return 1
            from report_agent.agent import quarter_range
            year = args.year or dt.date.today().year
            start, end = quarter_range(year, args.quarter)
            if end > dt.date.today():
                print(
                    f"[错误] 第{args.quarter}季度（{start.isoformat()} ~ "
                    f"{end.isoformat()}）尚未结束，请等该季度过完后再生成。",
                    file=sys.stderr,
                )
                return 1
            report_date = end.isoformat()
        elif args.quarter is not None:
            print("[错误] --quarter 只能在 --mode quarterly 时使用", file=sys.stderr)
            return 1

        summary = run_once(
            config_path=args.config,
            mode=args.mode,
            report_date=report_date,
            engine=args.engine,
            template_override=args.template,
        )
        cfg = load_config(args.config)
        save_summary(summary, cfg["output_dir"])
        return 0
    except Exception as exc:  # noqa: BLE001
        # 记录完整堆栈，而非仅打印错误消息——定时调度场景下便于定位根因
        tb = traceback.format_exc()
        log.error("报告生成失败: %s\n%s", exc, tb)
        print(f"[错误] {exc}", file=sys.stderr)
        print(f"[traceback]\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
