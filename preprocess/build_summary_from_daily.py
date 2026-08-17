#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接从 daily 目录重新汇总生成 summary.csv
==========================================

用途: 预处理已经跑完、daily/<传感器>/<特征>/<日期>.csv 已存在时，
      不需要重新读取原始数据，直接扫描 daily 目录汇总出 summary.csv
      （按 传感器/特征/日期 排序，中途中断/续跑丢失的记录也会补全）。

用法:
    python build_summary_from_daily.py [--daily-root ...] [--summary ...]

默认:
    daily-root = D:\\preprocess_sensor_data\\daily
    summary    = D:\\preprocess_sensor_data\\summary.csv
"""

import argparse
import csv
import os
import sys
import time

DEFAULT_DAILY_ROOT = r"D:\preprocess_sensor_data\daily"
DEFAULT_SUMMARY = r"D:\preprocess_sensor_data\summary.csv"


def rebuild_summary(daily_root, summary_path, limit_sensors=0,
                    start=None, end=None):
    """
    扫描 daily 目录，按天汇总出 summary 行。
    返回写入的行数；daily 目录不存在时返回 -1。
    """
    if not os.path.isdir(daily_root):
        return -1

    rows = []
    sensors = sorted(os.listdir(daily_root))
    if limit_sensors:
        sensors = sensors[:limit_sensors]

    t0 = time.time()
    for idx, sensor in enumerate(sensors, 1):
        sroot = os.path.join(daily_root, sensor)
        if not os.path.isdir(sroot):
            continue
        for feature in sorted(os.listdir(sroot)):
            fdir = os.path.join(sroot, feature)
            if not os.path.isdir(fdir):
                continue
            for fn in sorted(os.listdir(fdir)):
                if not fn.lower().endswith(".csv"):
                    continue
                date = fn[:-4]
                if start and date < start:
                    continue
                if end and date > end:
                    continue
                r = _summarize_one_day(os.path.join(fdir, fn), sensor,
                                       feature, date)
                if r:
                    rows.append(r)
        if idx % 50 == 0 or idx == len(sensors):
            print(f"  进度 {idx}/{len(sensors)}  "
                  f"(已汇总 {len(rows)} 行, 用时 {time.time()-t0:.0f}s)",
                  flush=True)

    if not rows:
        print("[警告] daily 目录下没有找到任何数据")
        return 0

    # 按 传感器(数值序)/特征/日期 排序
    rows.sort(key=lambda r: (int(r[0]) if r[0].isdigit() else r[0],
                             r[1], r[2]))

    os.makedirs(os.path.dirname(os.path.abspath(summary_path)), exist_ok=True)
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sensor", "feature", "date", "files",
                    "seconds", "missing_seconds", "min", "mean", "max"])
        w.writerows(rows)
    return len(rows)


def _summarize_one_day(path, sensor, feature, date):
    """
    把一个 daily CSV(24 行小时统计)汇总成 summary 的一行。
    口径与预处理保持一致: seconds=各小时count之和, mean=加权平均,
    min/max 取当日各小时最小/最大, files=有数据的小时数。
    """
    files = 0
    seconds = 0
    total = 0.0
    vmin = None
    vmax = None
    try:
        with open(path, "r", newline="", encoding="utf-8",
                  errors="replace") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 6:
                    continue
                try:
                    count = int(row[1])
                except ValueError:
                    continue
                if count <= 0:
                    continue
                files += 1
                seconds += count
                try:
                    total += float(row[5])   # sum 列
                    mn = float(row[3])       # min 列
                    mx = float(row[4])       # max 列
                except ValueError:
                    continue
                if vmin is None or mn < vmin:
                    vmin = mn
                if vmax is None or mx > vmax:
                    vmax = mx
    except OSError:
        return None
    if seconds <= 0:
        return None
    return [sensor, feature, date, files, seconds,
            max(0, 86400 - seconds),
            f"{vmin:.6g}", f"{total / seconds:.6g}", f"{vmax:.6g}"]


def main():
    ap = argparse.ArgumentParser(description="从 daily 目录重新汇总 summary.csv")
    ap.add_argument("--daily-root", default=DEFAULT_DAILY_ROOT,
                    help="daily 目录(默认 D:\\preprocess_sensor_data\\daily)")
    ap.add_argument("--summary", default=DEFAULT_SUMMARY,
                    help="summary.csv 输出路径")
    ap.add_argument("--limit-sensors", type=int, default=0,
                    help="只处理前 N 个传感器(试跑用)")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD(可选)")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD(可选)")
    args = ap.parse_args()

    n = rebuild_summary(args.daily_root, args.summary,
                        args.limit_sensors, args.start, args.end)
    if n < 0:
        print(f"[错误] daily 目录不存在: {args.daily_root}")
        sys.exit(1)
    print(f"[完成] 共汇总 {n} 行 -> {args.summary}")


if __name__ == "__main__":
    main()
