#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常驻调度服务：按 config/config.json 中的 schedule 配置，每周/每月自动生成报告。

用法：
  python serve_scheduler.py
  python serve_scheduler.py --config /path/to/config/config.json
  python serve_scheduler.py --bridge chishi
"""

import argparse
import os
import sys

from report_agent.bridges import resolve_bridge_config
from report_agent.scheduler import run_forever


def main() -> None:
    parser = argparse.ArgumentParser(description="报告定时调度服务")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--bridge", default=None,
                        help="桥梁 ID（从 bridges/registry.json 解析配置文件）")
    args = parser.parse_args()
    config = args.config
    if args.bridge:
        config = resolve_bridge_config(args.bridge)
        if not config:
            print(f"[错误] 未找到桥梁 '{args.bridge}' 的配置文件", file=sys.stderr)
            raise SystemExit(1)
    run_forever(config)


if __name__ == "__main__":
    main()
