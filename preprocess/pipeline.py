# -*- coding: utf-8 -*-
"""桥数据预处理管道：把相互独立的预处理脚本串成一条命令。

流程：
  1) 秒级原始数据 -> 日级/小时级数据     scripts/preprocess_sensor_data.py
  2) 日级数据 -> 图库 + 统计值           scripts/build_chart_library.py
  3) 测点编号表格.docx -> 传感器对照表   scripts/parse_sensor_map.py

配置：
  preprocess/config.json（可用 --config 指定其他文件），
  所有路径均可被命令行参数覆盖：
    --raw <秒级数据目录>
    --daily <日级数据输出根目录>
    --charts <图库目录>
    --stats <统计值目录>
    --sensor-map-docx <五座桥测点编号表格.docx>
    --bridge <桥名>

常用：
  python preprocess/pipeline.py                        # 全流程
  python preprocess/pipeline.py --skip-preprocess      # 只重建图库/统计值/对照表
  python preprocess/pipeline.py --skip-charts          # 只做预处理
  python preprocess/pipeline.py --limit-sensors 10     # 试跑前 10 个传感器
"""

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(ROOT, "config.json")
STATUS_PATH = os.path.join(ROOT, "status.json")
LOG_PATH = os.path.join(ROOT, "pipeline.log")

log = logging.getLogger("preprocess-pipeline")


def load_config(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(v: str) -> str:
    """把配置/命令行里的相对路径解析为相对 preprocess/ 目录的绝对路径；
    绝对路径保持不变。这样整个项目拷到别的机器也能直接跑。"""
    if not v:
        return v
    v = os.path.expandvars(os.path.expanduser(v))
    if os.path.isabs(v):
        return os.path.normpath(v)
    return os.path.normpath(os.path.join(ROOT, v))


def save_status(status: dict) -> None:
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def period_tag(start="", end=""):
    """由起止日期生成年月标签（与 preprocess_sensor_data/build_chart_library 一致）。"""
    def _parse(s):
        try:
            return dt.date.fromisoformat(str(s).strip())
        except (ValueError, AttributeError):
            return None
    d0, d1 = _parse(start), _parse(end)
    if not d0 or not d1:
        return ""
    if d0.year == d1.year:
        if d0.month == d1.month:
            return f"{d0.year}.{d0.month:02d}"
        return f"{d0.year}.{d0.month}~{d1.month}"
    return f"{d0.year}.{d0.month}~{d1.year}.{d1.month}"


def read_latest_dirs():
    """从 status.json 读取最近一次生成的 图库/统计值 目录。"""
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        dirs = d.get("dirs") or {}
        return dirs.get("charts"), dirs.get("stats")
    except Exception:
        return None, None


def run_step(name: str, cmd: list, status: dict) -> bool:
    """执行一步，日志追加到 pipeline.log，返回是否成功。"""
    log.info("== 步骤 %s: %s", name, " ".join(cmd))
    step = {"name": name, "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": None}
    status["steps"].append(step)
    status["step"] = name
    status["running"] = True
    save_status(status)
    t0 = time.time()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== 步骤 {name} @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            proc = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                                  stdout=lf, stderr=subprocess.STDOUT, timeout=86400)
        ok = proc.returncode == 0
        step["seconds"] = round(time.time() - t0, 1)
        step["status"] = "ok" if ok else "failed"
        if not ok:
            log.error("步骤 %s 失败，返回码 %s", name, proc.returncode)
        return ok
    except subprocess.TimeoutExpired:
        step["status"] = "timeout"
        step["seconds"] = round(time.time() - t0, 1)
        log.error("步骤 %s 超时", name)
        return False
    except Exception as exc:  # noqa: BLE001
        step["status"] = "error"
        step["seconds"] = round(time.time() - t0, 1)
        log.exception("步骤 %s 异常: %s", name, exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="桥数据预处理管道")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--raw", default="", help="秒级原始数据根目录")
    ap.add_argument("--daily", default="", help="日级数据输出根目录")
    ap.add_argument("--charts", default="", help="图库目录")
    ap.add_argument("--stats", default="", help="统计值目录")
    ap.add_argument("--sensor-map-docx", default="", help="测点编号表格.docx 路径")
    ap.add_argument("--bridge", default="", help="桥名(用于传感器对照表生成)")
    ap.add_argument("--limit-sensors", type=int, default=0, help="只处理前 N 个传感器")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--skip-preprocess", action="store_true")
    ap.add_argument("--skip-charts", action="store_true")
    ap.add_argument("--skip-sensor-map", action="store_true")
    ap.add_argument("--skip-per-sensor", action="store_true",
                    help="生成图库/统计值时跳过逐传感器图，只生成按监测部位"
                         "合并的图(传给 build_chart_library.py)")
    ap.add_argument("--period", choices=["quarterly", "yearly"],
                    default="quarterly",
                    help="统计周期(传给 build_quarterly_stats.py；"
                         "yearly 时统计值汇总桥根目录下所有 daily_* 子目录)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    raw = resolve_path(args.raw or cfg.get("raw_data_dir", ""))
    daily = resolve_path(args.daily or cfg.get("daily_dir", ""))
    charts = resolve_path(args.charts or cfg.get("charts_dir", ""))
    stats = resolve_path(args.stats or cfg.get("stats_dir", ""))
    map_docx = resolve_path(args.sensor_map_docx or cfg.get("sensor_map_docx", ""))
    limit = args.limit_sensors or int(cfg.get("limit_sensors", 0) or 0)
    start = args.start or cfg.get("start", "")
    end = args.end or cfg.get("end", "")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    os.makedirs(ROOT, exist_ok=True)

    status = {
        "running": True,
        "step": "init",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "error": None,
        "dirs": {"raw": raw, "daily": daily, "charts": charts, "stats": stats},
        "steps": [],
    }
    save_status(status)

    py = sys.executable
    ok = True

    # 1) 秒级 -> 日级
    if not args.skip_preprocess:
        if not raw or not os.path.isdir(raw):
            status["error"] = f"秒级原始数据目录不存在: {raw}"
            status["running"] = False
            save_status(status)
            log.error(status["error"])
            return 1
        os.makedirs(daily, exist_ok=True)
        cmd1 = [
            py, os.path.join(ROOT, "scripts", "preprocess_sensor_data.py"),
            "--mode", "preprocess", "--data-root", raw, "--output-root", daily,
        ]
        if args.bridge:
            cmd1 += ["--bridge", args.bridge]
        tag = period_tag(start, end)
        if tag:
            cmd1 += ["--period-tag", tag]
        if start:
            cmd1 += ["--start", start]
        if end:
            cmd1 += ["--end", end]
        ok = run_step("秒级数据->日级数据", cmd1, status)
        if not ok:
            status["error"] = "预处理步骤失败"
            status["running"] = False
            save_status(status)
            return 1

    # 2) 日级 -> 图库 + 统计值
    if not args.skip_charts:
        tag = period_tag(start, end)
        daily_base = os.path.join(daily, args.bridge) if (daily and args.bridge) \
            else daily
        if args.period == "yearly" and daily_base:
            # 年度：daily 根目录传桥根，build_chart_library 自动汇总 daily_* 子目录
            daily_data = daily_base
        else:
            daily_data = ((os.path.join(daily_base, f"daily_{tag}") if tag
                           else os.path.join(daily_base, "daily"))
                          if daily_base else "")
        cmd = [
            py, os.path.join(ROOT, "scripts", "build_chart_library.py"),
            "--daily-root", daily_data or ".",
            "--mode", "merged", "--dpi", "200",
            "--lib-root", ROOT,
        ]
        if args.bridge:
            cmd += ["--bridge", args.bridge]
        # 建图库固定跳过逐传感器图，只生成按监测部位合并的图（更省时，
        # 报告只用合并图）
        cmd += ["--skip-per-sensor"]
        if args.period == "yearly" and start and len(start) >= 4:
            # 年度：让 build_chart_library 按年份汇总桥根下所有季度 daily_* 子目录
            cmd += ["--year", start[:4]]
        # 图库/统计值目录名自动带年月范围（如 图库_2026.1~3）；
        # 仅命令行显式指定 --charts/--stats 时才用固定目录
        if args.charts:
            cmd += ["--charts-dir", args.charts]
        if args.stats:
            cmd += ["--stats-dir", args.stats]
        if limit:
            cmd += ["--limit-sensors", str(limit)]
        if start:
            cmd += ["--start", start]
        if end:
            cmd += ["--end", end]
        ok = run_step("日级数据->图库+统计值", cmd, status)
        if not ok:
            status["error"] = "图库/统计值生成步骤失败"
            status["running"] = False
            save_status(status)
            return 1

    # 2.5) 日级 -> 季度/年度统计值(按监测部位合并多传感器)
    if not args.skip_charts:
        if args.period == "yearly" and daily_base:
            stats_daily_root = daily_base      # 桥根目录, 汇总所有 daily_*
        else:
            stats_daily_root = daily_data
        cmd_q = [
            py, os.path.join(ROOT, "scripts", "build_quarterly_stats.py"),
            "--daily-root", stats_daily_root or ".",
            "--lib-root", ROOT,
            "--period", args.period,
            "--mode", "stats",
        ]
        if args.bridge:
            cmd_q += ["--bridge", args.bridge]
        if start:
            cmd_q += ["--start", start]
        if end:
            cmd_q += ["--end", end]
        ok = run_step("日级数据->季度/年度统计值", cmd_q, status)
        if not ok:
            status["error"] = "季度/年度统计值生成步骤失败"
            status["running"] = False
            save_status(status)
            return 1

    # 3) 测点编号表格 -> 传感器对照表
    if not args.skip_sensor_map and map_docx and os.path.isfile(map_docx):
        # 传感器对照表是固定产物（不随季度变化），统一放在 preprocess/传感器对照/
        map_dir = os.path.join(ROOT, "preprocess", "传感器对照")
        os.makedirs(map_dir, exist_ok=True)
        out_map = os.path.join(map_dir, "传感器编号名称.json")
        ok = run_step("测点编号表格->传感器对照表", [
            py, os.path.join(ROOT, "scripts", "parse_sensor_map.py"),
            map_docx, out_map,
        ], status)
        if not ok:
            status["error"] = "传感器对照表生成步骤失败"
            status["running"] = False
            save_status(status)
            return 1
    else:
        log.info("跳过传感器对照表（未提供 docx 或已跳过）")

    # 用 build_chart_library 写回的最新目录覆盖 status.dirs
    latest_charts, latest_stats = read_latest_dirs()
    if latest_charts:
        status["dirs"]["charts"] = latest_charts
    if latest_stats:
        status["dirs"]["stats"] = latest_stats
    status["running"] = False
    status["step"] = "done"
    status["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    status["error"] = None
    save_status(status)
    log.info("管道全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
