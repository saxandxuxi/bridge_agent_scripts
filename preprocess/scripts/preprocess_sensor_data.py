#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器端 TB 级传感器数据预处理脚本
==================================

目录结构（你描述的层级）:
    <DATA_ROOT>/<传感器编号>/<YYYY>/<MM>/<DD>/<传感器编号>_<YYYYMMDDHH>_<模块>(<特征>).csv

    例如:
    D:\信科采集软件解析数据\103\2026\03\03\103_2026030300_WSD(rh).csv
    D:\信科采集软件解析数据\103\2026\03\03\103_2026030301_WSD(temp).csv

每个 CSV 是"一个模块一个特征一个小时"的采样序列，形如:
    0000000,1233.8438
    0000100,1233.4688
    0000200,1233.4688
    ...
    第一列是文件内的相对时间计数器(默认毫秒，程序会按"一个文件=1 小时"
    自动校正单位)，第二列是测量值。

这个脚本做的事情（全部在服务器上执行，数据不下载）:
  1. 摸底: 扫描目录，统计每个传感器有哪些特征、日期范围、文件数和大小。
     支持按传感器编号和日期范围剪枝，只扫需要的目录（海量数据下大幅提速）;
  2. 预处理: 按天把一个特征的各小时文件**流式**聚合，
     一次只读一个小时文件、边读边归入小时桶，不把全天/全部文件
     一次性加载到内存，按小时聚合出该小时的完整统计量
     (count/mean/min/max/sum/std/median);
  3. 输出: 结果写到服务器上的结果目录，
     结构为 结果目录/daily/传感器编号/特征/日期.csv，
     每个 CSV 是某传感器某特征某天的 24 行小时统计，另加汇总表。

海量数据加速要点:
  - 摸底按 --sensors / --start / --end 剪枝，避免扫描全部传感器和年份;
  - 预处理按天拆分任务并行(多进程)，单个任务内存占用只与"一天"有关;
  - CSV 用二进制逐行解析(数字是 ASCII，省去 csv/strptime 开销)，
    计数器直接换算成小时桶下标，全程不构造 datetime 对象;
  - --median-mode none 可关闭精确中位数，进一步省内存。

用法:
    python3 preprocess_sensor_data.py                       # 只摸底(先跑这个,安全)
    python3 preprocess_sensor_data.py --mode all            # 摸底 + 全量预处理
    python3 preprocess_sensor_data.py --mode preprocess \
        --sensors 156 --features "GNSS(Ax),GNSS(Ay)" \
        --start 2026-01-01 --end 2026-01-31 \
        --workers 8
"""

import argparse
import csv
import datetime as dt
import logging
import math
import multiprocessing as mp
import os
import re
import time

import numpy as np

# ---------------- 需要按你的服务器修改的部分 ----------------
DATA_ROOT = r"D:\信科采集软件解析数据"     # 原始数据根目录(远程服务器 D 盘)
OUTPUT_ROOT = ""        # 留空 = 自动放在 DATA_ROOT 上一级的 results/ 目录
BUCKET_SECONDS = 3600   # 聚合粒度(秒)，默认 3600 = 1 小时，一天输出 24 行
WORKERS = 0             # 0 = 自动使用全部 CPU 核数
MEDIAN_MODE = "none"    # none=不计算中位数(默认,图库/统计值不使用中位数列)
RESUME = True           # True = 已生成过的 daily 日文件自动跳过(断点续跑，默认开)
SENSORS_PER_WORKER = 3  # 每个工作进程一次处理的传感器数量(按传感器分批)
DAILY_SUBDIR = "daily"  # daily 输出子目录（带期号时为 daily_2026.1~3）
# -----------------------------------------------------------

logger = logging.getLogger("bridge_preprocess")

# 交通荷载：周统计 HTML(xls) -> daily/交通荷载/车道N/日期.csv（与普通传感器同构）
TRAFFIC_SENSOR = "交通荷载"
TRAFFIC_DIR_RE = re.compile(r"(.+)_车道统计_(.+)$")
TRAFFIC_TIME_RE = re.compile(
    r"(\d{4})年(\d{2})月(\d{2})日\s+(\d{2}):(\d{2}):(\d{2})")


def _html_unescape(s):
    return (s.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").strip())


def parse_traffic_week(path):
    """解析每周车道统计 HTML(扩展名 .xls，实际为 UTF-8 HTML)。

    表头: 时间 | 总共 | 车道1 | 车道2 | 车道3 | 车道4
    返回 [(datetime, {车道1: n, ..., 车道4: n, 总共: n}), ...]
    """
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    rows = re.findall(r"<tr>(.*?)</tr>", text, re.S)
    out = []
    for r in rows:
        tds = [_html_unescape(re.sub(r"<[^>]+>", "", t))
               for t in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
        if len(tds) < 6 or not tds[0]:
            continue
        m = TRAFFIC_TIME_RE.search(tds[0])
        if not m:
            continue
        try:
            ts = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)), int(m.group(6)))
            # 表头: 时间 | 总共 | 车道1 | 车道2 | 车道3 | 车道4
            vals = {}
            for i, k in enumerate(("车道1", "车道2", "车道3", "车道4")):
                if i + 2 < len(tds) and tds[i + 2].strip():
                    vals[k] = int(float(tds[i + 2]))
            vals["总共"] = int(float(tds[1])) if tds[1].strip() else None
        except (ValueError, IndexError):
            continue
        if not vals or all(v is None for v in vals.values()):
            continue
        out.append((ts, vals))
    return out


def _find_traffic_dirs(traffic_root, bridge):
    """扫描交通荷载周数据目录：<桥名>_车道统计_<期>。bridge 留空时全部。"""
    if not traffic_root or not os.path.isdir(traffic_root):
        return []
    out = []
    for d in sorted(os.listdir(traffic_root)):
        full = os.path.join(traffic_root, d)
        if not os.path.isdir(full) or "车道统计" not in d:
            continue
        m = TRAFFIC_DIR_RE.match(d)
        if not m:
            continue
        if bridge and bridge not in m.group(1):
            continue
        out.append(full)
    return out


def _write_traffic_daily(out_path, date, hour_counts):
    """把某天 24 小时的车道计数写成与传感器一致的 daily CSV。
    hour_counts: {车道N: {小时下标: 计数}}；缺失小时写 count=0 空行。
    """
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    day_start = dt.datetime(date.year, date.month, date.day)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket_start", "count", "mean", "min", "max",
                    "sum", "std", "median"])
        for i in range(24):
            v = hour_counts.get(i)
            ts = (day_start + dt.timedelta(hours=i)).isoformat()
            if v is None or v < 0:
                w.writerow([ts, 0, "", "", "", "", "", ""])
            else:
                w.writerow([ts, 3600, f"{v:.6g}", f"{v:.6g}", f"{v:.6g}",
                            f"{v:.6g}", "0", f"{v:.6g}"])


def process_traffic(traffic_dirs, output_root, daily_subdir,
                    start="", end="", resume=False):
    """把周交通统计数据预处理成 daily/交通荷载/车道N/日期.csv。

    返回 (写入天数, 车道特征数, 跳过天数)。
    """
    start_d = dt.date.fromisoformat(start) if start else None
    end_d = dt.date.fromisoformat(end) if end else None
    lanes = []
    for tdir in traffic_dirs:
        for fn in sorted(os.listdir(tdir)):
            if not fn.lower().startswith("车道统计_") \
                    or not fn.lower().endswith(".xls"):
                continue
            rows = parse_traffic_week(os.path.join(tdir, fn))
            if not rows:
                continue
            by_date = {}
            for ts, vals in rows:
                d = ts.date()
                if start_d and d < start_d:
                    continue
                if end_d and d > end_d:
                    continue
                by_date.setdefault(d, {})[ts.hour] = vals
            for date, hour_map in sorted(by_date.items()):
                date_str = date.isoformat()
                per_lane = {}
                for ts_hour, vals in hour_map.items():
                    for k, v in vals.items():
                        per_lane.setdefault(k, {})[ts_hour] = v
                for lane in sorted(per_lane, key=lambda x: (x == "总共", x)):
                    if lane not in lanes:
                        lanes.append(lane)
                    out_path = os.path.join(
                        output_root, daily_subdir, TRAFFIC_SENSOR, lane,
                        date_str + ".csv")
                    if resume and os.path.exists(out_path) \
                            and os.path.getsize(out_path) > 0:
                        continue
                    _write_traffic_daily(out_path, date, per_lane[lane])
    return len(traffic_dirs), len(lanes), 0


def period_tag(start="", end=""):
    """由起止日期生成年月范围标签，与图库/统计值目录一致。
    例如 2026-01-01~2026-03-31 -> 2026.1~3；单月 -> 2026.07。"""
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


def bucket_seconds_for(feature):
    """特征专用聚合粒度：
      - 风速 FSFX2(spfs)/FSFX2(szfs) -> 600 秒（10 分钟一个均值）
      - 振动 DZJSD(xJsd)/yJsd/zJsd  -> 1 秒（保留全天秒级全量数据）
      - 其余特征 -> BUCKET_SECONDS（默认 1 小时）
    """
    m = re.search(r"\(([^)]+)\)$", feature or "")
    inner = (m.group(1) if m else (feature or "")).lower()
    if inner in ("spfs", "szfs"):
        return 600
    if inner.endswith("jsd"):
        return 1
    return BUCKET_SECONDS


# 真实文件名识别: 传感器编号_YYYYMMDDHH_模块(特征).csv
# 例如 103_2026030300_WSD(rh).csv -> stamp=2026030300, name=WSD, axis=rh
FILE_NAME_RE = re.compile(
    r"^(?P<fsensor>\d+)_(?P<stamp>\d{10})_"
    r"(?P<name>[^(]+?)\s*\((?P<axis>[^)]+)\)\.csv$",
    re.IGNORECASE,
)

# 常见时间戳格式（会自动适配并记住成功的格式）
TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y%m%d%H%M%S",
    "%Y-%m-%d %H:%M",
)
_LAST_OK_FORMAT = [TS_FORMATS[0]]

# CSV 列名自动识别（不区分大小写）
TS_NAMES = {"timestamp", "time", "datetime", "date", "ts", "时间", "采集时间"}
VAL_NAMES = {"value", "val", "v", "measurement", "measured", "data",
             "reading", "signal", "值", "测量值", "读数"}


def setup_logging(log_path):
    """初始化日志：同时输出到控制台和文件(带时间戳)，文件为追加模式。"""
    global logger
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False


def resolve_output_root():
    """结果目录：显式指定就用指定的，否则放数据根目录的上一级。"""
    if OUTPUT_ROOT:
        return OUTPUT_ROOT
    parent = os.path.dirname(DATA_ROOT.rstrip("/\\"))
    return os.path.join(parent, "results")


def parse_ts(text):
    """解析时间戳字符串，返回 datetime 或 None。"""
    s = text.strip()
    if not s:
        return None
    # 先试上次成功的格式（同一个传感器的文件格式一般一致）
    for fmt in (_LAST_OK_FORMAT[0],) + tuple(
        f for f in TS_FORMATS if f != _LAST_OK_FORMAT[0]
    ):
        try:
            t = dt.datetime.strptime(s, fmt)
            _LAST_OK_FORMAT[0] = fmt
            return t
        except ValueError:
            continue
    # 退路：Unix 时间戳（秒/毫秒）
    try:
        n = float(s)
        return dt.datetime.utcfromtimestamp(n / 1000 if n > 1e12 else n)
    except ValueError:
        return None


def detect_columns(first_row):
    """从第一行判断表头和时间/数值列的位置。"""
    lowered = [c.strip().lower() for c in first_row]
    ts_idx = val_idx = None
    for i, name in enumerate(lowered):
        if ts_idx is None and name in TS_NAMES:
            ts_idx = i
        if val_idx is None and name in VAL_NAMES:
            val_idx = i
    if ts_idx is None and val_idx is None:
        return 0, 1, False  # 没有表头，按"第1列时间、第2列数值"处理
    return (ts_idx if ts_idx is not None else 0,
            val_idx if val_idx is not None else 1,
            True)


def detect_counter_unit(counters):
    """
    根据计数器跨度反推单位。

    每个文件固定是 1 小时(3600 秒)的数据，若计数器为毫秒(10Hz 采样，
    每行 +100)，跨度约为 3600000；若为百分之一秒(1Hz，每行 +100)，
    跨度约为 360000。按"跨度 * 单位 ≈ 3600 秒"选出最可能的单位，
    识别不出时默认按毫秒处理。
    """
    if not counters:
        return 0.001
    vals = sorted(set(counters))
    deltas = [b - a for a, b in zip(vals, vals[1:])]
    step = min(deltas) if deltas else 0
    span = vals[-1] - vals[0] + step
    if span <= 0:
        return 0.001
    per_sec = span / 3600.0  # 每秒的计数器单位数
    for unit in (0.001, 0.01, 0.1, 1.0):
        expected = 1.0 / unit
        if abs(per_sec - expected) / expected < 0.05:
            return unit
    return 0.001


def _looks_like_header(row):
    """判断一行是否像表头（与已知时间/数值列名匹配）。"""
    lowered = [c.strip().lower() for c in row if c.strip()]
    return bool(lowered) and any(c in TS_NAMES or c in VAL_NAMES
                                 for c in lowered)


def _read_hour_file_fast(path):
    """
    快速解析一个"一个小时"的 CSV（向量化，pandas C 引擎优先）。
    按真实格式 计数器,数值 解析，返回 (counters, values) 为 numpy 数组；
    文件为空 / 打不开 / 解析不出数值时返回 None。
    """
    # 1) 向量化路径（快 5~15 倍）
    try:
        import pandas as pd
        df = pd.read_csv(
            path, header=None, usecols=[0, 1], engine="c",
            dtype=float, skip_blank_lines=True,
            on_bad_lines="skip", comment=None,
        )
        if df is None or df.empty:
            return None
        counters = df[0].to_numpy(dtype=np.float64)
        values = df[1].to_numpy(dtype=np.float64)
    except Exception:  # noqa: BLE001
        counters = None
    if counters is None:
        # 2) 回退：逐行二进制解析（带表头/脏行时使用）
        counters, values = [], []
        try:
            with open(path, "rb", buffering=1 << 20) as f:
                for line in f:
                    if line.startswith(b"\xef\xbb\xbf"):  # 去 UTF-8 BOM
                        line = line[3:]
                    line = line.strip()
                    if not line:
                        continue
                    p = line.split(b",")
                    if len(p) < 2:
                        continue
                    try:
                        c = int(p[0])
                        v = float(p[1])
                    except ValueError:
                        continue
                    counters.append(c)
                    values.append(v)
        except OSError:
            return None
        if not counters:
            return None
        return (np.array(counters, dtype=np.int64),
                np.array(values, dtype=np.float64))

    # pandas 路径：过滤非有限值，计数器整数化
    ok = np.isfinite(values) & np.isfinite(counters)
    counters = counters[ok]
    values = values[ok]
    if counters.size == 0:
        return None
    counters = np.round(counters).astype(np.int64)
    return counters, values


def _read_hour_file_text(path):
    """
    回退解析: 通用"时间戳,数值"文本格式（兼容其他来源的 CSV）。
    返回 (rows, errors)，rows 为 [(datetime, value)]。
    """
    raw = []
    try:
        with open(path, "r", newline="", encoding="utf-8",
                  errors="replace") as f:
            reader = csv.reader(f)
            first = next(reader, None)
            if first is None:
                return [], []
            if not _looks_like_header(first):
                raw.append(first)
            for row in reader:
                raw.append(row)
    except Exception as exc:
        return [], [f"{os.path.basename(path)}: {exc}"]

    if not raw:
        return [], []
    ts_idx, val_idx, has_header = detect_columns(raw[0])
    start = 1 if has_header else 0
    rows = []
    for row in raw[start:]:
        if not row or not any(c.strip() for c in row):
            continue
        t = parse_ts(row[ts_idx]) if len(row) > ts_idx else None
        v = None
        if len(row) > val_idx:
            try:
                v = float(row[val_idx].strip())
            except (ValueError, TypeError):
                v = None
        if t is not None and v is not None:
            rows.append((t, v))
    return rows, []


def load_feature_day(sensor, date, feature):
    """
    调试/兼容用：读取某传感器某天某个特征的所有小时文件，
    把整天数据一次性拼成列表（内存占用较大，正式处理请走
    aggregate_feature_day 的流式路径）。
    返回 (rows, files_loaded, errors)；
    rows 为 [(datetime, value), ...]，已按时间排序。
    """
    y, m, d = date.year, date.month, date.day
    day_dir = os.path.join(DATA_ROOT, sensor, f"{y:04d}", f"{m:02d}", f"{d:02d}")
    if not os.path.isdir(day_dir):
        return [], 0, ["day dir missing"]

    rows, files_loaded, errors = [], 0, []
    for hh in range(24):
        stamp = f"{y:04d}{m:02d}{d:02d}{hh:02d}"
        path = os.path.join(day_dir, f"{sensor}_{stamp}_{feature}.csv")
        if not os.path.exists(path):
            continue
        hour_start = dt.datetime(y, m, d, hh)
        fast = _read_hour_file_fast(path)
        if fast is not None:
            counters, values = fast
            unit = detect_counter_unit(counters)
            c0 = counters[0]
            rows.extend(
                (hour_start + dt.timedelta(seconds=(c - c0) * unit), v)
                for c, v in zip(counters, values))
            files_loaded += 1
            continue
        file_rows, file_errors = _read_hour_file_text(path)
        rows.extend(file_rows)
        errors.extend(file_errors)
        if not file_errors:
            files_loaded += 1

    rows.sort(key=lambda r: r[0])
    return rows, files_loaded, errors


def aggregate_feature_day(sensor, date, feature):
    """
    流式聚合某传感器某天某个特征的所有小时文件（正式处理路径，向量化）。

    每个小时文件用 numpy 批量分桶（count/sum/sumsq/min/max），
    不把全天数据攒在内存；中位数列默认不计算（MEDIAN_MODE="none"，
    图库/统计值不使用该列）。计数器单位同一天内只探测一次。

    返回 (out_rows, files_loaded, total_samples, errors)
    """
    y, m, d = date.year, date.month, date.day
    day_dir = os.path.join(DATA_ROOT, sensor,
                           f"{y:04d}", f"{m:02d}", f"{d:02d}")
    day_start = dt.datetime(y, m, d)
    bucket = bucket_seconds_for(feature)
    n_buckets = -(-86400 // bucket)  # 向上取整，兼容不能整除的粒度
    counts = np.zeros(n_buckets, dtype=np.int64)
    sums = np.zeros(n_buckets, dtype=np.float64)
    sq_sums = np.zeros(n_buckets, dtype=np.float64)
    mins = np.full(n_buckets, np.inf)
    maxs = np.full(n_buckets, -np.inf)

    files_loaded, errors, total = 0, [], 0
    unit_cache = None
    if not os.path.isdir(day_dir):
        errors = ["day dir missing"]
    else:
        for hh in range(24):
            stamp = f"{y:04d}{m:02d}{d:02d}{hh:02d}"
            path = os.path.join(day_dir, f"{sensor}_{stamp}_{feature}.csv")
            if not os.path.exists(path):
                continue
            fast = _read_hour_file_fast(path)
            if fast is not None:
                counters, vals = fast
                if unit_cache is None:
                    unit_cache = detect_counter_unit(counters.tolist())
                unit = unit_cache
                c0 = counters[0]
                base = hh * 3600.0
                idx = ((base + (counters - c0) * unit) // bucket).astype(np.int64)
                mask = (idx >= 0) & (idx < n_buckets) & np.isfinite(vals)
                ii = idx[mask]
                vv = vals[mask]
                n = int(ii.size)
                if n:
                    np.add.at(counts, ii, 1)
                    np.add.at(sums, ii, vv)
                    np.add.at(sq_sums, ii, vv * vv)
                    np.minimum.at(mins, ii, vv)
                    np.maximum.at(maxs, ii, vv)
                    total += n
                files_loaded += 1
                continue
            # 回退: 通用时间戳文本格式
            file_rows, file_errors = _read_hour_file_text(path)
            errors.extend(file_errors)
            if not file_errors:
                files_loaded += 1
            for t, v in file_rows:
                idx = int((t - day_start).total_seconds()) // bucket
                if 0 <= idx < n_buckets:
                    counts[idx] += 1
                    sums[idx] += v
                    sq_sums[idx] += v * v
                    total += 1
                    if v < mins[idx]:
                        mins[idx] = v
                    if v > maxs[idx]:
                        maxs[idx] = v

    out = []
    for i in range(n_buckets):
        ts = day_start + dt.timedelta(seconds=i * bucket)
        n = int(counts[i])
        if n:
            mean = sums[i] / n
            var = max(0.0, sq_sums[i] / n - mean * mean)
            std = math.sqrt(var)
            vmin = None if np.isinf(mins[i]) else float(mins[i])
            vmax = None if np.isinf(maxs[i]) else float(maxs[i])
            median = None
            out.append((ts, n, mean, vmin, vmax, sums[i], std, median))
        else:
            out.append((ts, 0, None, None, None, None, None, None))
    return out, files_loaded, total, errors


def aggregate_day(rows, date, bucket_seconds):
    """
    把一天内的时间序列按 bucket_seconds 分桶聚合，
    返回完整时间轴（含空桶）的
    (bucket_start, count, mean, min, max, sum, std, median)。
    """
    day_start = dt.datetime(date.year, date.month, date.day)
    n_buckets = -(-86400 // bucket_seconds)  # 向上取整，兼容不能整除的粒度
    counts = [0] * n_buckets
    sums = [0.0] * n_buckets
    sq_sums = [0.0] * n_buckets
    mins = [None] * n_buckets
    maxs = [None] * n_buckets
    values = [[] for _ in range(n_buckets)]

    for t, v in rows:
        idx = int((t - day_start).total_seconds()) // bucket_seconds
        if 0 <= idx < n_buckets:
            counts[idx] += 1
            sums[idx] += v
            sq_sums[idx] += v * v
            values[idx].append(v)
            if mins[idx] is None or v < mins[idx]:
                mins[idx] = v
            if maxs[idx] is None or v > maxs[idx]:
                maxs[idx] = v

    out = []
    for i in range(n_buckets):
        ts = day_start + dt.timedelta(seconds=i * bucket_seconds)
        if counts[i]:
            n = counts[i]
            mean = sums[i] / n
            var = max(0.0, sq_sums[i] / n - mean * mean)
            std = math.sqrt(var)
            values[i].sort()
            if n % 2:
                median = values[i][n // 2]
            else:
                median = (values[i][n // 2 - 1] + values[i][n // 2]) / 2.0
            out.append((ts, n, mean, mins[i], maxs[i], sums[i], std, median))
        else:
            out.append((ts, 0, None, None, None, None, None, None))
    return out


def process_task(task):
    """单个任务：某传感器某天某特征 -> 写输出文件，返回汇总信息。"""
    sensor, date_str, feature = task
    y, m, d = map(int, date_str.split("-"))
    date = dt.date(y, m, d)
    out_path = os.path.join(OUTPUT_ROOT, DAILY_SUBDIR, sensor, feature,
                            date_str + ".csv")
    # 断点续跑: 输出文件已存在且非空则跳过
    if RESUME and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        meta = meta_from_daily_csv(sensor, feature, date_str, out_path)
        meta["skipped"] = True
        return meta, None
    try:
        out_rows, files_loaded, total, errors = \
            aggregate_feature_day(sensor, date, feature)

        out_dir = os.path.join(OUTPUT_ROOT, DAILY_SUBDIR, sensor, feature)
        os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["bucket_start", "count", "mean",
                        "min", "max", "sum", "std", "median"])
            for r in out_rows:
                w.writerow([
                    r[0].isoformat(),
                    r[1],
                    "" if r[2] is None else f"{r[2]:.6g}",
                    "" if r[3] is None else f"{r[3]:.6g}",
                    "" if r[4] is None else f"{r[4]:.6g}",
                    "" if r[5] is None else f"{r[5]:.6g}",
                    "" if r[6] is None else f"{r[6]:.6g}",
                    "" if r[7] is None else f"{r[7]:.6g}",
                ])

        present = [r for r in out_rows if r[1] > 0]
        meta = {
            "sensor": sensor,
            "feature": feature,
            "date": date_str,
            "files": files_loaded,
            "seconds": total,
            "missing_seconds": 86400 - total,
            "min": min(r[3] for r in present) if present else None,
            "mean": (sum(r[5] for r in present) / total) if total else None,
            "max": max(r[4] for r in present) if present else None,
            "errors": ";".join(errors),
        }
        return meta, None
    except Exception as exc:
        return None, f"{sensor}/{date_str}/{feature}: {exc}"


def meta_from_daily_csv(sensor, feature, date_str, path):
    """
    断点续跑时，从已生成的 daily CSV 反推 summary 需要的统计值，
    保证跳过的任务记录也不会从汇总表丢失。
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
        pass
    return {
        "sensor": sensor,
        "feature": feature,
        "date": date_str,
        "files": files,
        "seconds": seconds,
        "missing_seconds": max(0, 86400 - seconds),
        "min": vmin,
        "mean": (total / seconds) if seconds else None,
        "max": vmax,
    }


def process_batch(batch):
    """
    处理一批任务(同一批内的任务属于相邻的若干个传感器)，
    依次调用 process_task，返回 [(meta, err)]。
    按传感器分批可显著减少进程池的调度与序列化开销。
    """
    results = []
    for task in batch:
        results.append(process_task(task))
    return results


def init_worker(data_root, output_root, bucket_seconds, median_mode, resume,
                daily_subdir):
    """把运行参数传给每个工作进程(Windows 多进程会重新导入模块)。"""
    global DATA_ROOT, OUTPUT_ROOT, BUCKET_SECONDS, MEDIAN_MODE, RESUME
    global DAILY_SUBDIR
    DATA_ROOT = data_root
    OUTPUT_ROOT = output_root
    BUCKET_SECONDS = bucket_seconds
    MEDIAN_MODE = median_mode
    RESUME = resume
    DAILY_SUBDIR = daily_subdir


def discover(sensors=None, start=None, end=None):
    """
    扫描数据根目录。

    sensors: 逗号分隔的传感器编号，None=全部；
    start/end: YYYY-MM-DD，None=不限。
    给定筛选条件时只下钻对应的传感器和年月日目录，
    海量数据下避免扫描无关目录。文件大小用 os.scandir 一次拿到，
    不额外 stat。

    返回 info: {sensor: {feature: {"files": n, "bytes": b, "days": set}}}
    """
    info = {}
    if not os.path.isdir(DATA_ROOT):
        logger.error(f"数据根目录不存在: {DATA_ROOT}")
        return info
    sensor_set = set(sensors.split(",")) if sensors else None
    start_d = dt.date.fromisoformat(start) if start else None
    end_d = dt.date.fromisoformat(end) if end else None

    for sensor in sorted(os.listdir(DATA_ROOT)):
        if sensor_set and sensor not in sensor_set:
            continue
        sroot = os.path.join(DATA_ROOT, sensor)
        if not os.path.isdir(sroot):
            continue
        for y in sorted(os.listdir(sroot)):
            if not y.isdigit():
                continue
            yi = int(y)
            if start_d and yi < start_d.year:
                continue
            if end_d and yi > end_d.year:
                continue
            yp = os.path.join(sroot, y)
            if not os.path.isdir(yp):
                continue
            for mo in sorted(os.listdir(yp)):
                if not mo.isdigit():
                    continue
                mi = int(mo)
                if start_d and yi == start_d.year and mi < start_d.month:
                    continue
                if end_d and yi == end_d.year and mi > end_d.month:
                    continue
                mop = os.path.join(yp, mo)
                if not os.path.isdir(mop):
                    continue
                for dd in sorted(os.listdir(mop)):
                    if not dd.isdigit():
                        continue
                    di = int(dd)
                    date_obj = dt.date(yi, mi, di)
                    if start_d and date_obj < start_d:
                        continue
                    if end_d and date_obj > end_d:
                        continue
                    dp = os.path.join(mop, dd)
                    if not os.path.isdir(dp):
                        continue
                    date = f"{yi:04d}-{mi:02d}-{di:02d}"
                    with os.scandir(dp) as it:
                        for ent in it:
                            if not ent.name.lower().endswith(".csv"):
                                continue
                            m = FILE_NAME_RE.match(ent.name)
                            if not m:
                                logger.warning(
                                    f"无法识别文件名"
                                    f"(应为 传感器编号_YYYYMMDDHH_模块(特征).csv): "
                                    f"{sensor}/{date}/{ent.name}")
                                continue
                            feature = (m.group("name").strip()
                                       + "(" + m.group("axis").strip() + ")")
                            feat = info.setdefault(sensor, {}).setdefault(
                                feature, {"files": 0, "bytes": 0, "days": set()})
                            feat["files"] += 1
                            try:
                                feat["bytes"] += ent.stat().st_size
                            except OSError:
                                pass
                            feat["days"].add(date)
    return info


def print_inventory(info):
    lines = ["=" * 70, "摸底结果", "=" * 70]
    total_files = total_bytes = 0
    for sensor, feats in sorted(info.items()):
        lines.append(f"\n传感器 {sensor}: {len(feats)} 个特征")
        for feature, st in sorted(feats.items()):
            days = sorted(st["days"])
            total_files += st["files"]
            total_bytes += st["bytes"]
            lines.append(f"  {feature:<24} 文件 {st['files']:>7}  "
                         f"大小 {st['bytes']/2**30:>8.3f} GB  "
                         f"天数 {len(days):>5}  范围 {days[0]} ~ {days[-1]}")
    lines.append("-" * 70)
    lines.append(f"总计: {total_files} 个文件, {total_bytes/2**30:.3f} GB")
    logger.info("\n".join(lines))


def show_sample(info, n_features=2):
    """每个传感器挑前几个特征，打印一个文件的表头和数据样例，用于核对解析。"""
    lines = ["文件样例(核对表头/列名):"]
    shown = 0
    for sensor, feats in sorted(info.items()):
        if shown >= n_features:
            break
        feature = sorted(feats)[0]
        day = sorted(feats[feature]["days"])[0]
        y, m, d = day.split("-")
        path = os.path.join(DATA_ROOT, sensor, y, m, d,
                            f"{sensor}_{y}{m}{d}00_{feature}.csv")
        if not os.path.exists(path):
            continue
        lines.append(f"  [{sensor}/{feature}] {path}")
        with open(path, "r", newline="", encoding="utf-8",
                  errors="replace") as f:
            for i, line in zip(range(4), f):
                lines.append("    " + line.rstrip("\n"))
        shown += 1
    if len(lines) > 1:
        logger.info("\n".join(lines))


def write_inventory(info):
    rows = []
    for sensor, feats in sorted(info.items()):
        for feature, st in sorted(feats.items()):
            days = sorted(st["days"])
            rows.append([sensor, feature, st["files"], st["bytes"],
                         len(days), days[0], days[-1], "|".join(days)])
    out_path = os.path.join(OUTPUT_ROOT, "inventory.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sensor", "feature", "files", "bytes", "days",
                    "first_day", "last_day", "dates"])
        w.writerows(rows)
    logger.info(f"摸底表已写入: {out_path}")


def load_inventory(path):
    """读取摸底表(inventory.csv)为 info 结构，用于跳过摸底扫描直接预处理。

    要求新版格式含 dates 列（实际日期列表，| 分隔）；旧格式或缺列返回 None。
    """
    if not os.path.isfile(path):
        return None
    info = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or "dates" not in header:
                return None
            idx = {name: i for i, name in enumerate(header)}
            for row in reader:
                if len(row) <= idx["sensor"]:
                    continue
                sensor, feature = row[idx["sensor"]], row[idx["feature"]]
                try:
                    files = int(row[idx["files"]])
                except (IndexError, ValueError):
                    continue
                dates = {x for x in row[idx["dates"]].split("|") if x}
                info.setdefault(sensor, {})[feature] = {
                    "files": files,
                    "bytes": 0,
                    "days": dates,
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取摸底表失败 {path}: {exc}")
        return None
    return info or None


def _inventory_covers(inventory, start, end):
    """复用摸底表的严格判定：请求时间段必须完整落在表内。

    规则（全部满足才允许复用，否则重新摸底）：
      - 表不能为空；
      - 请求的起止日期都必须真实出现在表中；
      - 请求时间段内每一天在表中出现的覆盖率 >= 90%。
    避免旧摸底表(如只含 2026)被复用到 2025，导致任务数为 0、
    “管道全部完成”却一个文件都不生成。
    """
    if not inventory:
        return False
    all_days = set()
    for feats in (inventory or {}).values():
        for st in feats.values():
            all_days.update(st.get("days") or ())
    if not all_days:
        return False
    try:
        s = dt.date.fromisoformat(start) if start else None
        e = dt.date.fromisoformat(end) if end else None
    except ValueError:
        return False
    if s and s.isoformat() not in all_days:
        return False
    if e and e.isoformat() not in all_days:
        return False
    if s and e:
        total = (e - s).days + 1
        if total <= 0:
            return False
        present = 0
        day = s
        while day <= e:
            if day.isoformat() in all_days:
                present += 1
            day += dt.timedelta(days=1)
        if present / total < 0.9:
            return False
    return True


def build_tasks(info, sensors, features, start, end, limit_days):
    tasks = []
    start_d = dt.date.fromisoformat(start) if start else None
    end_d = dt.date.fromisoformat(end) if end else None
    sensor_set = set(sensors.split(",")) if sensors else None
    feature_set = set(features.split(",")) if features else None

    for sensor in sorted(info):
        if sensor_set and sensor not in sensor_set:
            continue
        for feature in sorted(info[sensor]):
            if feature_set and feature not in feature_set:
                continue
            for day in sorted(info[sensor][feature]["days"]):
                d = dt.date.fromisoformat(day)
                if start_d and d < start_d:
                    continue
                if end_d and d > end_d:
                    continue
                tasks.append((sensor, day, feature))
    if limit_days:
        # 每个传感器每个特征只保留前 N 天，便于小范围试跑
        seen = {}
        kept = []
        for t in tasks:
            key = (t[0], t[2])
            if seen.get(key, 0) >= limit_days:
                continue
            seen[key] = seen.get(key, 0) + 1
            kept.append(t)
        tasks = kept
    return tasks


def build_batches(tasks, sensors_per_worker):
    """
    把任务按传感器分批：每批包含最多 sensors_per_worker 个传感器的
    全部任务。任务列表本身按传感器有序，所以相邻传感器自然聚在同一批。
    分批后进程池只需调度几百批，而不是几十万个单任务。
    """
    batches, current, cur_sensors = [], [], set()
    for t in tasks:
        if (current and t[0] not in cur_sensors
                and len(cur_sensors) >= sensors_per_worker):
            batches.append(current)
            current, cur_sensors = [], set()
        current.append(t)
        cur_sensors.add(t[0])
    if current:
        batches.append(current)
    return batches


def run_preprocess(tasks):
    if not tasks:
        logger.info("没有符合条件的任务。")
        return 0
    workers = WORKERS if WORKERS > 0 else max(1, (os.cpu_count() or 1) - 1)
    n_total = len(tasks)
    spw = max(1, SENSORS_PER_WORKER)
    batches = build_batches(tasks, spw)
    logger.info(f"开始预处理: {n_total} 个任务, {len(batches)} 批"
                f"(每批最多 {spw} 个传感器), {workers} 个进程, "
                f"粒度 {BUCKET_SECONDS} 秒, 断点续跑={'开' if RESUME else '关'}")
    t0 = time.time()
    errors = []
    skipped = 0
    done = 0
    # 每个传感器的任务数，用于"传感器完成"粒度的进度日志
    sensor_tasks = {}
    for t in tasks:
        sensor_tasks[t[0]] = sensor_tasks.get(t[0], 0) + 1
    sensor_done = {}
    sensor_t0 = {}

    # 汇总表流式写入：结果随任务完成逐行落盘，不在内存里攒全部结果；
    # 断点续跑时追加写入，保留上次已完成的记录
    summary_path = os.path.join(OUTPUT_ROOT, "summary.csv")
    summary_exists = RESUME and os.path.exists(summary_path)
    sf = open(summary_path, "a" if summary_exists else "w",
              newline="", encoding="utf-8")
    sw = csv.writer(sf)
    if not summary_exists:
        sw.writerow(["sensor", "feature", "date", "files",
                     "seconds", "missing_seconds", "min", "mean", "max"])
        sf.flush()
    written = 0
    chunksize = max(1, min(50, len(tasks) // (workers * 10) + 1))
    try:
        with mp.Pool(workers, initializer=init_worker,
                     initargs=(DATA_ROOT, OUTPUT_ROOT, BUCKET_SECONDS,
                               MEDIAN_MODE, RESUME, DAILY_SUBDIR)) as pool:
            for results in pool.imap_unordered(process_batch, batches,
                                               chunksize=1):
                for meta, err in results:
                    done += 1
                    # 任务失败时 process_task 返回 (None, 错误信息)，
                    # 记录后继续，不让整个运行崩溃
                    if meta is None:
                        errors.append(err or "未知错误")
                        logger.error(f"任务失败: {err}")
                        continue
                    sensor = meta.get("sensor") if isinstance(meta, dict) else None
                    if sensor:
                        sensor_done[sensor] = sensor_done.get(sensor, 0) + 1
                        if sensor_done[sensor] == 1:
                            sensor_t0[sensor] = time.time()
                        if sensor_done[sensor] >= sensor_tasks.get(sensor, 0):
                            logger.info(
                                f"传感器 {sensor} 完成 "
                                f"({sensor_done[sensor]}/{sensor_tasks[sensor]} 任务, "
                                f"用时 {time.time()-sensor_t0.get(sensor, t0):.0f}s)")
                    if meta.get("skipped"):
                        skipped += 1
                    if err:
                        errors.append(err)
                        logger.error(f"任务失败: {err}")
                    else:
                        # 正常任务和断点续跑跳过的任务都会写入汇总表
                        sw.writerow([meta["sensor"], meta["feature"], meta["date"],
                                     meta["files"], meta["seconds"],
                                     meta["missing_seconds"],
                                     "" if meta["min"] is None else f"{meta['min']:.6g}",
                                     "" if meta["mean"] is None else f"{meta['mean']:.6g}",
                                     "" if meta["max"] is None else f"{meta['max']:.6g}"])
                        written += 1
                        if written % 100 == 0:
                            sf.flush()
                    step = max(200, n_total // 200)
                    if done % step == 0 or done == n_total:
                        el = time.time() - t0
                        eta = el / done * (n_total - done) if done else 0
                        logger.info(
                            f"进度 {done}/{n_total} ({done/n_total*100:.2f}%), "
                            f"跳过 {skipped}, 出错 {len(errors)}, "
                            f"已用 {el:.0f}s, 预计剩余 {eta:.0f}s")
    finally:
        sf.close()

    # 汇总表按 传感器/特征/日期 排序并去重(流式写入+多轮续跑会产生乱序/重复)
    try:
        with open(summary_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            seen = {}
            for row in reader:
                if len(row) >= 9:
                    seen[(row[0], row[1], row[2])] = row
        rows = sorted(seen.values(),
                      key=lambda r: (int(r[0]) if r[0].isdigit() else r[0],
                                     r[1], r[2]))
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header or ["sensor", "feature", "date", "files",
                                  "seconds", "missing_seconds",
                                  "min", "mean", "max"])
            w.writerows(rows)
        logger.info(f"汇总表已按 传感器/特征/日期 排序去重: {len(rows)} 行")
    except OSError as exc:
        logger.error(f"汇总表排序失败: {exc}")

    logger.info(f"汇总表已写入: {summary_path} (跳过 {skipped} 个已完成任务)")
    logger.info(f"总耗时: {time.time() - t0:.0f} 秒")

    if errors:
        err_path = os.path.join(OUTPUT_ROOT, "errors.log")
        with open(err_path, "w", encoding="utf-8") as f:
            f.write("\n".join(errors))
        logger.error(f"共 {len(errors)} 条错误, 详见: {err_path}")
    return done - skipped


def main():
    global DATA_ROOT, OUTPUT_ROOT, BUCKET_SECONDS, WORKERS
    global MEDIAN_MODE, RESUME, SENSORS_PER_WORKER
    ap = argparse.ArgumentParser(
        description="TB 级传感器数据摸底 + 预处理(在服务器上运行)")
    ap.add_argument("--mode", choices=["inventory", "preprocess", "all"],
                    default="inventory",
                    help="inventory=只摸底; preprocess=只处理; all=先摸底再处理")
    ap.add_argument("--data-root", default=DATA_ROOT, help="原始数据根目录")
    ap.add_argument("--output-root", default=OUTPUT_ROOT, help="结果目录")
    ap.add_argument("--bridge", default="",
                    help="大桥名称(如 赤石)；结果写入 <output-root>/<桥名>/ 下")
    ap.add_argument("--sensors", default="", help="逗号分隔的传感器编号，留空=全部")
    ap.add_argument("--sensors-file", default="",
                    help="传感器编号列表文件(每行一个或逗号分隔)，与 --sensors 合并")
    ap.add_argument("--features", default="",
                    help="逗号分隔的特征名(如 GNSS(Ax))，留空=全部")
    ap.add_argument("--start", default="", help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--bucket", type=int, default=BUCKET_SECONDS,
                    help="聚合粒度秒数，默认 3600(1小时,每天24行)")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help="并行进程数，默认自动")
    ap.add_argument("--sensors-per-worker", type=int,
                    default=SENSORS_PER_WORKER,
                    help="每个工作进程一次处理的传感器数量(按传感器分批，默认3)")
    ap.add_argument("--median-mode", choices=["exact", "none"],
                    default=MEDIAN_MODE,
                    help="exact=精确中位数(默认); none=不计算中位数(最省内存)")
    ap.add_argument("--resume", action="store_true", default=RESUME,
                    help="跳过已生成过的 daily 日文件(断点续跑，默认开启)")
    ap.add_argument("--no-resume", action="store_false", dest="resume",
                    help="关闭断点续跑(重新处理全部)")
    ap.add_argument("--limit-days", type=int, default=0,
                    help="每个传感器每个特征只处理前 N 天(试跑用)")
    ap.add_argument("--period-tag", default="",
                    help="daily 目录的年月标签(如 2026.1~3)；留空按 --start/--end 自动推导")
    ap.add_argument("--traffic-root", default="",
                    help="交通荷载周统计数据根目录(含 <桥名>_车道统计_<期> 子目录)；"
                         "留空自动找 <cwd>/inputs。交通数据按周存，与传感器编号"
                         "数据分开处理，输出到 daily/交通荷载/车道N/")
    ap.add_argument("--traffic-only", action="store_true",
                    help="只处理交通荷载周统计数据，不扫描/预处理传感器原始数据")
    args = ap.parse_args()
    if args.traffic_only:
        args.mode = "preprocess"

    DATA_ROOT = args.data_root
    OUTPUT_ROOT = args.output_root or resolve_output_root()
    if args.bridge:
        OUTPUT_ROOT = os.path.join(OUTPUT_ROOT, args.bridge)
    BUCKET_SECONDS = args.bucket
    WORKERS = args.workers
    MEDIAN_MODE = args.median_mode
    RESUME = args.resume
    SENSORS_PER_WORKER = max(1, args.sensors_per_worker)
    global DAILY_SUBDIR
    tag = args.period_tag or period_tag(args.start, args.end)
    DAILY_SUBDIR = f"daily_{tag}" if tag else "daily"
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    setup_logging(os.path.join(OUTPUT_ROOT, "preprocess.log"))
    logger.info("=" * 28 + " 新一次运行开始 " + "=" * 28)
    logger.info(f"数据根目录: {DATA_ROOT}")
    logger.info(f"结果目录: {OUTPUT_ROOT}")
    logger.info(f"daily 输出目录: {os.path.join(OUTPUT_ROOT, DAILY_SUBDIR)}")

    # 合并 --sensors 与 --sensors-file 中的编号
    sensor_ids = set()
    if args.sensors:
        sensor_ids |= {s.strip() for s in args.sensors.split(",") if s.strip()}
    if args.sensors_file:
        # utf-8-sig: 兼容带 BOM 的文件(如 PowerShell/记事本另存的 UTF-8)
        with open(args.sensors_file, encoding="utf-8-sig") as f:
            for line in f:
                for s in line.replace(",", "\n").splitlines():
                    s = s.strip()
                    if s:
                        sensor_ids.add(s)
    sensors_arg = ",".join(sorted(sensor_ids))

    if not args.traffic_only:
        scan_scope = (sensors_arg or "全部") + " / " + \
            (args.start or "最早") + " ~ " + (args.end or "最新")
        logger.info(f"摸底范围: 传感器 {scan_scope}")
        info = None
        inv_path = os.path.join(OUTPUT_ROOT, "inventory.csv")
        reused_inventory = False
        if args.mode != "inventory":
            # 已有本桥摸底表则直接复用，跳过耗时扫描（--mode inventory 才会重扫）
            info = load_inventory(inv_path)
            if info is not None and not _inventory_covers(
                    info, args.start, args.end):
                logger.warning("摸底表未覆盖请求时间段(%s ~ %s)，"
                               "重新摸底扫描", args.start or "最早",
                               args.end or "最新")
                info = None
            else:
                reused_inventory = info is not None
        if info is None:
            logger.info("未找到可复用的摸底表，开始摸底扫描...")
            info = discover(sensors=sensors_arg, start=args.start, end=args.end)
            print_inventory(info)
            show_sample(info)
            write_inventory(info)
            logger.info(f"摸底完成: 共 {sum(len(feats) for feats in info.values())} "
                        f"个传感器-特征组合")
        else:
            logger.info(f"复用摸底表: {inv_path}（跳过摸底扫描，"
                        f"共 {sum(len(feats) for feats in info.values())} "
                        f"个传感器-特征组合）")
    else:
        info = {}

    if args.mode in ("preprocess", "all") and not args.traffic_only:
        tasks = build_tasks(info, sensors_arg, args.features,
                            args.start, args.end, args.limit_days)
        if not tasks and reused_inventory:
            # 兜底：复用表可能仍不准确（如部分传感器缺日期），强制重新摸底一次
            logger.warning("复用摸底表后任务数为 0，强制重新摸底扫描一次...")
            info = discover(sensors=sensors_arg, start=args.start, end=args.end)
            write_inventory(info)
            tasks = build_tasks(info, sensors_arg, args.features,
                                args.start, args.end, args.limit_days)
        if not tasks:
            logger.warning("没有符合条件的任务：请确认原始数据目录结构、摸底表"
                           "是否覆盖 %s ~ %s，以及 --bridge 桥名是否正确",
                           args.start or "最早", args.end or "最新")
        processed = run_preprocess(tasks)
        logger.info(f"本次运行结束: 新完成任务 {processed} 个")

    # 交通荷载：周统计 HTML 数据，与传感器数据分开读取
    traffic_root = args.traffic_root
    if not traffic_root:
        cand = os.path.join(os.getcwd(), "inputs")
        if os.path.isdir(cand):
            traffic_root = cand
    traffic_dirs = _find_traffic_dirs(traffic_root, args.bridge)
    if traffic_dirs:
        _n_dir, n_lane, _n_skip = process_traffic(
            traffic_dirs, OUTPUT_ROOT, DAILY_SUBDIR,
            args.start, args.end, RESUME)
        logger.info(f"交通荷载预处理完成: {len(traffic_dirs)} 个期目录, "
                    f"{n_lane} 个车道特征 -> "
                    f"{os.path.join(OUTPUT_ROOT, DAILY_SUBDIR, TRAFFIC_SENSOR)}")
    else:
        logger.info("未找到交通荷载周统计目录，跳过")


if __name__ == "__main__":
    main()
