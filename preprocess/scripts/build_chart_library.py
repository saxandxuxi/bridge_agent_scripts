#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图库生成脚本（集成版）
======================

读取预处理结果，为每个传感器、每个特征：
  1) 计算统计值（中文键）      -> 统计值/<传感器编号>.json
  2) 生成统计图                -> 图库/<传感器编号>/<特征>/时间序列图.png
                                图库/<传感器编号>/<特征>/频率分布图.png
可选 --correlation：同一传感器不同特征间的相关性分析
                                -> 图库/<传感器编号>/相关性_<特征A>_<特征B>.png

数据源（自动选择）:
  A) daily 目录(小时级明细，优先): daily/<传感器>/<特征>/<日期>.csv，
     每天 24 行，按小时描点(一天 24 个点)，横轴仍标日期
  仅使用 daily 目录（小时级/10 分钟级/秒级明细），已取消 summary.csv 日级回退。

进一步预处理(小时级):
  - 零散尖峰点(均值/最大/最小各自检测): 用去掉上下 5% 极端值后的
    稳健基线判定，连续 <=3 小时、且总占比 <=2% 才替代;
    同一小时在多天重复出现的"规律性高峰"(如早晚高峰)视为常态不替代;
    尖峰占比 >2% 时判定疑似传感器异常，不替代并在图上/JSON 里警告;
  - 长时间突变段: 当天"偏高/偏低小时"占多数(>=60%)且连续 >= 7 天，
    才判定为突变区间，图上着色标注 起始时间~结束时间(精确到小时);

统计值覆盖参考脚本的目标口径：
  平均值 / 最大值 / 最小值 / 差值 / 绝对最大值 / 均方根值 / 标准差 / 中位数
  有效小时数 / 缺失小时数 / 有效天数 / 缺失天数，以及每日统计明细。

用法:
    python build_chart_library.py [--daily-root ...] [--lib-root ...]
                                  [--correlation] [--limit-sensors N]
"""

import argparse
import calendar
import csv
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import time
from collections import defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False

# ---------------- 默认路径（可按需修改/用命令行参数覆盖） ----------------
DEFAULT_DAILY_ROOT = r"D:\preprocess_sensor_data\daily"   # 预处理后的 daily 目录
# 图库/统计值的上级目录 = 项目 preprocess/ 目录（按脚本位置计算，与运行目录无关）
DEFAULT_LIB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 传感器对照表（固定产物，不随季度变化，统一挂在 preprocess/ 下）
DEFAULT_SENSOR_MAP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "传感器对照")
# ---------------------------------------------------------------------

# 特征英文代号 -> 中文名（便于中文查询，未列出的按原名显示）
FEATURE_CN = {
    "rh": "湿度", "temp": "温度",
    "nd": "挠度", "rsg": "应变", "sl": "索力", "lf": "裂缝",
    "xJsd": "X向加速度", "xJd": "X向倾角", "yJd": "Y向倾角",
    "spfs": "风速", "spfx": "风向", "szfs": "竖向风速", "szfx": "竖向风向",
    "Δx": "X方向", "Δy": "Y方向", "Δz": "Z方向",
    "Ax": "X方向", "Ay": "Y方向", "Az": "Z方向",
}

# 特征括号内代号 -> 物理合理范围(超出即视为错误值，统计/绘图前剔除)
FEATURE_RANGES = {
    "rh": (0.0, 100.0),            # 湿度 %
    "temp": (-80.0, 80.0),         # 温度 ℃
    "rsg": (-50000.0, 50000.0),    # 应变 με
    "xJsd": (-10000.0, 10000.0),   # 加速度 mg
    "spfs": (0.0, 100.0),          # 水平风速 m/s
    "szfs": (-100.0, 100.0),       # 竖向风速(带符号) m/s
    "spfx": (0.0, 360.0),          # 风向 °
    "szfx": (-360.0, 360.0),       # 竖向风向(数据为带符号小值)
    "xJd": (-90.0, 90.0),          # 倾角 °
    "yJd": (-90.0, 90.0),
    "nd": (-100000.0, 100000.0),   # 挠度 mm
    "sl": (0.0, 100000.0),         # 索力 KN
    "lf": (-10000.0, 10000.0),     # 裂缝 mm
    "Δx": (-100000.0, 100000.0),   # 位移/空间变位 mm
    "Δy": (-100000.0, 100000.0),
    "Δz": (-100000.0, 100000.0),
}

DEFAULT_DIST_K = 20.0   # --dist-k 默认值；dist_k=0 时"突变段"剔除仍用该带宽
VRANGE_MIN_RATIO = 0.98  # 物理范围仅当 >=98% 数据落在区间内才生效，否则仅作绘图参考

# 风向类特征(spfx/szfx)是圆形量，线性"尖峰"没有意义，不做统计尖峰替代；
# 风速(spfs/szfs)按普通特征处理：允许剔除零散尖峰，长段持续偏高/低只标注不剔除。


def feature_range(feature):
    """按特征名(如 WSD(rh)/GNSS(Δx))取物理合理范围，取不到返回 None。"""
    m = re.search(r"\(([^)]+)\)$", feature)
    code = m.group(1) if m else feature
    return FEATURE_RANGES.get(code)


def feature_code(feature):
    m = re.search(r"\(([^)]+)\)$", feature)
    return m.group(1) if m else feature


def feature_display(feature):
    """把 WSD(rh) 之类的特征名补上中文说明。"""
    m = re.search(r"\(([^)]+)\)$", feature)
    if m and m.group(1) in FEATURE_CN:
        return f"{feature}（{FEATURE_CN[m.group(1)]}）"
    return feature


# 交通荷载：daily/交通荷载/车道N/日期.csv，统计库位置统计/交通荷载/交通荷载.json
# 内以 车道X 为键（报告 cell.vehicle_count.车道X.count/ratio 引用）
TRAFFIC_SENSOR = "交通荷载"
TRAFFIC_STAT_FEATURE = "交通荷载"
TRAFFIC_TOTAL_FEATURE = "总共"


def _true_runs(mask):
    """返回连续 True 段的 (start, end_exclusive) 列表（numpy 向量化，O(n) 在 C 层）。"""
    m = np.asarray(mask, dtype=bool)
    padded = np.concatenate(([False], m, [False]))
    diff = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def _fmt_cn_date(d):
    """2026-02-05 -> 2月5日(图上标注用)。"""
    try:
        y, m, day = d.split("-")
        return f"{int(m)}月{int(day)}日"
    except ValueError:
        return d


def _fmt_compact_time(t, with_year=False):
    """紧凑时间: 1.18 3；with_year=True 时 2026.1.18 3。"""
    if with_year:
        return f"{t.year}.{t.month}.{t.day} {t.hour}"
    return f"{t.month}.{t.day} {t.hour}"


def _fmt_compact_range(start_s, end_s):
    """紧凑时间段: 1.18 3~1.19 5(同年省年份)；跨年时两端带年份。"""
    try:
        t0 = dt.datetime.strptime(str(start_s), "%Y-%m-%d %H:%M")
        t1 = dt.datetime.strptime(str(end_s), "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return f"{start_s}~{end_s}"
    if t0.year != t1.year:
        return f"{_fmt_compact_time(t0, True)}~{_fmt_compact_time(t1, True)}"
    return f"{_fmt_compact_time(t0)}~{_fmt_compact_time(t1)}"


def clean_series_value(times, values, label, spike_k=5.0, max_run=3,
                       hour_level=True, vrange=None, max_spikes=3,
                       dist_k=20.0, max_dist_outliers=5,
                       max_total_removals=5):
    """
    尖峰替代 v2：
      0) 物理范围过滤(vrange=(min,max)): 超出合理范围的值(如错误码 435000、
         9e7 等)一律用稳健基线替代，不论连续多长；边界给微小容差
         (量程的百万分之一)，避免基线贴近边界(如索力≈0)时把
         微小测量噪声(-1e-06)误判为超范围导致满图红点。
         仅当 >=98% 数据落在区间内(VRANGE_MIN_RATIO)才做硬过滤；
         否则认为该范围与传感器量程不符，仅作绘图参考、不硬过滤，
         交由分布极端点/尖峰逻辑处理。
      0b) 分布极端点过滤(dist_k): 单个孤立点偏离超过 dist_k×尺度(默认20)
         视为异常值，按偏离程度排名最多剔除 max_total_removals 个；
         连续超过 max_run 个点的异常段不剔除，由 detect_deviation_blocks
         在图上标注"XX时间段偏高/低"(精确到数据粒度)。
         dist_k=0 时关闭散点异常值剔除(异常段标注仍用默认带宽)。
      1) 稳健基线: 去掉上下 5% 极端值后的中位数(不被少数大值拉偏);
      2) 稳健尺度: 修剪后的 MAD(1.4826 倍);
      3) 候选异常: |x - 基线| > spike_k * 尺度(仅对范围内值);
      4) 零散尖峰: 滑动(重合)窗口法——每个窗口去掉 1 个最小+1 个最大后
         看局部分布，被去掉的极值偏离窗口中心超过 spike_k×窗口尺度、
         且被 >=2 个重合窗口命中才算尖峰；连续 > max_run 个点的长段
         属于持续偏高/偏低，不剔除，只标注;
      5) 规律性高峰: 小时级数据中，同一小时在多天(>=20%天数)重复出现
         的高点视为常态(如早晚高峰)，不替代只记录;
      6) 数量上限: 分布极端点 + 零散尖峰合计最多剔除 max_total_removals
         (默认5)个：分布极端点按偏离程度排名先占名额，
         剩余额度给零散尖峰(仍受 max_spikes 上限约束)；
         物理范围外/非有限值不计入。
    返回 (新序列, 记录, 尖峰替代下标, 范围外替代下标)。
    """
    n = len(values)
    if n < 10:
        return list(values), [], [], []
    arr = np.array(values, dtype=float)
    finite = np.isfinite(arr)
    in_range = finite.copy()
    vrange_note = None
    if vrange:
        rlo, rhi = vrange
        tol = max(1e-9, (rhi - rlo) * 1e-6)
        n_finite = int(finite.sum())
        ratio = (inside := (arr >= rlo) & (arr <= rhi))[finite].sum() / n_finite \
            if n_finite else 0.0
        if ratio >= VRANGE_MIN_RATIO:
            # 数据大部分落在区间内 → 物理范围可信，做硬过滤(边界带微小容差)
            in_range &= (arr >= rlo - tol) & (arr <= rhi + tol)
        else:
            # 数据大量落在区间外(如传感器量程/单位不同) → 范围失去参考意义，
            # 仅作绘图参考，不硬过滤，交由分布极端点/尖峰逻辑处理
            vrange_note = (f"物理范围({rlo:g}~{rhi:g})与数据量程不符"
                           f"(命中率{ratio * 100:.1f}%)，仅作绘图参考未硬过滤")

    # 1) 稳健基线(只用范围内、去 5%~95% 极值的数据)
    base_arr = arr[in_range]
    if base_arr.size < 5:
        return list(values), [], [], []
    lo, hi = np.percentile(base_arr, [5.0, 95.0])
    trim = base_arr[(base_arr >= lo) & (base_arr <= hi)]
    if trim.size < 5:
        return list(values), [], [], []
    base = float(np.median(trim))
    mad = float(np.median(np.abs(trim - base)))
    if mad > 0:
        scale = 1.4826 * mad
    else:
        # MAD 退化(>50% 值相同，如静止传感器恒为 0)时，用中央 68% 分位距
        # 估计尺度，避免被少数极端长段(如连续 24h = 3000)把 std 撑大，
        # 导致分布极端值/长段连续异常全部漏检。
        p84, p16 = np.percentile(trim, [84.0, 16.0])
        scale = max(0.0, (p84 - p16) / 2.0)
        if scale <= 0:
            scale = float(trim.std()) if trim.std() > 0 else 1.0
    fixed = arr.copy()
    indices = []
    range_indices = []
    records = []
    if vrange_note:
        records.append({"说明": vrange_note})

    # 0) 范围外/非有限值: 一律替代(不论连续长短，物理错误不占剔除限额)
    bad = ~in_range
    if np.any(bad):
        for t in np.flatnonzero(bad):
            fixed[t] = base
            range_indices.append(int(t))
            reason = ("非有限值(inf/nan)" if not finite[t]
                      else "超出合理范围")
            records.append({
                "时间": str(times[int(t)]),
                "系列": label,
                "原值": round(float(arr[t]), 6),
                "处理": f"{reason}，用稳健基线替代",
            })

    # 0b) 分布极端点：单个孤立点偏离超过 dist_k×尺度(默认20)才剔除；
    #     连续 > max_run 个点的段视为持续偏高/偏低，不剔除，
    #     由 detect_deviation_blocks 负责标注(精确到数据粒度)。
    budget = max(0, int(max_total_removals))
    block_k = dist_k if dist_k > 0 else DEFAULT_DIST_K
    dist_band = block_k * scale
    if budget > 0 and dist_band > 0:
        cand = in_range & (np.abs(arr - base) > dist_band)
        idx = np.flatnonzero(cand)
        iso = [int(t) for t in idx
               if (t == 0 or not cand[t - 1])
               and (t == n - 1 or not cand[t + 1])]
        if iso:
            dev = np.abs(arr[iso] - base)
            order = np.argsort(-dev, kind="stable")
            take = min(len(iso), budget)
            for k in range(take):
                t = iso[order[k]]
                bad[t] = True
                fixed[t] = base
                range_indices.append(t)
                records.append({
                    "时间": str(times[t]),
                    "系列": label,
                    "原值": round(float(arr[t]), 6),
                    "处理": "超出分布极端范围，用稳健基线替代",
                })
            budget -= take

    # 4) 零散尖峰：滑动(重合)窗口法，在每个窗口内去掉 1 个最小+1 个最大后
    #     看局部分布，被去掉的极值远离窗口中心才记为候选(需 >=2 个重合窗口
    #     命中)；再用全局带的连续段长度过滤，长段(> max_run)内的点属于
    #     持续偏高/偏低(走标注)，不当尖峰。
    spike_pos = set()
    if spike_k > 0 and budget > 0:
        cand_idx, _v = detect_window_spikes(
            times, values, k=spike_k, min_votes=2)
        # 长段过滤: 全局 spike 带中连续 > max_run 的点 → 突变段, 不判尖峰
        glob_cand = in_range & ~bad & (np.abs(arr - base) / scale > spike_k)
        long_exclude = np.zeros(n, dtype=bool)
        for a, b in _true_runs(glob_cand):
            if b - a > max_run:
                long_exclude[a:b] = True
        spike_pos = set(t for t in cand_idx
                        if in_range[t] and not bad[t]
                        and not long_exclude[t])

    # 5) 规律性高峰(小时级)
    regular_hours = set()
    if hour_level and spike_pos:
        total_days = len({t.date() for t in times})
        by_hour = defaultdict(set)
        for t in spike_pos:
            by_hour[times[t].hour].add(times[t].date())
        for hour, days in by_hour.items():
            if len(days) >= max(3, int(0.2 * total_days)):
                regular_hours.add(hour)

    if hour_level:
        real = sorted(t for t in spike_pos
                      if times[t].hour not in regular_hours)
    else:
        real = sorted(spike_pos)

    if regular_hours:
        records.append({
            "说明": f"规律性高峰小时 {sorted(regular_hours)}，"
                    f"视为常态未替代",
        })
    if not real:
        return fixed.tolist(), records, indices, range_indices

    # 6) 数量上限: 与分布极端点合计最多剔除 max_total_removals 个
    if len(real) > min(max_spikes, budget):
        real = sorted(real, key=lambda t: abs(arr[t] - base),
                      reverse=True)[:min(max_spikes, budget)]
        records.append({
            "说明": f"尖峰候选超过上限，只替代最极端的 "
                    f"{min(max_spikes, budget)} 个，"
                    f"其余视为正常分布尾部未替代",
        })

    for t in real:
        fixed[t] = base
        indices.append(t)
        records.append({
            "时间": str(times[t]),
            "系列": label,
            "原值": round(float(arr[t]), 6),
            "处理": "零散尖峰，用稳健基线替代",
        })
    return fixed.tolist(), records, indices, range_indices


def fill_long_gaps(hours, means, maxs, mins, min_gap_hours=24):
    """
    检测连续缺失 >= min_gap_hours(默认 24 小时)的数据段：
      1) 在有效小时序列中，相邻两点间隔 >= 24h 即为缺段;
      2) 缺失小时用首尾有效值之间的线性插值填充(只用于绘图，不改统计);
      3) 返回 (填充后的 4 个序列, 缺段记录)。
    """
    n = len(hours)
    if n < 2:
        return (list(hours), list(means), list(maxs), list(mins)), []
    filled_hours = list(hours)
    filled_means = list(means)
    filled_maxs = list(maxs)
    filled_mins = list(mins)
    gaps = []
    for i in range(n - 1):
        t0, t1 = hours[i], hours[i + 1]
        gap_h = (t1 - t0).total_seconds() / 3600.0
        if gap_h < min_gap_hours:
            continue
        miss_count = int(round(gap_h)) - 1
        gaps.append({
            "起始时间": t0.strftime("%Y-%m-%d %H:%M"),
            "结束时间": t1.strftime("%Y-%m-%d %H:%M"),
            "缺失小时数": miss_count,
        })
        v0m, v1m = filled_means[i], filled_means[i + 1]
        v0x, v1x = filled_maxs[i], filled_maxs[i + 1]
        v0n, v1n = filled_mins[i], filled_mins[i + 1]
        for k in range(1, int(round(gap_h))):
            frac = k / gap_h
            filled_hours.append(t0 + dt.timedelta(hours=k))
            filled_means.append(v0m + (v1m - v0m) * frac)
            filled_maxs.append(v0x + (v1x - v0x) * frac)
            filled_mins.append(v0n + (v1n - v0n) * frac)
    # 按时间排序(填充点插在中间)
    order = sorted(range(len(filled_hours)), key=lambda k: filled_hours[k])
    filled_hours = [filled_hours[k] for k in order]
    filled_means = [filled_means[k] for k in order]
    filled_maxs = [filled_maxs[k] for k in order]
    filled_mins = [filled_mins[k] for k in order]
    return (filled_hours, filled_means, filled_maxs, filled_mins), gaps


def _find_runs(labels):
    """找出连续非零段 [(起点, 终点)]。"""
    runs = []
    i, n = 0, len(labels)
    while i < n:
        if labels[i] != 0:
            j = i
            while j < n and labels[j] == labels[i]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def detect_level_shifts(hours, values, min_days=7, k=2.5, max_iter=4,
                        day_frac=0.6):
    """
    小时级突变段检测：
      1) 用基线(中位数)与稳健尺度给每个小时打 高/低/正常 标签；
      2) 按天统计"偏高/偏低小时占比"，占比 >= day_frac 的天才是突变天；
      3) 3 天多数票平滑，剔除最长段后重估基线(迭代)；
      4) 连续 >= min_days 的突变天合并为区间，起止精确到小时。
    返回 [{起始时间, 结束时间, 方向, 段内平均值, 段内最大/最小值,
           基线平均值}]。
    """
    arr = np.array(values, dtype=float)
    n = len(arr)
    if n < 24 * (min_days + 2) or k <= 0:
        return []
    # 按天分组
    day_index = {}
    for i, h in enumerate(hours):
        day_index.setdefault(h.date().isoformat(), []).append(i)
    day_dates = sorted(day_index)

    med = np.median(arr)
    mad = np.median(np.abs(arr - med))
    scale = 1.4826 * mad if mad > 0 else (arr.std() if arr.std() > 0 else 1.0)
    day_labels = np.zeros(len(day_dates), dtype=int)

    for _ in range(max_iter):
        dev = arr - med
        high = dev > k * scale
        low = dev < -k * scale
        lab = np.zeros(len(day_dates), dtype=int)
        for di, d in enumerate(day_dates):
            pos = day_index[d]
            nh = int(np.count_nonzero(high[pos]))
            nl = int(np.count_nonzero(low[pos]))
            if nh >= day_frac * len(pos) and nh > nl:
                lab[di] = 1
            elif nl >= day_frac * len(pos) and nl > nh:
                lab[di] = -1
        # 3 天多数票平滑，去掉零散标签
        sm = np.zeros(len(day_dates), dtype=int)
        for i in range(len(day_dates)):
            w = lab[max(0, i - 1):min(len(day_dates), i + 2)]
            if np.count_nonzero(w == 1) >= 2:
                sm[i] = 1
            elif np.count_nonzero(w == -1) >= 2:
                sm[i] = -1
        day_labels = sm
        # 剔除最长段后重估基线，避免长突变污染"正常"水平
        runs = _find_runs(day_labels)
        if not runs:
            break
        longest = max(runs, key=lambda r: r[1] - r[0] + 1)
        exclude_days = set(day_dates[longest[0]:longest[1] + 1])
        mask = np.array([h.date().isoformat() not in exclude_days
                         for h in hours])
        if mask.sum() < 24 * 5:
            break
        new_med = np.median(arr[mask])
        if abs(new_med - med) < 1e-12:
            break
        med = new_med
        mad = np.median(np.abs(arr[mask] - med))
        scale = (1.4826 * mad if mad > 0
                 else (arr[mask].std() if arr[mask].std() > 0 else 1.0))

    shifts = []
    for a, b in _find_runs(day_labels):
        if b - a + 1 < min_days:
            continue
        d0, d1 = day_dates[a], day_dates[b]
        start_dt = dt.datetime.fromisoformat(d0)
        end_dt = dt.datetime.fromisoformat(d1).replace(hour=23, minute=59)
        pos = [i for i in range(n)
               if day_dates[a] <= hours[i].date().isoformat() <= day_dates[b]]
        seg = arr[pos]
        shifts.append({
            "起始时间": start_dt.strftime("%Y-%m-%d %H:%M"),
            "结束时间": end_dt.strftime("%Y-%m-%d %H:%M"),
            "方向": "偏高" if day_labels[a] == 1 else "偏低",
            "段内平均值": round(float(seg.mean()), 6),
            "段内最大值": round(float(seg.max()), 6),
            "段内最小值": round(float(seg.min()), 6),
            "基线平均值": round(float(med), 6),
        })
    return shifts


def detect_deviation_blocks(times, values, k=DEFAULT_DIST_K, min_points=2):
    """检测连续偏离稳健基线超过 k×尺度的异常段（不剔除，仅用于图上标注）。
    起始/结束时间精确到数据粒度(小时/10分钟/秒)。
    返回与 detect_level_shifts 相同结构的记录列表：
    [{起始时间, 结束时间, 方向(偏高/偏低), 段内平均值, 段内最大值,
      段内最小值, 基线平均值, 来源}]。
    """
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < 2 or k <= 0:
        return []
    finite = np.isfinite(arr)
    base_arr = arr[finite]
    if base_arr.size < 5:
        return []
    lo, hi = np.percentile(base_arr, [5.0, 95.0])
    trim = base_arr[(base_arr >= lo) & (base_arr <= hi)]
    if trim.size < 5:
        return []
    base = float(np.median(trim))
    mad = float(np.median(np.abs(trim - base)))
    if mad > 0:
        scale = 1.4826 * mad
    else:
        p84, p16 = np.percentile(trim, [84.0, 16.0])
        scale = max(0.0, (p84 - p16) / 2.0)
        if scale <= 0:
            scale = float(trim.std()) if trim.std() > 0 else 1.0
    if scale <= 0:
        return []
    cand = finite & (np.abs(arr - base) > k * scale)
    blocks = []
    for a, b in _true_runs(cand):
        if b - a < min_points:
            continue
        seg = arr[a:b]
        direction = "偏高" if float(np.median(seg)) >= base else "偏低"
        blocks.append({
            "起始时间": times[a].strftime("%Y-%m-%d %H:%M"),
            "结束时间": times[b - 1].strftime("%Y-%m-%d %H:%M"),
            "方向": direction,
            "段内平均值": round(float(seg.mean()), 6),
            "段内最大值": round(float(seg.max()), 6),
            "段内最小值": round(float(seg.min()), 6),
            "基线平均值": round(base, 6),
            "来源": "持续偏高/偏低段标注",
        })
    return blocks


def detect_window_spikes(times, values, k=5.0, min_votes=2,
                         window_points=None, overlap=0.5):
    """滑动(重合)窗口尖峰检测：
    在每个窗口内去掉 1 个最小值+1 个最大值，用剩余值估计窗口的稳健中心/尺度；
    被去掉的极值若偏离中心超过 k×尺度，记为尖峰候选(1票)；
    同一数据点被 >= min_votes 个重合窗口同时命中才确认为尖峰，
    从而避免在波动较大的区间把局部极值误判为尖峰。
    返回 (候选下标列表, {下标: 票数})。
    说明：重合窗口下数据点被 1~2 个窗口覆盖，票数取
    min(min_votes, 覆盖窗口数)，避免边界点永远凑不够票被漏检。
    """
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < 10:
        return [], {}
    if window_points is None:
        bucket = _series_bucket_seconds(times)
        window_points = max(12, int(round(86400.0 / bucket)))  # 默认约一天
        # 数据不足一天(如振动按天出图)时收缩窗口，保证仍有多个滑动窗口
        window_points = min(window_points, max(24, n // 4))
    stride = max(1, int(round(window_points * (1.0 - overlap))))
    votes = {}
    appears = {}
    i = 0
    while i < n:
        a = i
        b = min(n, i + window_points)
        m = b - a
        if m >= 5:
            seg = arr[a:b]
            finite_seg = np.isfinite(seg)
            if finite_seg.sum() >= 5:
                seg = seg[finite_seg]
                imin = a + int(np.flatnonzero(finite_seg)[int(np.argmin(seg))])
                imax = a + int(np.flatnonzero(finite_seg)[int(np.argmax(seg))])
                keep = np.ones(finite_seg.sum(), dtype=bool)
                keep[int(np.argmin(seg))] = False
                keep[int(np.argmax(seg))] = False
                rest = seg[keep]
                if rest.size >= 3:
                    center = float(np.median(rest))
                    mad = float(np.median(np.abs(rest - center)))
                    scale = (1.4826 * mad if mad > 0
                             else (float(rest.std()) if rest.std() > 0 else 1.0))
                    for idx, val in ((imin, float(arr[imin])),
                                     (imax, float(arr[imax]))):
                        appears[idx] = appears.get(idx, 0) + 1
                        if abs(val - center) > k * scale:
                            votes[idx] = votes.get(idx, 0) + 1
        i += stride
    cand = sorted(t for t, v in votes.items()
                  if v >= min(min_votes, appears.get(t, 0)))
    return cand, votes


def load_sensor_map(path):
    """读取 传感器编号名称.json，返回 {编号: {信息}} 或 {}。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("传感器", {})
    except Exception as exc:
        print(f"[警告] 传感器编号名称.json 读取失败: {exc}")
        return {}


def discover_sensor_features(daily_root):
    """扫描 daily/<传感器>/<特征>/ 目录结构。"""
    roots = daily_root if isinstance(daily_root, (list, tuple)) \
        else [daily_root]
    result = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for sensor in sorted(os.listdir(root)):
            sroot = os.path.join(root, sensor)
            if not os.path.isdir(sroot):
                continue
            feats = result.setdefault(sensor, [])
            for feature in sorted(os.listdir(sroot)):
                froot = os.path.join(sroot, feature)
                if os.path.isdir(froot) and any(
                        fn.lower().endswith(".csv")
                        for fn in os.listdir(froot)):
                    if feature not in feats:
                        feats.append(feature)
    return result


def read_hourly_series(feature_dir):
    """
    读取某个特征目录下的全部 daily CSV(每天 24 行小时统计)。
    返回 (hours, hmeans, hmaxs, hmins, day_dates, day_means,
          day_maxs, day_mins, day_secs, day_miss)。
    hours 为每个非空小时的起点(datetime)，对应的小时均值/最大/最小
    分别放在 hmeans/hmaxs/hmins；同时给出清洗前的每日聚合。
    """
    dirs = feature_dir if isinstance(feature_dir, (list, tuple)) \
        else [feature_dir]
    hours, hmeans, hmaxs, hmins = [], [], [], []
    day_dates, day_means, day_maxs, day_mins = [], [], [], []
    day_secs, day_miss = [], []
    seen_hours = set()
    for fdir in dirs:
        if not os.path.isdir(fdir):
            continue
        for fn in sorted(os.listdir(fdir)):
            if not fn.lower().endswith(".csv"):
                continue
            date = fn[:-4]
            path = os.path.join(fdir, fn)
            d_means, d_maxs, d_mins, d_secs = [], [], [], 0
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
                            mean = float(row[2])
                            vmin = float(row[3])
                            vmax = float(row[4])
                        except ValueError:
                            continue
                        if (count <= 0 or not (math.isfinite(mean)
                                               and math.isfinite(vmin)
                                               and math.isfinite(vmax))):
                            continue
                        try:
                            t = dt.datetime.fromisoformat(row[0])
                        except ValueError:
                            continue
                        if t in seen_hours:
                            continue
                        seen_hours.add(t)
                        hours.append(t)
                        hmeans.append(mean)
                        hmaxs.append(vmax)
                        hmins.append(vmin)
                        d_means.append(mean)
                        d_maxs.append(vmax)
                        d_mins.append(vmin)
                        d_secs += count
            except OSError:
                continue
            if d_means:
                day_dates.append(date)
                day_means.append(float(np.mean(d_means)))
                day_maxs.append(float(np.max(d_maxs)))
                day_mins.append(float(np.min(d_mins)))
                day_secs.append(d_secs)
                day_miss.append(max(0, 86400 - d_secs))
    return (hours, hmeans, hmaxs, hmins,
            day_dates, day_means, day_maxs, day_mins, day_secs, day_miss)


def _is_second_level_feature(feature: str) -> bool:
    """振动类特征(DZJSD(xJsd)/yJsd/zJsd 等)为秒级全量数据，按天出图。"""
    return feature_code(feature).lower().endswith("jsd")


def read_daily_file(path):
    """读取单个 daily CSV(一天)：返回 (hours, means, maxs, mins, secs, miss)。
    秒级特征一天 86400 行，小时级一天 24 行。"""
    hours, means, maxs, mins = [], [], [], []
    secs = 0
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
                    mean = float(row[2])
                    vmin = float(row[3])
                    vmax = float(row[4])
                except ValueError:
                    continue
                if (count <= 0 or not (math.isfinite(mean)
                                       and math.isfinite(vmin)
                                       and math.isfinite(vmax))):
                    continue
                try:
                    t = dt.datetime.fromisoformat(row[0])
                except ValueError:
                    continue
                hours.append(t)
                means.append(mean)
                maxs.append(vmax)
                mins.append(vmin)
                secs += count
    except OSError:
        return [], [], [], [], 0, 86400
    miss = max(0, 86400 - secs)
    return hours, means, maxs, mins, secs, miss


def aggregate_daily_from_hours(hours, means, maxs, mins):
    """把清洗后的序列重新聚合成每日序列(统计口径用)。"""
    by_day = {}
    for h, m, x, n in zip(hours, means, maxs, mins):
        d = h.date().isoformat()
        by_day.setdefault(d, [[], [], []])
        by_day[d][0].append(m)
        by_day[d][1].append(x)
        by_day[d][2].append(n)
    day_dates, day_means, day_maxs, day_mins = [], [], [], []
    day_secs, day_miss = [], []
    for d in sorted(by_day):
        ms, xs, ns = by_day[d]
        day_dates.append(d)
        day_means.append(float(np.mean(ms)))
        day_maxs.append(float(np.max(xs)))
        day_mins.append(float(np.min(ns)))
        day_secs.append(len(ms) * 3600)
        day_miss.append(max(0, 86400 - len(ms) * 3600))
    return (day_dates, day_means, day_maxs, day_mins,
            day_secs, day_miss)


def compute_feature_stats(dates, means, maxs, mins, seconds=None,
                          missing=None, start=None, end=None,
                          native_means=None, native_maxs=None,
                          native_mins=None):
    """
    由日期/日均值/日最大/日最小系列计算整体统计值 + 每日统计。
    native_means/native_maxs/native_mins 提供时（清洗后的原生粒度序列，
    小时级或秒级，取决于特征），平均值/均方根值用原生均值序列，极值用
    原生最大/最小序列（与报告表格“日最大值/日最小值”聚合口径一致），
    避免“日均值抹平”导致极值失真（如振动 ±5 的瞬时极值被日均摊到 -0.0003）。
    seconds/missing 为每日有效/缺失秒数(可选，来自 daily 明细)。
    返回 (stats_dict, dates, means, maxs, mins)。
    """
    if not dates:
        return None, [], [], [], []

    # 日期过滤
    kept = [(d, m, x, n, (s if seconds else 0),
             (k if missing else 0))
            for d, m, x, n, s, k in zip(
                dates, means, maxs, mins,
                seconds or [0] * len(dates), missing or [0] * len(dates))
            if (not start or d >= start) and (not end or d <= end)]
    if not kept:
        return None, [], [], [], []
    dates, means, maxs, mins, secs, mis = zip(*kept)
    dates = list(dates)
    means = list(means)
    maxs = list(maxs)
    mins = list(mins)
    secs = list(secs)
    mis = list(mis)

    ext_means = native_means if native_means is not None else means
    ext_maxs = native_maxs if native_maxs is not None else maxs
    ext_mins = native_mins if native_mins is not None else mins
    if not ext_maxs:
        ext_maxs = maxs
    if not ext_mins:
        ext_mins = mins
    arr = np.array(ext_means, dtype=float)
    daily = []
    for d, m, x, n in zip(dates, means, maxs, mins):
        daily.append({
            "日期": d,
            "平均值": round(float(m), 6),
            "最大值": round(float(x), 6),
            "最小值": round(float(n), 6),
        })

    stats = {
        "起始日期": dates[0],
        "结束日期": dates[-1],
        "覆盖天数": len(dates),
        "有效小时数": 0,
        "缺失小时数": 0,
        "平均值": round(float(np.mean(arr)), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(np.std(arr)), 6),
        "最大值": round(float(np.max(ext_maxs)), 6),
        "最小值": round(float(np.min(ext_mins)), 6),
        "差值": round(float(np.max(ext_maxs) - np.min(ext_mins)), 6),
        "绝对最大值": round(
            max(abs(np.max(ext_maxs)), abs(np.min(ext_mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
        "每日统计": daily,
    }

    # 有效/缺失小时数：秒数->小时；无秒数时按天估算(每天 24 小时)
    if any(secs):
        total_sec = sum(secs)
        missing_sec = sum(mis)
        stats["有效小时数"] = round(total_sec / 3600, 1)
        stats["缺失小时数"] = round(missing_sec / 3600, 1)
        stats["有效天数"] = sum(1 for s in secs if s > 0)
        stats["缺失天数"] = sum(1 for s in secs if s <= 0)
    else:
        stats["有效小时数"] = len(dates) * 24
        stats["缺失小时数"] = 0
        stats["有效天数"] = len(dates)
        stats["缺失天数"] = 0
    return stats, dates, means, maxs, mins


def _temp_effect_stats(strain_dates, strain_means, temp_dates, temp_means):
    """应变-温度联合统计：按日对齐做线性回归，
    计算 剔除温度后的应变最大值/最小值 与 应变-温度相关系数。
    返回 dict 或 None(数据不足)。"""
    dset = set(strain_dates) & set(temp_dates)
    if len(dset) < 10:
        return None
    ia = {d: i for i, d in enumerate(strain_dates)}
    ib = {d: i for i, d in enumerate(temp_dates)}
    xs = [temp_means[ib[d]] for d in sorted(dset)]
    ys = [strain_means[ia[d]] for d in sorted(dset)]
    xa, ya = np.array(xs, dtype=float), np.array(ys, dtype=float)
    try:
        slope, intercept = np.polyfit(xa, ya, 1)
    except Exception:  # noqa: BLE001
        return None
    resid = ya - (slope * xa + intercept)   # 剔除温度效应后的应变
    if resid.size < 2 or float(np.std(xa)) == 0:
        corr = 0.0
    else:
        corr = float(np.corrcoef(xa, ya)[0, 1])
    return {
        "剔除温度最大值": round(float(np.max(resid)), 6),
        "剔除温度最小值": round(float(np.min(resid)), 6),
        "相关性系数": round(corr, 6),
    }


def _norm_strain_temp_pos(name):
    """位置归一化：去掉 上游/下游/左幅/右幅/左侧/右侧 等方位词，
    用于应变-温度联合统计的跨位置配对（如 顶板左幅 <-> 顶板上游）。
    保留 顶板/底板/截面/跨/墩 等核心部位，避免把不同截面错配。"""
    s = str(name or "")
    for w in ("上游侧", "下游侧", "上游", "下游", "左幅", "右幅",
              "左侧", "右侧"):
        s = s.replace(w, "")
    return re.sub(r"\s+", "", s)


# 允许“应变-温度跨方位配对”的大桥：编号文件里应变用 左幅/右幅、
# 结构温度用 上游/下游，两套命名无法精确配对。
# 对这些桥：同一核心部位(如 炎陵侧中跨1/4截面顶板)的温度序列，
# 同时配给 左幅/右幅 的应变计算 剔除温度/相关性系数。
STRAIN_TEMP_CROSS_SIDE_BRIDGES = {"洣水河", "洣水河特大桥", "mishuihe"}


def _fmt_cn_dt(s):
    """2026-03-26 14:00 -> 3月26日14时(图上标注用)。"""
    try:
        d, t = s.split(" ")
        y, m, day = d.split("-")
        hh = t.split(":")[0]
        return f"{int(m)}月{int(day)}日{int(hh)}时"
    except (ValueError, AttributeError):
        return s


def _fmt_dt_short(dt):
    """紧凑时间标注：1.18 3（月.日 时），分钟非整时补 :MM；
    同一年份不重复写年份，避免图上标注过长重叠。"""
    if getattr(dt, "minute", 0) == 0:
        return f"{dt.month}.{dt.day} {dt.hour}"
    return f"{dt.month}.{dt.day} {dt.hour}:{dt.minute:02d}"


def _series_bucket_seconds(hours) -> int:
    """由时间序列的相邻最小间隔反推聚合粒度(秒)：小时=3600、10分钟=600、秒级=1。"""
    if not hours or len(hours) < 2:
        return 3600
    # 桶长均匀，只需采样前若干间隔即可确定粒度，避免对秒级全量做 O(n) 循环
    sample = min(len(hours) - 1, 1000)
    deltas = [(b - a).total_seconds()
              for a, b in zip(hours[:sample + 1], hours[1:sample + 2])
              if b > a]
    if not deltas:
        return 3600
    d = min(deltas)
    for cand in (86400, 3600, 1800, 600, 300, 60, 30, 10, 5, 1):
        if abs(d - cand) < cand * 0.02:
            return cand
    return int(round(d))


def _bucket_label(bucket_seconds: int) -> str:
    """聚合粒度对应的图例文案：小时=均值、10分钟=10分钟均值、秒级=秒级均值。"""
    if bucket_seconds == 3600:
        return "均值"
    if bucket_seconds == 600:
        return "10分钟均值"
    if bucket_seconds == 60:
        return "1分钟均值"
    if bucket_seconds == 1:
        return "秒级均值"
    return f"{bucket_seconds}秒均值"


def _unwrap_circular(values, period=360.0, jump=180.0):
    """把 0~360 的圆形量展开成连续序列（仅用于绘图）。
    消除 350°->10° 这种 0<->360 的伪跳变（物理上只差 20°）。"""
    arr = np.asarray(values, dtype=float)
    out = arr.copy()
    for i in range(1, len(arr)):
        d = out[i] - out[i - 1]
        if d > jump:
            out[i] -= period
        elif d < -jump:
            out[i] += period
    return out


def _is_direction_feature(feature: str) -> bool:
    """风向类特征：水平风向 spfx、竖向风向 szfx（圆形量）。"""
    return feature_code(feature) in ("spfx", "szfx")


def _cap_shifts(shifts, max_n: int = 5):
    """突变段只保留偏离(段均值-基线)最大的前 max_n 条，避免标注淹没图面。"""
    if max_n <= 0 or len(shifts) <= max_n:
        return shifts
    def key(s):
        try:
            return abs(float(s.get("段内平均值", 0)) - float(s.get("基线平均值", 0)))
        except (TypeError, ValueError):
            return 0.0
    return sorted(shifts, key=key, reverse=True)[:max_n]


def _cap_annotations(items, max_n=5, key=None):
    """标注(缺失/突变段)过多时只保留最显著的 max_n 条并返回被截断的条数，
    避免图上文字重叠；默认按缺失小时数降序。返回 (保留列表, 截断条数)。"""
    items = list(items or [])
    if len(items) <= max_n:
        return items, 0
    if key is None:
        key = lambda it: float(it.get("缺失小时数", 0) or 0)
    ordered = sorted(items, key=key, reverse=True)[:max_n]
    return ordered, len(items) - max_n


def _label_on_bands(ax, fig, items, fontsize=12, max_n=4):
    """缺失/突变段 ≤ max_n 条时，文字统一放图上方留白区逐行排列：
    - 按行高向上扩展 y 轴预留空间，文字不会压住数据线段；
    - 一行一条，行高固定，绝不重叠；
    - 图上色带保留，文字与色带通过紧凑时间段文字对应。"""
    if not items:
        return
    try:
        from matplotlib.dates import date2num
    except Exception:  # noqa: BLE001
        date2num = None
    n = len(items)
    y0, y1 = ax.get_ylim()
    yspan = (y1 - y0) or 1.0
    try:
        ax_pt = (ax.get_window_extent(fig.canvas.get_renderer()).height
                 * 72.0 / fig.dpi)
    except Exception:  # noqa: BLE001
        ax_pt = 250.0
    row_pt = fontsize + 6.0
    if ax_pt > n * row_pt:
        expand = n * row_pt * yspan / (ax_pt - n * row_pt)
    else:
        expand = yspan * max(0.3, 0.05 * n)
    y1_new = y1 + expand
    ax.set_ylim(y0, y1_new)
    row_h = expand / n
    xlim0, xlim1 = ax.get_xlim()
    x_left = xlim0 + (xlim1 - xlim0) * 0.02
    for i, (label, color, xv, yv) in enumerate(items):
        ax.text(x_left, y1_new - row_h * (i + 0.5), label,
                ha="left", va="center", fontsize=fontsize,
                fontweight="bold", color=color,
                bbox=dict(facecolor="white", alpha=0.9, pad=0.9,
                          edgecolor=color, linewidth=0.8))


def _label_in_margin(fig, axes_region, items, fontsize=12):
    """缺失/突变段 > max_n 条时，文字全部挪到子图右侧留白区逐行排列，
    图内只保留色带；行高按子图纵向高度均分，不会重叠。"""
    if not items:
        return
    n = len(items)
    left, bottom, width, height = axes_region.bounds
    row_h = height / n
    x = left + width + 0.012
    for i, (label, color, xv, yv) in enumerate(items):
        fig.text(x, bottom + height - row_h * (i + 0.5), label,
                 ha="left", va="center", fontsize=fontsize,
                 fontweight="bold", color=color,
                 bbox=dict(facecolor="white", alpha=0.9, pad=0.7,
                           edgecolor=color, linewidth=0.8))


# 0 为正常值的特征：风速(代号 spfs/szfs)、裂缝(前缀 LF，如 LF(Δx))、
# 挠度(代号 nd / 前缀 ND)。静风、裂缝闭合、挠度空载时长时间为 0 属正常，
# 不按 24h 标注；只有连续恒 0 超过一周才标“可能故障”。
ZERO_OK_CODES = {"spfs", "szfs", "nd"}
ZERO_OK_PREFIXES = {"LF", "ND"}
ZERO_OK_MIN_HOURS = 24.0 * 7


def zero_min_hours(feature, default=24.0):
    """恒 0 标注阈值：普通特征 24h；0 为正常值的特征放宽到一周。"""
    code = feature_code(feature)
    if code in ZERO_OK_CODES:
        return ZERO_OK_MIN_HOURS
    prefix = re.match(r"^[A-Za-z0-9]+", str(feature))
    if prefix and prefix.group(0).upper() in ZERO_OK_PREFIXES:
        return ZERO_OK_MIN_HOURS
    return default


def detect_zero_runs(hours, means, min_hours=24.0):
    """检测连续恒 0 超过 min_hours 的段（疑似传感器故障/未接入），
    返回 [{起始时间, 结束时间, 持续小时数}]。"""
    runs = []
    if not hours or len(hours) < 2:
        return runs
    start = None
    for i, v in enumerate(means):
        try:
            is_zero = abs(float(v)) <= 1e-9
        except (TypeError, ValueError):
            is_zero = False
        if is_zero and start is None:
            start = i
        elif not is_zero and start is not None:
            dur = (hours[i - 1] - hours[start]).total_seconds() / 3600.0
            if dur > min_hours:
                runs.append({
                    "起始时间": hours[start].strftime("%Y-%m-%d %H:%M"),
                    "结束时间": hours[i - 1].strftime("%Y-%m-%d %H:%M"),
                    "持续小时数": round(dur, 1),
                })
            start = None
    if start is not None:
        dur = (hours[-1] - hours[start]).total_seconds() / 3600.0
        if dur > min_hours:
            runs.append({
                "起始时间": hours[start].strftime("%Y-%m-%d %H:%M"),
                "结束时间": hours[-1].strftime("%Y-%m-%d %H:%M"),
                "持续小时数": round(dur, 1),
            })
    return runs


def _mask_zero_run_hours(hours, means, maxs, mins, runs):
    """剔除落在连续恒0时间段内的小时/秒级点，返回 (hours, means, maxs, mins)。"""
    if not runs:
        return hours, means, maxs, mins
    spans = []
    for z in runs:
        try:
            t0 = dt.datetime.strptime(z["起始时间"], "%Y-%m-%d %H:%M")
            t1 = dt.datetime.strptime(z["结束时间"], "%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            continue
        spans.append((t0, t1))
    if not spans:
        return hours, means, maxs, mins
    ho, mo, xo, no = [], [], [], []
    for i, h in enumerate(hours):
        if any(t0 <= h <= t1 for t0, t1 in spans):
            continue
        ho.append(h)
        mo.append(means[i])
        xo.append(maxs[i])
        no.append(mins[i])
    return ho, mo, xo, no


def plot_time_series(sensor_id, sensor_name, feature, times, means,
                     out_path, shifts=None, replaced_indices=None,
                     replaced_range_indices=None, hour_level=True, gaps=None):
    """
    时间序列图。
    hour_level=True 时按小时描点(一天 24 个点)，横轴仍标日期；
    突变区间着色标注，文字放在图内顶部(不与标题重叠)。
    """
    x = list(range(len(times)))
    fig, ax = plt.subplots(figsize=(15, 5.5))
    mean_label = _bucket_label(_series_bucket_seconds(times))
    plot_means = (_unwrap_circular(means) if _is_direction_feature(feature)
                  else means)
    if len(times) == 1:
        ax.plot(x, plot_means, "o", color="#1f77b4", markersize=8,
                label=mean_label)
    else:
        ax.plot(x, plot_means, "-", color="#1f77b4", linewidth=1.2,
                label=mean_label)

    # 尖峰替代位置打叉标记(黑=统计尖峰, 红=物理范围外)
    if replaced_indices:
        ax.plot([x[i] for i in replaced_indices],
                [means[i] for i in replaced_indices],
                "x", color="black", markersize=8, mew=2,
                label="已替换尖峰点(统计)")
    if replaced_range_indices:
        ax.plot([x[i] for i in replaced_range_indices],
                [means[i] for i in replaced_range_indices],
                "x", color="#d62728", markersize=9, mew=2,
                label="已剔除异常值(物理范围外/分布极端)")

    # 横轴: 日期刻度(小时模式下按天定位，避免挤)
    if hour_level:
        day_start = {}
        for i, t in enumerate(times):
            day_start.setdefault(t.date().isoformat(), i)
        day_list = sorted(day_start)
        step = max(1, len(day_list) // 15)
        tick_days = day_list[::step]
        ax.set_xticks([day_start[d] for d in tick_days])
        ax.set_xticklabels([_fmt_cn_date(d) for d in tick_days],
                           rotation=30, fontsize=9)
    else:
        step = max(1, len(times) // 15)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([_fmt_cn_date(t) for t in times[::step]],
                           rotation=30, fontsize=9)

    # 突变区间 + 缺失段标注: 着色段全部画上；≤4 段文字标在带上，
    # >4 段文字挪到图右侧留白区，图上只留色带
    label_items = []   # [(label, color, xv, yv)]
    for s in shifts or []:
        try:
            t0 = dt.datetime.strptime(s["起始时间"], "%Y-%m-%d %H:%M")
            t1 = dt.datetime.strptime(s["结束时间"], "%Y-%m-%d %H:%M")
            if hour_level:
                a = min(range(len(times)),
                        key=lambda i: abs((times[i] - t0).total_seconds()))
                b = min(range(len(times)),
                        key=lambda i: abs((times[i] - t1).total_seconds()))
            else:
                a = times.index(s["起始时间"][:10])
                b = times.index(s["结束时间"][:10])
        except (ValueError, KeyError):
            continue
        color = "#d62728" if s["方向"] == "偏高" else "#2ca02c"
        ax.axvspan(a - 0.5, b + 0.5, color=color, alpha=0.12)
        yv = (plot_means[b] if b < len(plot_means)
              else plot_means[a] if a < len(plot_means) else 0.0)
        label_items.append(
            (f"{_fmt_compact_range(s['起始时间'], s['结束时间'])} "
             f"{s['方向']}", color, float(x[a]), yv))
    for g in gaps or []:
        try:
            t0 = dt.datetime.strptime(g["起始时间"], "%Y-%m-%d %H:%M")
            t1 = dt.datetime.strptime(g["结束时间"], "%Y-%m-%d %H:%M")
            a = min(range(len(times)),
                    key=lambda i: abs((times[i] - t0).total_seconds()))
            b = min(range(len(times)),
                    key=lambda i: abs((times[i] - t1).total_seconds()))
        except (ValueError, KeyError):
            continue
        ax.axvspan(a - 0.5, b + 0.5, color="#ff7f0e", alpha=0.18)
        yv = (plot_means[b] if b < len(plot_means)
              else plot_means[a] if a < len(plot_means) else 0.0)
        label_items.append(
            (_fmt_compact_range(g['起始时间'], g['结束时间']), "#d2691e",
             float(x[a]), yv))
    # 连续恒 0 超过阈值(普通 24h，0 为正常值的特征一周)：
    # 紫色色带 + “编号(时间段)可能故障”
    for z in detect_zero_runs(times, means,
                              min_hours=zero_min_hours(feature)):
        try:
            t0 = dt.datetime.strptime(z["起始时间"], "%Y-%m-%d %H:%M")
            t1 = dt.datetime.strptime(z["结束时间"], "%Y-%m-%d %H:%M")
            a = min(range(len(times)),
                    key=lambda i: abs((times[i] - t0).total_seconds()))
            b = min(range(len(times)),
                    key=lambda i: abs((times[i] - t1).total_seconds()))
        except (ValueError, KeyError):
            continue
        ax.axvspan(a - 0.5, b + 0.5, color="#9467bd", alpha=0.16)
        yv = (plot_means[b] if b < len(plot_means)
              else plot_means[a] if a < len(plot_means) else 0.0)
        label_items.append(
            (f"{sensor_id}({_fmt_compact_range(z['起始时间'], z['结束时间'])})"
             f"可能故障", "#9467bd", float(x[a]), yv))
    if label_items:
        y0, y1 = ax.get_ylim()
        if len(label_items) <= 4:
            _label_on_bands(ax, fig, label_items, fontsize=10)

    ax.set_xlabel("日期")
    ax.set_ylabel("数值")
    if sensor_name:
        title = (f"{sensor_name}（编号{sensor_id}）- "
                 f"{feature_display(feature)} 时间序列")
    else:
        title = f"传感器{sensor_id} - {feature_display(feature)} 时间序列"
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=10, title=f"编号 {sensor_id}")
    fig.tight_layout()
    if label_items and len(label_items) > 4:
        # 多段标注: 收缩子图宽度，文字画到右侧留白区
        x0, y0, w, h = ax.get_position().bounds
        ax.set_position([x0, y0, w * 0.76, h])
        _label_in_margin(fig, ax.get_position(), label_items, fontsize=9)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_histogram(sensor_id, sensor_name, feature, means, out_path):
    """频率分布直方图：小时均值分布。"""
    if not means:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(means, bins=min(40, max(10, len(means) // 5)),
            density=True, alpha=0.75, color="#4c72b0", edgecolor="black")
    ax.set_xlabel("数值")
    ax.set_ylabel("频率")
    if sensor_name:
        title = (f"{sensor_name}（编号{sensor_id}）- "
                 f"{feature_display(feature)} 频率分布")
    else:
        title = f"传感器{sensor_id} - {feature_display(feature)} 频率分布"
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def density2d(x, y):
    """2D 密度估计(参考原脚本的核密度做法)。"""
    if not HAS_SCIPY or len(x) < 5:
        return None
    xi = np.linspace(min(x), max(x), 100)
    yi = np.linspace(min(y), max(y), 100)
    xi, yi = np.meshgrid(xi, yi)
    kernel = scipy_stats.gaussian_kde(np.vstack([x, y]))
    zi = np.reshape(kernel(np.vstack([xi.ravel(), yi.ravel()])).T, xi.shape)
    try:
        from scipy.interpolate import griddata
        grid_z = griddata((xi.flatten(), yi.flatten()), zi.flatten(),
                          (np.column_stack((x, y))[:, 0],
                           np.column_stack((x, y))[:, 1]), method="linear")
        return grid_z
    except Exception:
        return None


def plot_correlation(feat_a, feat_b, x, y, sensor_name, sensor_id, out_path):
    """相关性分析图：密度散点 + 回归直线 + 相关系数。"""
    if len(set(x)) < 2 or len(set(y)) < 2:
        return None  # 常数列无法计算相关系数
    fig, ax = plt.subplots(figsize=(9, 6))
    density = density2d(x, y)
    if density is not None:
        ax.scatter(x, y, c=density, s=8, cmap="GnBu")
    else:
        ax.scatter(x, y, s=8, alpha=0.6, color="#4c72b0")
    slope, intercept, r = np.polyfit(x, y, 1)[0], 0.0, 0.0
    if len(x) >= 2:
        slope, intercept = np.polyfit(x, y, 1)
        r = np.corrcoef(x, y)[0, 1]
    xq = np.linspace(min(x), max(x), 100)
    ax.plot(xq, slope * xq + intercept, "k:", linewidth=1.5, label="回归直线")
    ax.set_title(f"{sensor_name}（编号{sensor_id}）相关性分析")
    ax.set_xlabel(feature_display(feat_a))
    ax.set_ylabel(feature_display(feat_b))
    ax.text(0.03, 0.90, f"y = {slope:.4f}x + {intercept:.4f}\nr = {r:.4f}",
            transform=ax.transAxes, fontsize=11,
            bbox=dict(facecolor="white", alpha=0.8))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return {"相关系数": round(float(r), 6),
            "斜率": round(float(slope), 6),
            "截距": round(float(intercept), 6)}


def plot_daily_time_series(sensor_id, sensor_name, feature, day_date, times,
                           means, out_path, replaced_indices=None,
                           replaced_range_indices=None, shifts=None,
                           gaps=None, dpi=200):
    """振动按天时间序列图：横轴 0~24 小时(秒级点按小时定位)，
    标题含具体年月日；图上标注尖峰/异常/突变段/缺失。"""
    t0 = times[0].replace(hour=0, minute=0, second=0, microsecond=0)
    xs = [(t - t0).total_seconds() / 3600.0 for t in times]
    fig, ax = plt.subplots(figsize=(15, 5.5))
    ax.plot(xs, means, "-", color="#1f77b4", linewidth=0.9,
            label="秒级均值")
    if replaced_indices:
        ax.plot([xs[i] for i in replaced_indices],
                [means[i] for i in replaced_indices],
                "x", color="black", markersize=8, mew=2,
                label="已替换尖峰点(统计)")
    if replaced_range_indices:
        ax.plot([xs[i] for i in replaced_range_indices],
                [means[i] for i in replaced_range_indices],
                "x", color="#d62728", markersize=9, mew=2,
                label="已剔除异常值(物理范围外/分布极端)")
    ax.set_xticks(range(0, 25, 6))
    ax.set_xticklabels([f"{h}时" for h in range(0, 25, 6)], fontsize=12)
    ax.set_xlim(0, 24)
    ax.set_xlabel("时刻（小时）", fontsize=13)
    ax.set_ylabel(feature_display(feature), fontsize=13)
    ax.tick_params(axis="y", labelsize=12)
    ax.grid(True, alpha=0.3)
    ax.set_title(
        f"{sensor_id} {sensor_name}｜{feature_display(feature)}｜"
        f"{_fmt_cn_date(day_date)}", fontsize=14)

    # 突变区间 + 缺失段标注: 着色段全部画上；≤4 段文字标在带上，
    # >4 段文字挪到图右侧留白区，图上只留色带
    label_items = []   # [(label, color, xv, yv)]
    for s in shifts or []:
        try:
            st = dt.datetime.strptime(s["起始时间"], "%Y-%m-%d %H:%M")
            et = dt.datetime.strptime(s["结束时间"], "%Y-%m-%d %H:%M")
            a = min(range(len(times)),
                    key=lambda i: abs((times[i] - st).total_seconds()))
            b = min(range(len(times)),
                    key=lambda i: abs((times[i] - et).total_seconds()))
        except (ValueError, KeyError):
            continue
        color = "#d62728" if s["方向"] == "偏高" else "#2ca02c"
        ax.axvspan(xs[a] - 0.01, xs[b] + 0.01, color=color, alpha=0.12)
        yv = (means[b] if b < len(means)
              else means[a] if a < len(means) else 0.0)
        label_items.append(
            (f"{_fmt_compact_range(s['起始时间'], s['结束时间'])} "
             f"{s['方向']}", color, float(xs[a]), yv))
    for g in gaps or []:
        try:
            st = dt.datetime.strptime(g["起始时间"], "%Y-%m-%d %H:%M")
            et = dt.datetime.strptime(g["结束时间"], "%Y-%m-%d %H:%M")
            a = min(range(len(times)),
                    key=lambda i: abs((times[i] - st).total_seconds()))
            b = min(range(len(times)),
                    key=lambda i: abs((times[i] - et).total_seconds()))
        except (ValueError, KeyError):
            continue
        ax.axvspan(xs[a] - 0.01, xs[b] + 0.01, color="#ff7f0e", alpha=0.18)
        yv = (means[b] if b < len(means)
              else means[a] if a < len(means) else 0.0)
        label_items.append(
            (_fmt_compact_range(g['起始时间'], g['结束时间']), "#d2691e",
             float(xs[a]), yv))
    if label_items:
        y0, y1 = ax.get_ylim()
        if len(label_items) <= 4:
            _label_on_bands(ax, fig, label_items, fontsize=12)
    ax.legend(loc="best", fontsize=10)
    fig.tight_layout()
    if label_items and len(label_items) > 4:
        # 多段标注: 收缩子图宽度，文字画到右侧留白区
        x0, y0, w, h = ax.get_position().bounds
        ax.set_position([x0, y0, w * 0.76, h])
        _label_in_margin(fig, ax.get_position(), label_items, fontsize=11)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_histogram_from_counts(sensor_id, sensor_name, feature, bin_edges,
                               counts, out_path):
    """由累积直方图计数画频率分布图(振动按天处理时季度汇总用)。"""
    centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    widths = np.diff(bin_edges)
    total = float(counts.sum()) or 1.0
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.bar(centers, counts / (total * widths), width=widths,
           color="#1f77b4", alpha=0.75, edgecolor="black", linewidth=0.4)
    ax.set_title(
        f"{sensor_id} {sensor_name}｜{feature_display(feature)} 频率分布",
        fontsize=14)
    ax.set_xlabel("数值", fontsize=13)
    ax.set_ylabel("频率密度", fontsize=13)
    ax.tick_params(labelsize=12)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ==================== 交通荷载跨车道图（不生成车道子文件夹） ====================

def _traffic_date_axis(ax, hours, x):
    """交通图横轴：按小时描点，刻度标日期。"""
    step = max(1, len(x) // 12)
    ticks = list(range(0, len(x), step))
    if ticks[-1] != len(x) - 1:
        ticks.append(len(x) - 1)
    ax.set_xticks(ticks)
    ax.set_xticklabels([hours[i].strftime("%m-%d") for i in ticks],
                       rotation=30, fontsize=8)
    ax.grid(alpha=0.3)


def plot_traffic_charts(series, total_series, out_dir, dpi=200):
    """交通荷载跨车道合并图（各车道车辆累计通过数量图 / 各车道通过数量
    比例图 / 各车道频率分布图），直接放在 交通荷载/ 下，不分子车道文件夹。

    series: [(车道名, hours, means), ...]（清洗后小时计数，各车道时间对齐）
    total_series: (hours, means) 或 None（比例分母，缺省按各车道求和）
    """
    os.makedirs(out_dir, exist_ok=True)
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    n = len(series)
    x = list(range(len(series[0][2])))

    # 1) 各车道车辆累计通过数量图（多元时间序列折线）
    fig, ax = plt.subplots(figsize=(15, 6), dpi=dpi)
    for i, (lane, hours, means) in enumerate(series):
        ax.plot(x, np.cumsum(means), "-",
                color=colors[i % len(colors)], linewidth=1.3, label=lane)
    _traffic_date_axis(ax, series[0][1], x)
    ax.set_ylabel("累计通过数量（辆）")
    ax.set_title("各车道车辆累计通过数量图", fontsize=14)
    ax.legend(loc="upper left", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "各车道车辆累计通过数量图.png"),
                dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # 2) 各车道通过数量比例图（小时级数据，子图）：每个车道一张子图，
    #    绘制在同一张画布上，避免四条折线叠在一起看不清。
    total_arr = None
    if total_series and total_series[1]:
        total_arr = np.array(total_series[1], dtype=float)
    nrows = 2 if n > 2 else 1
    ncols = 2 if n > 1 else 1
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(15, 2.8 * nrows), dpi=dpi,
                             squeeze=False)
    for i, (lane, hours, means) in enumerate(series):
        ax = axes.flat[i]
        m = np.array(means, dtype=float)
        if total_arr is not None and len(total_arr) == len(m):
            denom = total_arr
        else:
            denom = np.zeros(len(m))
            for _l, _h, _mm in series:
                denom += np.array(_mm, dtype=float)
        pct = np.where(denom > 0, m / np.maximum(denom, 1e-9) * 100, np.nan)
        xi = list(range(len(m)))
        ax.plot(xi, pct, "-", color=colors[i % len(colors)], linewidth=0.8)
        _traffic_date_axis(ax, hours, xi)
        ax.set_ylim(0, 100)
        ax.set_ylabel("比例（%）", fontsize=9)
        valid = pct[np.isfinite(pct)]
        avg = float(np.mean(valid)) if valid.size else 0.0
        ax.set_title(f"{lane}（平均 {avg:.1f}%）", fontsize=11)
    for j in range(n, nrows * ncols):
        axes.flat[j].axis("off")
    fig.suptitle("各车道通过数量比例图（小时级）", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(os.path.join(out_dir, "各车道通过数量比例图.png"),
                dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # 3) 各车道频率分布图（2x2 子图，每车道一张直方图）
    nrows = 2 if n > 2 else 1
    ncols = 2 if n > 1 else 1
    fig, axes = plt.subplots(nrows, ncols, figsize=(12, 8), dpi=dpi,
                             squeeze=False)
    for i, (lane, hours, means) in enumerate(series):
        ax = axes.flat[i]
        ax.hist(means, bins=40, color=colors[i % len(colors)], alpha=0.75)
        ax.set_title(lane, fontsize=12)
        ax.set_xlabel("小时通过数量（辆）", fontsize=10)
        ax.set_ylabel("小时数", fontsize=10)
        ax.grid(alpha=0.3)
    for j in range(n, nrows * ncols):
        axes.flat[j].axis("off")
    fig.suptitle("各车道频率分布图", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(os.path.join(out_dir, "各车道频率分布图.png"),
                dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _build_traffic_charts(args, sensor_feats, chart_dir, issues):
    """读取交通荷载各车道小时序列（清洗口径与统计库一致），生成跨车道合并图。"""
    feats = sensor_feats.get(TRAFFIC_SENSOR, []) or []
    lanes = sorted([f for f in feats if re.match(r"^车道\d+$", f)],
                   key=lambda x: int(re.search(r"\d+", x).group()))
    if args.features:
        lanes = [f for f in lanes
                 if _traffic_selected(args.features)
                 or _feature_selected(f, args.features)]
    if not lanes:
        return

    def _clean_lane(lane):
        hours, means, _, _, _, _, _, _, _, _ = read_hourly_series(
            resolve_feature_dirs(args.daily_root, TRAFFIC_SENSOR, lane))
        if not hours:
            return None, None
        keep = [i for i, h in enumerate(hours)
                if (not args.start or h.date().isoformat() >= args.start)
                and (not args.end or h.date().isoformat() <= args.end)]
        hours = [hours[i] for i in keep]
        means = [means[i] for i in keep]
        if not hours:
            return None, None
        means, _recs, _ix, _rx = clean_series_value(
            hours, means, f"{TRAFFIC_SENSOR}-{lane}", args.spike_threshold,
            hour_level=True, vrange=feature_range(lane),
            max_spikes=args.max_spikes, dist_k=args.dist_k,
            max_dist_outliers=args.max_dist_outliers,
            max_total_removals=args.max_removals)
        return (hours if means else None), means

    series = []
    for lane in lanes:
        hours, means = _clean_lane(lane)
        if hours:
            series.append((lane, hours, means))
    if not series:
        issues.append(f"无数据: {TRAFFIC_SENSOR}/交通荷载图")
        return

    total_series = None
    if TRAFFIC_TOTAL_FEATURE in feats:
        thours, tmeans = _clean_lane(TRAFFIC_TOTAL_FEATURE)
        if thours:
            total_series = (thours, tmeans)
    plot_traffic_charts(series, total_series,
                        os.path.join(chart_dir, TRAFFIC_SENSOR),
                        dpi=args.dpi)


# ==================== 合并绘图（按监测部位分组，一张图多测点/多特征） ====================

AXIS_INNER = {"Δx", "Δy", "Δz", "x", "y", "z"}


def _is_axis_triple(pairs):
    """判断 (sensor, feature) 列表是否为“同一传感器 X/Y/Z 三向分量”
    （如 GNSS(Δx/Δy/Δz)、SZJSD(xJsd/yJsd/zJsd)、EZJD(xJd/yJd)），
    用于时间序列图/直方图按三行一列竖排。"""
    if len(pairs) != 3:
        return False
    sensors = {str(s) for s, _ in pairs}
    if len(sensors) != 1:
        return False
    feats = [f for _, f in pairs]
    if len(set(feats)) != 3:
        return False
    inners = []
    for f in feats:
        m = re.search(r"\(([^)]+)\)$", str(f))
        if not m:
            return False
        inners.append(m.group(1))
    return all(i in AXIS_INNER or i.lower() in AXIS_INNER
               or i.lower().endswith(("jd", "jsd")) for i in inners)


def feature_group(feature: str) -> str:
    """把特征编码归组：
      - GNSS(Δx/Δy/Δz)、EZJD(xJd/yJd)、DZJSD(xJsd) 等轴/方向分量 -> 同一组(prefix)
      - WSD(rh)/WSD(temp)、WD(temp)、YB(rsg) 等不同测量 -> 各自成组
      - FSFX2(spfs/szfs) 风速、FSFX2(spfx/szfx) 风向 -> 按类成组
    """
    m = re.search(r"^([A-Za-z0-9]+)\(([^)]+)\)$", feature)
    if not m:
        return feature
    prefix, inner = m.group(1), m.group(2)
    if inner in AXIS_INNER or inner.lower() in AXIS_INNER:
        return prefix
    if inner.lower().endswith(("jd", "jsd")):
        return prefix
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return f"{prefix}({inner[-1].lower()})"
    return feature


def load_position_map(name_dict_path: str = "", sensor_map_path: str = "") -> dict:
    """返回 {位置名: [(传感器编号, 特征编码), ...]}。
    优先用 传感器名称对照/<桥>.json 的 传感器名称；否则回退 传感器编号名称.json。"""
    pos_map = defaultdict(list)
    if name_dict_path and os.path.isfile(name_dict_path):
        try:
            with open(name_dict_path, encoding="utf-8") as f:
                data = json.load(f)
            for name, entries in (data.get("传感器名称") or {}).items():
                for e in entries or []:
                    feats = e.get("特征编码") or ([e["特征"]] if e.get("特征") else [])
                    for feat in feats:
                        pos_map[name].append((str(e["编号"]), feat))
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 读取名称对照失败: {exc}")
    if not pos_map and sensor_map_path and os.path.isfile(sensor_map_path):
        try:
            with open(sensor_map_path, encoding="utf-8") as f:
                data = json.load(f)
            for sid, info in (data.get("传感器") or {}).items():
                name = info.get("监测部位") or info.get("名称") or ""
                if name:
                    pos_map[name].append((str(sid), info.get("类别", "")))
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 读取传感器对照失败: {exc}")
    return {k: sorted(set(v)) for k, v in pos_map.items()}


def _safe_dirname(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()


def _bridge_variants(name):
    """桥名的常见写法变体(湘江特 <-> 湘江特大桥)，用于兼容预处理与
    建图库传不同桥名时目录对不上(如预处理 --bridge 湘江特、建图库
    --bridge 湘江特大桥)。"""
    name = (name or "").strip()
    if not name:
        return []
    variants = [name]
    for suffix in ("特大桥", "大桥"):
        if name.endswith(suffix):
            variants.append(name[:-len(suffix)])
        else:
            variants.append(name + suffix)
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _feature_selected(feature: str, feature_arg: str) -> bool:
    """--features 过滤：留空=全部；否则特征名包含任一给定词即选中
    （如传 DZJSD 会匹配 DZJSD(xJsd)）。"""
    if not feature_arg:
        return True
    fn = str(feature).lower()
    return any(t.strip() and t.strip().lower() in fn
               for t in feature_arg.split(","))


def _traffic_selected(feature_arg: str) -> bool:
    """--features 里是否选了“交通荷载”（匹配 daily 下的 车道N/总共 特征）。"""
    return any(t.strip() == TRAFFIC_SENSOR
               for t in (feature_arg or "").split(","))


def resolve_feature_dir(daily_root, sensor, feature):
    """定位 daily 下某特征的目录。模块前缀不一致时(如对照表 DZJSD(xJsd)
    但实际数据 SZJSD(xJsd))，按括号内编码(xJsd/yJsd/zJsd)匹配实际存在的目录。
    返回 (目录路径, 实际特征名)。"""
    dirs = resolve_feature_dirs(daily_root, sensor, feature)
    if dirs:
        return dirs[0], os.path.basename(dirs[0])
    root = daily_root if isinstance(daily_root, str) else daily_root[0]
    return os.path.join(root, str(sensor), feature), feature


def resolve_feature_dirs(daily_root, sensor, feature):
    """定位 daily 下某特征的全部目录（年度多季度时返回多个）。
    模块前缀不一致时按括号内编码匹配实际存在的目录。"""
    roots = daily_root if isinstance(daily_root, (list, tuple)) \
        else [daily_root]
    out = []
    target = feature_code(feature)
    for root in roots:
        d = os.path.join(root, str(sensor), feature)
        if os.path.isdir(d):
            out.append(d)
            continue
        sroot = os.path.join(root, str(sensor))
        if os.path.isdir(sroot):
            for fn in sorted(os.listdir(sroot)):
                if os.path.isdir(os.path.join(sroot, fn)) \
                        and feature_code(fn) == target:
                    out.append(os.path.join(sroot, fn))
                    break
    return out


def read_clean_hourly_means(daily_root, sensor, feature, start="", end="",
                            spike_k=5.0, max_spikes=3, gap_fill_hours=24.0,
                            shift_min_days=7, shift_k=2.5, dist_k=20.0,
                            max_dist_outliers=5, max_shifts=5,
                            max_total_removals=5):
    """读取某 (传感器,特征) 的均值序列，返回:
    (plot_hours, plot_means, spike_pts, range_pts, gaps, records, shifts)
      - 已做日期过滤 + 物理范围过滤 + 尖峰清洗（与 per_sensor 同一套逻辑）
      - plot_* 已按 gap_fill_hours 线性填充缺失段（仅用于绘图）
      - spike_pts/range_pts: 被替换尖峰/被剔除异常值的坐标 (时间, 清洗后值)
      - gaps: 缺失段记录 [{起始时间, 结束时间, 缺失小时数}]
      - records: 清洗记录明细（尖峰替代 / 范围外剔除）
      - shifts: 突变段记录（持续高于/低于基线，精确到数据粒度）
    """
    feat_dirs = resolve_feature_dirs(daily_root, sensor, feature)
    if not feat_dirs:
        return [], [], [], [], [], [], []
    hours, means, _, _ = read_hourly_series(feat_dirs)[:4]
    if not hours:
        return [], [], [], [], [], [], []
    keep = [i for i, h in enumerate(hours)
            if (not start or h.date().isoformat() >= start)
            and (not end or h.date().isoformat() <= end)]
    hours = [hours[i] for i in keep]
    means = [means[i] for i in keep]

    records, spike_idx, range_idx = [], [], []
    if hours:
        vrange = feature_range(feature)
        means, records, spike_idx, range_idx = clean_series_value(
            hours, means, f"{sensor}-{feature}", spike_k,
            hour_level=True, vrange=vrange, max_spikes=max_spikes,
            dist_k=dist_k, max_dist_outliers=max_dist_outliers,
            max_total_removals=max_total_removals)
    spike_pts = [(hours[i], means[i]) for i in spike_idx]
    range_pts = [(hours[i], means[i]) for i in range_idx]

    shifts = []
    # 风向(spfx/szfx)是圆形量，没有“偏高/偏低”概念，不做突变段检测
    if (shift_k > 0 and len(hours) >= 24 * (shift_min_days + 2)
            and not _is_direction_feature(feature)):
        shifts = _cap_shifts(
            detect_level_shifts(hours, means, shift_min_days, shift_k),
            max_shifts)
    # 连续异常段(> max_run 点)：不剔除，只在图上标注(精确到数据粒度)
    if hours and not _is_direction_feature(feature):
        block_k = dist_k if dist_k > 0 else DEFAULT_DIST_K
        blocks = detect_deviation_blocks(hours, means, k=block_k)
        if blocks:
            shifts = _cap_shifts(shifts + blocks, max_shifts)

    plot_hours, plot_means = hours, means
    gaps = []
    if gap_fill_hours > 0 and plot_hours:
        (plot_hours, plot_means, _, _), gaps = fill_long_gaps(
            hours, means, means, means, gap_fill_hours)
    return plot_hours, plot_means, spike_pts, range_pts, gaps, records, shifts


def _copy_per_sensor_charts(charts_dir, sensor, feature, out_dir, sub_dir="",
                            fallback_charts_dir=""):
    """把 per_sensor 图(图库/<编号>/<特征>/时间序列图.png 等)复制到合并目录。
    sub_dir 非空时复制到 out_dir/<sub_dir>/（单传感器多特征场景）。
    返回复制成功的文件数。"""
    src_dir = os.path.join(charts_dir, str(sensor), feature)
    if not os.path.isdir(src_dir) and fallback_charts_dir:
        # 新图库目录没有 per_sensor 图时，从旧图库回退复制
        src_dir = os.path.join(fallback_charts_dir, str(sensor), feature)
    if not os.path.isdir(src_dir):
        return 0
    dest = os.path.join(out_dir, sub_dir) if sub_dir else out_dir
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for fname in ("时间序列图.png", "频率分布图.png"):
        src = os.path.join(src_dir, fname)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(dest, fname))
                copied += 1
            except OSError as exc:
                print(f"[警告] 复制 per_sensor 图失败 {src}: {exc}", flush=True)
    return copied


def _build_merged_series(daily_root, gf_pairs, start, end, spike_threshold,
                         max_spikes, gap_fill_hours, shift_min_days,
                         shift_threshold, dist_k=20.0,
                         max_dist_outliers=5, max_shifts=5,
                         max_total_removals=5):
    """读取组内各 (传感器,特征) 的清洗后小时序列（含缺失/突变/替换标记）。"""
    series = []
    uniq_sensors = {s for s, _ in gf_pairs}
    uniq_feats = {f for _, f in gf_pairs}
    for sensor, feat in gf_pairs:
        spike_k = (0 if _is_direction_feature(feat)
                   else spike_threshold)
        (hours, means, spike_pts, range_pts, gaps, records, shifts) = \
            read_clean_hourly_means(
                daily_root, sensor, feat, start, end, spike_k, max_spikes,
                gap_fill_hours, shift_min_days, shift_threshold, dist_k,
                max_dist_outliers, max_shifts, max_total_removals)
        if not hours:
            continue
        if len(uniq_sensors) == 1:
            label = feat if len(uniq_feats) > 1 else sensor
        elif len(uniq_feats) == 1:
            label = sensor
        else:
            label = f"{sensor}-{feat}"
        series.append({
            "label": label, "feature": feat, "sensor": sensor,
            "hours": hours, "means": means,
            "spike_pts": spike_pts, "range_pts": range_pts,
            "gaps": gaps, "records": records, "shifts": shifts,
        })
    return series


def _build_merged_daily_charts(args, pos, g, gf_pairs, out_dir, issues):
    """振动(秒级)合并图：按天生成 时间序列图_日期.png(横轴 0~24 小时，
    标题含年月日)；频率分布图按季度逐日累积；每天只加载当天数据。"""
    try:
        os.makedirs(out_dir, exist_ok=True)
        day_set = set()
        for sensor, feat in gf_pairs:
            for feat_dir in resolve_feature_dirs(
                    args.daily_root, sensor, feat):
                if not os.path.isdir(feat_dir):
                    continue
                for fn in os.listdir(feat_dir):
                    if not fn.lower().endswith(".csv"):
                        continue
                    d = fn[:-4]
                    if ((not args.start or d >= args.start)
                            and (not args.end or d <= args.end)):
                        day_set.add(d)
        days = sorted(day_set)
        if not days:
            issues.append(f"无数据: {pos}/{g}")
            return
        uniq_sensors = {s for s, _ in gf_pairs}
        uniq_feats = {f for _, f in gf_pairs}
        hist_bins = np.linspace(-1000.0, 1000.0, 101)
        hist_acc = {}
        all_records = []
        chart_series = None
        chart_day = ""
        best_total_secs = -1
        for day in days:
            series = []
            day_total_secs = 0
            for sensor, feat in gf_pairs:
                path = ""
                for feat_dir in resolve_feature_dirs(
                        args.daily_root, sensor, feat):
                    cand = os.path.join(feat_dir, day + ".csv")
                    if os.path.isfile(cand):
                        path = cand
                        break
                if not path:
                    continue
                hours_d, means_d, maxs_d, mins_d, secs, miss = \
                    read_daily_file(path)
                if not hours_d:
                    continue
                vrange = feature_range(feat)
                spike_k = (0 if _is_direction_feature(feat)
                           else args.spike_threshold)
                means_d, recs, ix, rx = clean_series_value(
                    hours_d, means_d, f"{sensor}-{feat}", spike_k,
                    hour_level=True, vrange=vrange,
                    max_spikes=args.max_spikes, dist_k=args.dist_k,
                    max_dist_outliers=args.max_dist_outliers,
                    max_total_removals=args.max_removals)
                shifts_d = []
                if not _is_direction_feature(feat):
                    block_k = (args.dist_k if args.dist_k > 0
                               else DEFAULT_DIST_K)
                    blocks = detect_deviation_blocks(
                        hours_d, means_d, k=block_k, min_points=60)
                    if blocks:
                        shifts_d = _cap_shifts(blocks, args.max_shifts)
                gaps_d = []
                if len(hours_d) > 1:
                    for a in range(len(hours_d) - 1):
                        gaph = (hours_d[a + 1] - hours_d[a]
                                ).total_seconds() / 3600.0
                        if gaph > 5.0 / 60.0:
                            gaps_d.append({
                                "起始时间": hours_d[a].strftime(
                                    "%Y-%m-%d %H:%M"),
                                "结束时间": hours_d[a + 1].strftime(
                                    "%Y-%m-%d %H:%M"),
                                "缺失小时数": round(gaph, 3),
                            })
                if len(uniq_sensors) == 1:
                    label = feat if len(uniq_feats) > 1 else sensor
                elif len(uniq_feats) == 1:
                    label = sensor
                else:
                    label = f"{sensor}-{feat}"
                series.append({
                    "label": label, "feature": feat, "sensor": sensor,
                    "hours": hours_d, "means": means_d,
                    "spike_pts": [(hours_d[i], means_d[i]) for i in ix],
                    "range_pts": [(hours_d[i], means_d[i]) for i in rx],
                    "gaps": gaps_d, "records": recs, "shifts": shifts_d,
                })
                day_total_secs += secs
                cnts, _ = np.histogram(means_d, bins=hist_bins)
                key = (sensor, feat)
                hist_acc[key] = hist_acc.get(key, 0) + cnts
                all_records.append({
                    "日期": day, "传感器": sensor, "特征": feat,
                    "清洗记录": recs, "数据缺失时段": gaps_d,
                    "突变区间": shifts_d,
                })
            # 选定出图日：--vibration-date 优先，否则取有效秒数最多的一天
            if series and (args.vibration_date == day
                           or (not args.vibration_date
                               and day_total_secs > best_total_secs)):
                best_total_secs = day_total_secs
                chart_series = series
                chart_day = day
        if chart_series:
            plot_group_time_series(
                pos, g, chart_series,
                os.path.join(out_dir, "时间序列图.png"),
                dpi=args.dpi, day_mode=True)
        if hist_acc:
            plot_group_histogram_from_counts(
                pos, g, hist_acc, hist_bins,
                os.path.join(out_dir, "频率分布图.png"), dpi=args.dpi)
        with open(os.path.join(out_dir, "预处理记录.json"), "w",
                  encoding="utf-8") as f:
            json.dump({
                "位置": pos,
                "特征组": g,
                "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "出图日期": chart_day,
                "记录": all_records,
            }, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"合并图错误: {pos}/{g}: {exc}")
        print(f"[警告] 合并图失败 {pos}/{g}: {exc}", flush=True)


def plot_group_time_series(position, group, series, out_path, dpi=200,
                           day_mode=False):
    """同位置同特征组的时间序列图。面板数 ≤6 时一张图；超过 6 个拆成
    多张图(时间序列图.png / _2.png / _3.png ...)，每张 ≤6 个子图，
    避免子图过小、标注看不清，便于插入报告。"""
    n = len(series)
    if n == 0:
        return
    feats = []
    for s in series:
        if s["feature"] not in feats:
            feats.append(s["feature"])
    single_feat = len(feats) == 1
    if single_feat:
        panels = [(s.get("sensor") or s["label"], [s]) for s in series]
    else:
        panels = [(f, [s for s in series if s["feature"] == f])
                  for f in feats]
    max_panels = 6
    chunks = [panels[i:i + max_panels]
              for i in range(0, len(panels), max_panels)]
    for ci, chunk in enumerate(chunks):
        if ci == 0:
            out = out_path
        else:
            base, ext = os.path.splitext(out_path)
            out = f"{base}_{ci + 1}{ext}"
        _plot_group_time_series_one(position, group, chunk, out, dpi, day_mode,
                                    ci + 1, len(chunks))


def _plot_group_time_series_one(position, group, panels, out_path, dpi=200,
                                day_mode=False, chunk_idx=1, chunk_total=1):
    """同位置同特征组的时间序列图（子图布局，保证清晰度）：
      - 单特征多测点：每个传感器一个子图，标题带传感器编号；
      - 多特征：每个特征一个子图，子图内叠加各测点；
      - 缺失时段橙色着色 + 文字“传感器XX缺失(起~止 数据)”（支持跨天）；
      - 长时间突变段红/绿着色 + 文字“均值xx 偏高/偏低(起~止)”。
      day_mode=True 时横轴改为 0~24 小时(秒级振动按天出图)，标题含日期。"""
    n = len(panels)
    single_feat = len({sub[0]["feature"] for _, sub in panels}) == 1
    # 单传感器 X/Y/Z 等轴/方向分量(如 GNSS Δx/Δy/Δz)：三行一列竖排，
    # 每个子图占满整幅宽度(约 9.5 英寸/行)，上下留白最小化
    row3 = _is_axis_triple(
        [(sub[0].get("sensor"), sub[0]["feature"]) for _, sub in panels])
    if row3:
        ncols = 1
        panel_h = 3.4
        panel_w = 9.5
    elif n <= 3:
        ncols = 1
        panel_w = 9.5
        # 2/3 个子图两/三行一列竖排，行高给足避免被裁成横版
        panel_h = 4.6 if n == 2 else 3.4
    else:
        ncols = 2
        panel_w = 9.5
        panel_h = 5.0
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(panel_w * ncols, panel_h * nrows + 1.5),
                             squeeze=False)
    axes = axes.reshape(-1)
    colors = plt.cm.tab10.colors + plt.cm.Set2.colors
    global_handles = []
    global_labels = []
    margin_labels = []    # [(ax_index, label, color, xv, yv)] 多段时画到右侧留白

    for pi, (ptitle, sub) in enumerate(panels):
        ax = axes[pi]
        # 缺失/突变段全部着色并收集文字(带对应色带位置)
        any_gap = any(s["gaps"] for s in sub)
        any_shift = any(s.get("shifts") for s in sub)
        any_zero = False
        panel_labels = []   # [(label, color)]
        panel_pos = []      # [(label, color, xv, yv)] 与 panel_labels 同步
        for i, s in enumerate(sub):
            plot_label = s["label"]
            bucket = _series_bucket_seconds(s["hours"])
            if bucket != 3600:
                plot_label = f"{plot_label}（{_bucket_label(bucket)}）"
            plot_means = (_unwrap_circular(s["means"])
                          if _is_direction_feature(s["feature"])
                          else s["means"])
            if day_mode:
                t0 = s["hours"][0].replace(hour=0, minute=0, second=0,
                                           microsecond=0)
                xs = [(t - t0).total_seconds() / 3600.0 for t in s["hours"]]
                day_date = t0.date().isoformat()
            else:
                xs = s["hours"]
                day_date = ""
            ax.plot(xs, plot_means, "-", linewidth=1.3,
                    color=colors[i % len(colors)], label=plot_label)
            for g in s["gaps"]:
                try:
                    t0 = dt.datetime.strptime(g["起始时间"], "%Y-%m-%d %H:%M")
                    t1 = dt.datetime.strptime(g["结束时间"], "%Y-%m-%d %H:%M")
                except (ValueError, KeyError):
                    continue
                a = min(range(len(s["hours"])),
                        key=lambda k: abs((s["hours"][k] - t0).total_seconds()))
                b = min(range(len(s["hours"])),
                        key=lambda k: abs((s["hours"][k] - t1).total_seconds()))
                ax.axvspan(xs[a], xs[b],
                           color="#ff7f0e", alpha=0.18)
                panel_labels.append(
                    (_fmt_compact_range(g['起始时间'], g['结束时间']),
                     "#d2691e"))
                yv = (plot_means[b] if b < len(plot_means)
                      else plot_means[a] if a < len(plot_means) else 0.0)
                panel_pos.append(
                    (_fmt_compact_range(g['起始时间'], g['结束时间']),
                     "#d2691e", xs[a], yv))
            for sh in s.get("shifts") or []:
                try:
                    t0 = dt.datetime.strptime(sh["起始时间"], "%Y-%m-%d %H:%M")
                    t1 = dt.datetime.strptime(sh["结束时间"], "%Y-%m-%d %H:%M")
                except (ValueError, KeyError):
                    continue
                a = min(range(len(s["hours"])),
                        key=lambda k: abs((s["hours"][k] - t0).total_seconds()))
                b = min(range(len(s["hours"])),
                        key=lambda k: abs((s["hours"][k] - t1).total_seconds()))
                color = "#d62728" if sh["方向"] == "偏高" else "#2ca02c"
                ax.axvspan(xs[a], xs[b],
                           color=color, alpha=0.12)
                panel_labels.append(
                    (f"{_fmt_compact_range(sh['起始时间'], sh['结束时间'])} "
                     f"{sh['方向']}", color))
                yv = (plot_means[b] if b < len(plot_means)
                      else plot_means[a] if a < len(plot_means) else 0.0)
                panel_pos.append(
                    (f"{_fmt_compact_range(sh['起始时间'], sh['结束时间'])} "
                     f"{sh['方向']}", color, xs[a], yv))
            # 连续恒 0 超过阈值(普通 24h，0 为正常值的特征一周)：
            # 紫色色带 + “编号(时间段)可能故障”
            if not day_mode:
                for z in detect_zero_runs(
                        s["hours"], s["means"],
                        min_hours=zero_min_hours(s["feature"])):
                    try:
                        t0 = dt.datetime.strptime(
                            z["起始时间"], "%Y-%m-%d %H:%M")
                        t1 = dt.datetime.strptime(
                            z["结束时间"], "%Y-%m-%d %H:%M")
                        a = min(range(len(s["hours"])),
                                key=lambda k: abs(
                                    (s["hours"][k] - t0).total_seconds()))
                        b = min(range(len(s["hours"])),
                                key=lambda k: abs(
                                    (s["hours"][k] - t1).total_seconds()))
                    except (ValueError, KeyError):
                        continue
                    ax.axvspan(xs[a], xs[b], color="#9467bd", alpha=0.16)
                    zrange = _fmt_compact_range(z['起始时间'], z['结束时间'])
                    zlabel = f"{plot_label}({zrange})可能故障"
                    panel_labels.append((zlabel, "#9467bd"))
                    yv = (plot_means[b] if b < len(plot_means)
                          else plot_means[a] if a < len(plot_means) else 0.0)
                    panel_pos.append((zlabel, "#9467bd", xs[a], yv))
                    any_zero = True
            if s["spike_pts"]:
                ax.plot([p[0] for p in s["spike_pts"]],
                        [p[1] for p in s["spike_pts"]],
                        "x", color="black", markersize=9, mew=2.2, zorder=5)
            if s["range_pts"]:
                ax.plot([p[0] for p in s["range_pts"]],
                        [p[1] for p in s["range_pts"]],
                        "x", color="#d62728", markersize=10, mew=2.2, zorder=5)

        # 标注: ≤4 段标在带上(带间距近自动让位)；>4 段全部挪到图旁留白
        if panel_labels:
            y0, y1 = ax.get_ylim()
            if len(panel_labels) <= 4:
                _label_on_bands(ax, fig, panel_pos, fontsize=13)
            else:
                for item in panel_pos:
                    margin_labels.append((pi, *item))

        if day_mode:
            ax.set_xticks(range(0, 25, 6))
            ax.set_xticklabels([f"{h}时" for h in range(0, 25, 6)],
                               fontsize=13)
            ax.set_xlim(0, 24)
            ax.set_xlabel("时刻（小时）", fontsize=14)
        all_hours = sorted({h for s in sub for h in s["hours"]})
        if all_hours and not day_mode:
            day_starts = {}
            for h in all_hours:
                day_starts.setdefault(h.date().isoformat(), h)
            days = sorted(day_starts)
            step = max(1, len(days) // 12)
            ticks = [day_starts[d] for d in days[::step]]
            ax.set_xticks(ticks)
            ax.set_xticklabels([_fmt_cn_date(d) for d in days[::step]],
                               rotation=30, fontsize=13)
        if day_mode:
            ax.set_title(
                f"{ptitle}｜{feature_display(sub[0]['feature'])}｜"
                f"{_fmt_cn_date(day_date)}", fontsize=15)
        elif row3:
            # 一行三列时子图标题直接用分量名(如 GNSS(Δx))，整图标题带位置
            ax.set_title(feature_display(sub[0]["feature"]), fontsize=15)
        elif single_feat:
            ax.set_title(f"{ptitle}｜{feature_display(sub[0]['feature'])}",
                         fontsize=15)
        else:
            ax.set_title(feature_display(ptitle), fontsize=15)
        ax.set_ylabel(feature_display(sub[0]["feature"]) if single_feat
                      else feature_display(ptitle), fontsize=14)
        ax.tick_params(axis="y", labelsize=12)
        ax.grid(True, alpha=0.3)

        handles, labels = ax.get_legend_handles_labels()
        handles += [plt.Line2D([], [], marker="x", color="black",
                               linestyle="None", markersize=7, mew=1.8),
                    plt.Line2D([], [], marker="x", color="#d62728",
                               linestyle="None", markersize=8, mew=1.8)]
        labels += ["已替换尖峰(统计)", "已剔除异常值(范围外)"]
        if any_gap:
            handles.append(plt.Rectangle((0, 0), 1, 1, facecolor="#ff7f0e",
                                         alpha=0.35))
            labels.append("数据缺失(已插值填充)")
        if any_shift:
            handles.append(plt.Rectangle((0, 0), 1, 1, facecolor="#d62728",
                                         alpha=0.25))
            labels.append("长时间偏高")
            handles.append(plt.Rectangle((0, 0), 1, 1, facecolor="#2ca02c",
                                         alpha=0.25))
            labels.append("长时间偏低")
        if any_zero:
            handles.append(plt.Rectangle((0, 0), 1, 1, facecolor="#9467bd",
                                         alpha=0.25))
            labels.append("可能故障(恒0超过24h)")
        # 子图不画图例(避免遮挡数据)；统一收集到整图底部一个全局图例
        for h, lb in zip(handles, labels):
            if lb not in global_labels:
                global_labels.append(lb)
                global_handles.append(h)

    for j in range(len(panels), len(axes)):
        axes[j].axis("off")

    _chunk_txt = f"（第 {chunk_idx}/{chunk_total} 张）" if chunk_total > 1 else ""
    if day_mode:
        fig.suptitle(
            f"{position}｜{group} 小时均值时间序列（{n} 个测点）｜"
            f"{_fmt_cn_date(day_date)}{_chunk_txt}", fontsize=19)
    else:
        fig.suptitle(
            f"{position}｜{group} 小时均值时间序列（{n} 个测点）{_chunk_txt}",
            fontsize=19)
    if global_handles:
        fig.legend(global_handles, global_labels, loc="lower center",
                   ncol=min(len(global_labels), 8), fontsize=12,
                   frameon=True, borderaxespad=0.3)
        fig.tight_layout(rect=(0, 0.09, 1, 0.95))
    else:
        fig.tight_layout(rect=(0, 0, 1, 0.97))
    # 多段标注: 收缩子图宽度，把右侧留白区让出来放文字
    if margin_labels:
        for a in axes:
            x0, y0, w, h = a.get_position().bounds
            a.set_position([x0, y0, w * 0.78, h])
        by_ax = {}
        for pi, label, color, xv, yv in margin_labels:
            by_ax.setdefault(pi, []).append((label, color, xv, yv))
        for pi, items in by_ax.items():
            _label_in_margin(fig, axes[pi].get_position(), items,
                             fontsize=12)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_group_histogram(position, group, series, out_path, dpi=200):
    """频率分布图：每个测点(传感器-特征)一个子图，不再叠加。
    超过 6 个测点时拆成多张图(频率分布图.png / _2.png / ...)。"""
    n = len(series)
    if n == 0:
        return
    max_panels = 6
    chunks = [series[i:i + max_panels]
              for i in range(0, n, max_panels)]
    for ci, chunk in enumerate(chunks):
        if ci == 0:
            out = out_path
        else:
            base, ext = os.path.splitext(out_path)
            out = f"{base}_{ci + 1}{ext}"
        _plot_group_histogram_chunk(position, group, chunk, out, dpi,
                                    ci + 1, len(chunks))


def _plot_group_histogram_chunk(position, group, series, out_path, dpi=200,
                                chunk_idx=1, chunk_total=1):
    """频率分布图单个图块：每个测点(传感器-特征)一个子图。"""
    n = len(series)
    if n == 0:
        return
    # 2/3 个子图一律单列竖排(两行/三行一列)，4 个以上两列网格，
    # 1 个即主图；子图占满整幅宽度，避免列数多导致挤压
    cols = 1 if n <= 3 else 2
    rows = (n + cols - 1) // cols
    row_h = 4.6 if n <= 3 else 3.8
    fig, axes = plt.subplots(
        rows, cols,
        figsize=((7.5, row_h * rows) if n <= 3 else (7.5 * cols, 3.8 * rows)))
    axes = np.array(axes).reshape(-1)
    colors = plt.cm.tab10.colors + plt.cm.Set2.colors
    for i, s in enumerate(series):
        ax = axes[i]
        ax.hist(s["means"], bins=min(40, max(10, len(s["means"]) // 5)),
                density=True, alpha=0.7, color=colors[i % len(colors)],
                edgecolor="black")
        ax.set_title(f"{s['label']}｜{feature_display(s['feature'])}",
                     fontsize=12)
        ax.set_xlabel("数值", fontsize=11)
        ax.set_ylabel("频率", fontsize=11)
        ax.grid(True, alpha=0.3)
    for j in range(n, len(axes)):
        axes[j].axis("off")
    _chunk_txt = f"（第 {chunk_idx}/{chunk_total} 张）" if chunk_total > 1 else ""
    fig.suptitle(
        f"{position}｜{group} 频率分布直方图（{n} 个测点）{_chunk_txt}",
        fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_group_histogram_from_counts(position, group, hist_acc, bin_edges,
                                     out_path, dpi=200):
    """振动(秒级)合并频率分布图：每个 (传感器,特征) 一个子图，计数按季度逐日累积。
    超过 6 个测点时拆成多张图。"""
    keys = sorted(hist_acc)
    if not keys:
        return
    max_panels = 6
    chunks = [keys[i:i + max_panels]
              for i in range(0, len(keys), max_panels)]
    for ci, chunk in enumerate(chunks):
        if ci == 0:
            out = out_path
        else:
            base, ext = os.path.splitext(out_path)
            out = f"{base}_{ci + 1}{ext}"
        sub_acc = {k: hist_acc[k] for k in chunk}
        _plot_group_histogram_from_counts_chunk(
            position, group, sub_acc, bin_edges, out, dpi, ci + 1, len(chunks))


def _plot_group_histogram_from_counts_chunk(position, group, hist_acc,
                                            bin_edges, out_path, dpi=200,
                                            chunk_idx=1, chunk_total=1):
    """振动(秒级)合并频率分布图单个图块。"""
    keys = sorted(hist_acc)
    if not keys:
        return
    n = len(keys)
    # 2/3 个子图一律单列竖排，4 个以上两列网格，1 个即主图
    cols = 1 if n <= 3 else 2
    rows = (n + cols - 1) // cols
    row_h = 4.6 if n <= 3 else 3.8
    fig, axes = plt.subplots(
        rows, cols,
        figsize=((7.5, row_h * rows) if n <= 3 else (7.5 * cols, 3.8 * rows)),
        squeeze=False)
    axes = np.array(axes).reshape(-1)
    centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    widths = np.diff(bin_edges)
    colors = plt.cm.tab10.colors
    for i, (sensor, feat) in enumerate(keys):
        ax = axes[i]
        counts = hist_acc[(sensor, feat)]
        total = float(counts.sum()) or 1.0
        ax.bar(centers, counts / (total * widths), width=widths,
               color=colors[i % len(colors)], alpha=0.75,
               edgecolor="black", linewidth=0.4)
        ax.set_title(f"{sensor}｜{feature_display(feat)}",
                     fontsize=12)
        ax.set_xlabel("数值", fontsize=11)
        ax.set_ylabel("频率密度", fontsize=11)
        ax.grid(True, alpha=0.3)
    for j in range(n, len(axes)):
        axes[j].axis("off")
    _chunk_txt = f"（第 {chunk_idx}/{chunk_total} 张）" if chunk_total > 1 else ""
    fig.suptitle(
        f"{position}｜{group} 频率分布直方图（{n} 个测点，按日累积）{_chunk_txt}",
        fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_group_correlation(position, group, series, out_path, dpi=200):
    """组内特征两两相关：只有一对时独立成一张图，多对时用子图网格。"""
    feat_series = defaultdict(list)
    for s in series:
        feat_series[s["feature"]].extend(zip(s["hours"], s["means"]))
    feats = sorted(feat_series)
    pairs = [(a, b) for i, a in enumerate(feats) for b in feats[i + 1:]]
    if not pairs:
        return
    single = len(pairs) == 1
    if single:
        fig, ax0 = plt.subplots(1, 1, figsize=(9, 6.5))
        axes = np.array([ax0])
    else:
        cols = 2
        rows = (len(pairs) + cols - 1) // cols
        fig, axes = plt.subplots(rows, cols, figsize=(14, 5.6 * rows))
        axes = np.array(axes).reshape(-1)
    for k, (a, b) in enumerate(pairs):
        ax = axes[k]
        da = dict(feat_series[a])
        db = dict(feat_series[b])
        xs = [da[t] for t in da if t in db]
        ys = [db[t] for t in da if t in db]
        if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
            ax.axis("off")
            continue
        slope, intercept = np.polyfit(xs, ys, 1)
        r = np.corrcoef(xs, ys)[0, 1]
        ax.scatter(xs, ys, s=7, alpha=0.5, color="#4c72b0")
        xq = np.linspace(min(xs), max(xs), 100)
        ax.plot(xq, slope * xq + intercept, "k:", linewidth=1.3)
        ax.set_title(f"{a} vs {b}　r = {r:.4f}", fontsize=13)
        ax.grid(True, alpha=0.3)
    if not single:
        for j in range(len(pairs), len(axes)):
            axes[j].axis("off")
    fig.suptitle(f"{position}｜{group} 特征相关性分析", fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_position_correlation(position, series, pos_dir, dpi=200):
    """位置内跨特征相关性散点图（如 结构应变-温度）。

    同一测点(传感器)同时有两种特征时按传感器编号配对；
    不同传感器承载不同特征时按测点顺序配对（测点1 温度 ↔ 测点1 应变）。
    每个特征对一张图，图内每个测点一个子图，保存为
    图库/<位置>/相关性_<特征A>-<特征B>.png。
    """
    feat_sensors = defaultdict(list)  # feature -> [(sensor, hours, means)]
    for s in series:
        feat_sensors[s["feature"]].append(
            (s["sensor"], s["hours"], s["means"]))
    feats = sorted(feat_sensors)
    pairs = [(a, b) for i, a in enumerate(feats) for b in feats[i + 1:]]
    if not pairs:
        return
    os.makedirs(pos_dir, exist_ok=True)

    for a, b in pairs:
        da_by_sid = {sid: dict(zip(hs, ms))
                     for sid, hs, ms in feat_sensors[a]}
        db_by_sid = {sid: dict(zip(hs, ms))
                     for sid, hs, ms in feat_sensors[b]}
        # 1) 优先：同一传感器同时有两种特征 -> 按传感器配对
        panels = []
        for sid in da_by_sid:
            if sid in db_by_sid:
                panels.append((sid, da_by_sid[sid], db_by_sid[sid]))
        # 2) 否则：不同传感器 -> 按测点顺序配对
        if not panels:
            def _key(x):
                return (int(x) if x.isdigit() else x)
            sa = sorted(feat_sensors[a], key=lambda t: _key(t[0]))
            sb = sorted(feat_sensors[b], key=lambda t: _key(t[0]))
            for k, (t1, t2) in enumerate(zip(sa, sb), 1):
                panels.append((f"测点{k}",
                               dict(zip(t1[1], t1[2])),
                               dict(zip(t2[1], t2[2]))))
        valid = []
        for label, da, db in panels:
            xs = [da[t] for t in da if t in db]
            ys = [db[t] for t in da if t in db]
            if len(xs) >= 3 and len(set(xs)) >= 2 and len(set(ys)) >= 2:
                valid.append((label, xs, ys))
        if not valid:
            continue

        single = len(valid) == 1
        if single:
            fig, ax0 = plt.subplots(1, 1, figsize=(9, 6.5))
            axes = np.array([ax0])
        else:
            cols = 2
            rows = (len(valid) + cols - 1) // cols
            fig, axes = plt.subplots(rows, cols, figsize=(14, 4.5 * rows))
            axes = np.array(axes).reshape(-1)
        for k, (label, xs, ys) in enumerate(valid):
            ax = axes[k]
            slope, intercept = np.polyfit(xs, ys, 1)
            r = np.corrcoef(xs, ys)[0, 1]
            ax.scatter(xs, ys, s=7, alpha=0.5, color="#4c72b0")
            xq = np.linspace(min(xs), max(xs), 100)
            ax.plot(xq, slope * xq + intercept, "k:", linewidth=1.3)
            ax.set_title(f"{label}　r = {r:.4f}", fontsize=13)
            ax.set_xlabel(feature_display(a), fontsize=11)
            ax.set_ylabel(feature_display(b), fontsize=11)
            ax.grid(True, alpha=0.3)
        if not single:
            for j in range(len(valid), len(axes)):
                axes[j].axis("off")
        fig.suptitle(
            f"{position}｜{feature_display(a)}-{feature_display(b)} "
            f"相关性散点图", fontsize=17)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        out = os.path.join(pos_dir, f"相关性_{a}-{b}.png")
        fig.savefig(out, dpi=dpi, bbox_inches="tight", facecolor="white")
        plt.close(fig)


def _derive_period_tag(start="", end="", daily_root=""):
    """推导“年份.月份范围”标签，如 2026.1~3、2025.12~2026.3。

    优先级：--start/--end > daily 目录文件名。
    无法推导时返回空字符串（目录名回退为不带日期的“图库/统计值”）。
    """
    def _parse(s):
        try:
            return dt.datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            return None

    d0 = d1 = None
    if start and end:
        d0, d1 = _parse(start), _parse(end)
    if not d0 or not d1:
        dates = []
        if daily_root and os.path.isdir(daily_root):
            try:
                for sensor_dir in os.listdir(daily_root):
                    sd = os.path.join(daily_root, sensor_dir)
                    if not os.path.isdir(sd):
                        continue
                    for feat_dir in os.listdir(sd):
                        fd = os.path.join(sd, feat_dir)
                        if not os.path.isdir(fd):
                            continue
                        for fn in os.listdir(fd):
                            if fn.endswith(".csv"):
                                p = _parse(fn[:-4])
                                if p:
                                    dates.append(p)
                    if len(dates) > 50000:
                        break
            except Exception:
                pass
        if dates:
            d0, d1 = min(dates), max(dates)
    if not d0 or not d1:
        return ""
    if d0.year == d1.year:
        return f"{d0.year}.{d0.month}~{d1.month}"
    return f"{d0.year}.{d0.month}~{d1.year}.{d1.month}"


def _copy_per_sensor_dirs(lib_root, chart_dir, bridge=""):
    """把旧图库的 per_sensor 数字文件夹(如 184/DZJSD(xJsd))复制到新图库目录(缺则补)。

    振动/应变等章节按显式传感器编号出图，需要 per_sensor 图；新目录合并图之外
    补一份，保证带年月目录自包含、报告全量命中。
    """
    old_charts = os.path.join(lib_root, "图库", bridge) if bridge \
        else os.path.join(lib_root, "图库")
    if not os.path.isdir(old_charts):
        return 0
    copied = 0
    for name in os.listdir(old_charts):
        if not name.isdigit():
            continue
        s = os.path.join(old_charts, name)
        d = os.path.join(chart_dir, name)
        if os.path.isdir(s) and not os.path.isdir(d):
            try:
                shutil.copytree(s, d)
                copied += 1
            except OSError as exc:
                print(f"[警告] 复制 per_sensor 目录失败 {s}: {exc}", flush=True)
    return copied


def _write_status_dirs(lib_root, chart_dir, stats_dir, update_charts=True):
    """把本期实际目录写入 <lib_root>/status.json 的 dirs（保留其它字段）。"""
    status_path = os.path.join(lib_root, "status.json")
    try:
        status = {}
        if os.path.isfile(status_path):
            with open(status_path, "r", encoding="utf-8") as f:
                status = json.load(f)
        status.setdefault("dirs", {})
        if update_charts:
            status["dirs"]["charts"] = os.path.abspath(chart_dir)
        status["dirs"]["stats"] = os.path.abspath(stats_dir)
        status["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[警告] 写 status.json 失败: {exc}")


def main():
    ap = argparse.ArgumentParser(description="图库 + 统计值生成(集成版)")
    ap.add_argument("--mode", choices=["merged", "per_sensor"], default="merged",
                    help="merged=按监测部位合并出图(多传感器/多特征一张图，默认)；"
                         "per_sensor=旧的按传感器出图")
    ap.add_argument("--position-map", default="",
                    help="传感器名称对照 json 路径(默认扫描 "
                         "传感器对照/传感器名称对照/*.json)")
    ap.add_argument("--dpi", type=int, default=200,
                    help="图片分辨率(默认 200，保证插入 Word 清晰)")
    ap.add_argument("--daily-root", default=None,
                    help="预处理输出的 daily 目录(默认由 --year/--quarter 或 "
                         "DEFAULT_DAILY_ROOT 推导)")
    ap.add_argument("--year", type=int, default=0,
                    help="年份(如 2026)，与 --quarter 一起自动推导 daily 目录/期号/日期范围")
    ap.add_argument("--quarter", type=int, default=0, choices=[1, 2, 3, 4],
                    help="季度(1~4)，与 --year 一起使用")
    ap.add_argument("--lib-root", default=DEFAULT_LIB_ROOT,
                    help="图库/统计值 的上级目录")
    ap.add_argument("--charts-dir", default="",
                    help="图库输出目录(默认 <lib-root>/图库)")
    ap.add_argument("--stats-dir", default="",
                    help="统计值输出目录(默认 <lib-root>/统计值)")
    ap.add_argument("--sensor-map", default="",
                    help="传感器编号名称.json 路径(默认 "
                         "传感器对照/传感器编号名称.json)")
    ap.add_argument("--period-tag", default="",
                    help="目录名里的年月标签(如 2026.1~3)；默认自动从数据范围推导")
    ap.add_argument("--bridge", default="",
                    help="大桥名称(如 赤石)；图库/统计值挂到 图库_<期>/<桥名>、"
                         "统计值_<期>/<桥名> 下，daily 根目录取 "
                         "<daily根>/<桥名>/daily_<期>。不填时按传感器对照表自动推导")
    ap.add_argument("--features", default="",
                    help="只生成指定特征(逗号分隔，如 DZJSD(xJsd),YB(rsg))；"
                         "留空=全部。用于对不满意的特征选择性重新生成")
    ap.add_argument("--vibration-date", default="",
                    help="振动(秒级)出图日期 YYYY-MM-DD；不指定时自动取"
                         "数据最完整/最新的一天(只出一张时间序列图)")
    ap.add_argument("--correlation", action="store_true",
                    help="同时生成同传感器不同特征间的相关性分析图")
    ap.add_argument("--limit-sensors", type=int, default=0,
                    help="只处理前 N 个传感器(试跑用)")
    ap.add_argument("--spike-threshold", type=float, default=5.0,
                    help="尖峰检测阈值(偏离稳健基线多少个MAD，0=关闭尖峰清洗)")
    ap.add_argument("--max-spikes", type=int, default=3,
                    help="每个序列最多替代几个尖峰(只保留最极端的，默认3)")
    ap.add_argument("--dist-k", type=float, default=20.0,
                    help="分布极端值过滤宽带(稳健尺度倍数，默认20；"
                         "0=关闭，只保留物理范围过滤)")
    ap.add_argument("--max-dist-outliers", type=int, default=5,
                    help="保留参数(兼容旧命令)；现按连续异常段整段剔除，"
                         "一段占 --max-removals 1 个名额，不再按点数限制)")
    ap.add_argument("--max-removals", type=int, default=5,
                    help="分布极端点+零散尖峰合计最多剔除几个(按偏离程度排名，"
                         "默认5；物理范围外/非有限值不计入)")
    ap.add_argument("--max-shifts", type=int, default=5,
                    help="突变区间最多标注几条(按偏离程度排名，默认5)")
    ap.add_argument("--skip-per-sensor", action="store_true",
                    help="跳过逐传感器(per_sensor)出图，直接生成 merged 图"
                         "(单传感器单特征组也会由 merged 路径直接出图)")
    ap.add_argument("--skip-charts", action="store_true",
                    help="跳过图库生成(per_sensor 与 merged 图都不生成)，"
                         "只计算并写出统计库(位置统计/)，用于只刷新统计值")
    ap.add_argument("--skip-stats", action="store_true",
                    help="跳过统计值计算与写出(per_sensor 只出图、不重算统计；"
                         "与 --skip-per-sensor 一起用时逐传感器整体跳过，"
                         "只生成合并图；统计值保留上次结果)")
    ap.add_argument("--gap-fill-hours", type=float, default=24,
                    help="连续缺失达到该小时数时，图上标注并用线性插值"
                         "填充绘图(默认24小时，0=关闭)")
    ap.add_argument("--shift-threshold", type=float, default=2.5,
                    help="突变段检测阈值(偏离基线多少个尺度，0=关闭突变标注)")
    ap.add_argument("--shift-min-days", type=int, default=7,
                    help="突变段最短持续天数")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD(可选)")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD(可选)")
    args = ap.parse_args()

    # --year/--quarter 自动推导：起止日期、期号(daily 根目录在桥名推导后统一拼接)
    if args.year and args.quarter:
        qs = (args.quarter - 1) * 3 + 1
        qe = args.quarter * 3
        start_d = dt.date(args.year, qs, 1)
        end_d = dt.date(args.year, qe, calendar.monthrange(args.year, qe)[1])
        args.start = args.start or start_d.isoformat()
        args.end = args.end or end_d.isoformat()
    tag = args.period_tag or _derive_period_tag(
        args.start, args.end, args.daily_root or DEFAULT_DAILY_ROOT)
    chart_dir = args.charts_dir or (
        os.path.join(args.lib_root, f"图库_{tag}") if tag
        else os.path.join(args.lib_root, "图库"))
    stats_dir = args.stats_dir or (
        os.path.join(args.lib_root, f"统计值_{tag}") if tag
        else os.path.join(args.lib_root, "统计值"))

    map_path = (args.sensor_map
                or os.path.join(DEFAULT_SENSOR_MAP_DIR, "传感器编号名称.json"))
    if not os.path.isfile(map_path):
        # 兼容旧布局：统计值目录里还有旧对照表时回退
        old_map = os.path.join(stats_dir, "传感器编号名称.json")
        if os.path.isfile(old_map):
            map_path = old_map
    sensor_map = load_sensor_map(map_path)
    print(f"传感器名称对照: {'已加载(' + str(len(sensor_map)) + '个)' if sensor_map else '未找到'}")

    # 大桥名称：--bridge 优先，否则按对照表里最常见的桥名自动推导
    bridge = args.bridge or ""
    if not bridge:
        names = [info.get("桥名", "") for info in sensor_map.values()]
        names = [n for n in names if n]
        if names:
            bridge = max(set(names), key=names.count)
    # daily 根目录(绝对路径)；显式 --daily-root 优先
    if not args.daily_root:
        base = os.path.dirname(DEFAULT_DAILY_ROOT.rstrip("/\\"))
        daily_name = os.path.basename(DEFAULT_DAILY_ROOT.rstrip("/\\"))
        yearly = bool(args.year and not args.quarter)
        if args.year and args.quarter:
            daily_name = f"daily_{args.year}.{qs}~{qe}"
        if bridge:
            # 兼容桥名写法差异(湘江特 vs 湘江特大桥)：按目录存在性匹配，
            # 避免预处理用 --bridge 湘江特、建图库用 --bridge 湘江特大桥 时
            # daily 目录对不上
            resolved = False
            for v in _bridge_variants(bridge):
                if yearly:
                    # 年度：daily 根为桥根目录，下面挂各季度 daily_* 子目录
                    cand = os.path.join(base, v)
                    if os.path.isdir(cand) and any(
                            os.path.isdir(os.path.join(cand, d))
                            for d in os.listdir(cand)
                            if d == "daily" or d.startswith("daily_")):
                        bridge = v
                        args.daily_root = cand
                        resolved = True
                        break
                else:
                    cand = os.path.join(base, v, daily_name)
                    if os.path.isdir(cand):
                        bridge = v
                        args.daily_root = cand
                        resolved = True
                        break
            if not resolved:
                args.daily_root = (
                    os.path.join(base, bridge) if yearly
                    else os.path.join(base, bridge, daily_name))
        else:
            args.daily_root = os.path.join(
                base, "" if yearly else daily_name)
    # 年度模式：加载该年全部季度 daily 子目录(daily_2026.1~3 / 4~6 / ...)
    if args.year and not args.quarter:
        root = args.daily_root if isinstance(args.daily_root, str) \
            else args.daily_root[0]
        subdirs = sorted(
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
            and (d == "daily" or d.startswith("daily_")))
        if subdirs:
            args.daily_root = subdirs
        else:
            args.daily_root = [root]
        tag = f"{args.year}.1~12"
        chart_dir = args.charts_dir or (
            os.path.join(args.lib_root, f"图库_{tag}") if tag
            else os.path.join(args.lib_root, "图库"))
        stats_dir = args.stats_dir or (
            os.path.join(args.lib_root, f"统计值_{tag}") if tag
            else os.path.join(args.lib_root, "统计值"))
    if bridge:
        # 图库/统计值：图库_<期>/<桥名>、统计值_<期>/<桥名>。
        # 显式 --charts-dir/--stats-dir 可能已带桥名（web 传的是
        # 图库_<期>/<桥名> 这种），已带时不再重复拼，避免出现
        # 统计值_2026.4~6/洣水河特大桥/洣水河特大桥/位置统计
        def _leaf_has_bridge(path):
            leaf = os.path.basename(os.path.normpath(str(path or "")))
            return bool(leaf) and leaf in _bridge_variants(bridge)
        if not _leaf_has_bridge(chart_dir):
            chart_dir = os.path.join(chart_dir, bridge)
        if not _leaf_has_bridge(stats_dir):
            stats_dir = os.path.join(stats_dir, bridge)
        print(f"大桥名称: {bridge}")
        print(f"daily 数据源: {args.daily_root}")
    if isinstance(args.daily_root, list) and len(args.daily_root) > 1:
        print(f"年度模式: 加载 {len(args.daily_root)} 个季度 daily 目录")
    if not args.skip_charts:
        os.makedirs(chart_dir, exist_ok=True)
    os.makedirs(stats_dir, exist_ok=True)
    if tag:
        print(f"本期年月范围: {tag}")

    # ---------- 选择数据源 ----------
    sensor_feats = discover_sensor_features(args.daily_root)
    if not sensor_feats:
        print(f"[错误] daily 目录为空，无法生成图库（已取消 summary.csv 日级回退）: "
              f"{args.daily_root}")
        sys.exit(1)
    print(f"数据源: daily 目录(小时级明细) {args.daily_root}")

    # 收集 (传感器, 特征) 列表
    pairs = [(s, f) for s, feats in sorted(sensor_feats.items())
             for f in feats]
    if args.features:
        want_traffic = _traffic_selected(args.features)
        pairs = [(s, f) for s, f in pairs
                 if _feature_selected(f, args.features)
                 or (want_traffic and s == TRAFFIC_SENSOR)]
    sensors = sorted({p[0] for p in pairs})
    if args.limit_sensors:
        sensors = sensors[:args.limit_sensors]
        pairs = [p for p in pairs if p[0] in set(sensors)]
    print(f"共发现 {len(sensors)} 个传感器，开始生成图库...")

    t0 = time.time()
    issues = []   # 失败/数据不足记录
    overview = []
    pos_stats = {}   # 位置 -> 传感器编号 -> 特征 -> {统计, 每日统计}
    pos_sensor_order = {}   # 位置 -> 传感器编号列表(按首次出现顺序)
    pos_daily = {}   # 位置 -> 传感器编号 -> 特征 -> (dates, means) 用于应变-温度联合统计
    # 只出合并图(--skip-per-sensor --skip-stats)时，逐传感器统计计算与出图
    # 整体跳过，直接进入合并图库阶段，节省大量读取/统计时间
    _sensor_iter = sensors
    if args.skip_per_sensor and args.skip_stats:
        print("--skip-per-sensor --skip-stats: 跳过逐传感器统计计算与出图，"
              "直接生成合并图")
        _sensor_iter = []
    for idx, sensor in enumerate(_sensor_iter, 1):
        info = sensor_map.get(sensor, {})
        sensor_name = info.get("名称", "") or sensor
        bridge = info.get("桥名", "")
        sensor_stats = {
            "编号": sensor,
            "名称": sensor_name,
            "桥名": bridge,
            "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "特征统计": {},
        }
        # 传感器所在位置(与图库 merge 位置目录一致)；找不到回退到传感器编号
        pos_name = (info.get("监测部位") or info.get("名称") or "").strip() or sensor
        corr_results = {}
        daily_by_feat = {}

        for feature in [f for (s, f) in pairs if s == sensor]:
            try:
                # ---------- 振动(秒级)按天出图：每天只加载当天数据 ----------
                if _is_second_level_feature(feature):
                    day_files = sorted({
                        fn for feat_dir in resolve_feature_dirs(
                            args.daily_root, sensor, feature)
                        for fn in os.listdir(feat_dir)
                        if fn.lower().endswith(".csv")})
                    day_files = [fn for fn in day_files
                                 if (not args.start or fn[:-4] >= args.start)
                                 and (not args.end or fn[:-4] <= args.end)]
                    if not day_files:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    vrange = feature_range(feature)
                    spike_k = (0 if _is_direction_feature(feature)
                               else args.spike_threshold)
                    spike_rec = []
                    shifts_all = []
                    gaps_all = []
                    day_dates, day_means, day_maxs = [], [], []
                    day_mins, day_secs, day_miss = [], [], []
                    native_hours = []
                    native_means, native_maxs, native_mins = [], [], []
                    hist_bins = np.linspace(-1000.0, 1000.0, 101)
                    hist_counts = None
                    chart_day = None          # (日期, hours, means, ix, rx, shifts, gaps)
                    best_secs = -1
                    for fn in day_files:
                        date_str = fn[:-4]
                        path = ""
                        for feat_dir in resolve_feature_dirs(
                                args.daily_root, sensor, feature):
                            cand = os.path.join(feat_dir, fn)
                            if os.path.isfile(cand):
                                path = cand
                                break
                        if not path:
                            continue
                        hours_d, means_d, maxs_d, mins_d, secs, miss = \
                            read_daily_file(path)
                        if not hours_d:
                            continue
                        means_d, r1, ix1, rx1 = clean_series_value(
                            hours_d, means_d, f"{date_str}均值", spike_k,
                            hour_level=True, vrange=vrange,
                            max_spikes=args.max_spikes, dist_k=args.dist_k,
                            max_dist_outliers=args.max_dist_outliers,
                            max_total_removals=args.max_removals)
                        maxs_d, r2, ix2, rx2 = clean_series_value(
                            hours_d, maxs_d, f"{date_str}最大值", spike_k,
                            hour_level=True, vrange=vrange,
                            max_spikes=args.max_spikes, dist_k=args.dist_k,
                            max_dist_outliers=args.max_dist_outliers,
                            max_total_removals=args.max_removals)
                        mins_d, r3, ix3, rx3 = clean_series_value(
                            hours_d, mins_d, f"{date_str}最小值", spike_k,
                            hour_level=True, vrange=vrange,
                            max_spikes=args.max_spikes, dist_k=args.dist_k,
                            max_dist_outliers=args.max_dist_outliers,
                            max_total_removals=args.max_removals)
                        spike_rec += r1 + r2 + r3
                        # 累积清洗后的原生粒度序列（小时级/秒级），
                        # 供全期极值按特征颗粒度计算
                        native_hours += list(hours_d)
                        native_means += list(means_d)
                        native_maxs += list(maxs_d)
                        native_mins += list(mins_d)
                        # 突变段(按天, 秒级至少 1 分钟)
                        shifts_d = []
                        if not _is_direction_feature(feature):
                            block_k = (args.dist_k if args.dist_k > 0
                                       else DEFAULT_DIST_K)
                            blocks = detect_deviation_blocks(
                                hours_d, means_d, k=block_k, min_points=60)
                            if blocks:
                                shifts_d = _cap_shifts(blocks,
                                                       args.max_shifts)
                        shifts_all += shifts_d
                        # 缺失(>5 分钟)标注
                        gaps_d = []
                        if len(hours_d) > 1:
                            for a in range(len(hours_d) - 1):
                                gaph = (hours_d[a + 1] - hours_d[a]
                                        ).total_seconds() / 3600.0
                                if gaph > 5.0 / 60.0:
                                    gaps_d.append({
                                        "起始时间": hours_d[a].strftime(
                                            "%Y-%m-%d %H:%M"),
                                        "结束时间": hours_d[a + 1].strftime(
                                            "%Y-%m-%d %H:%M"),
                                        "缺失小时数": round(gaph, 3),
                                    })
                        gaps_all += gaps_d
                        # 日聚合(清洗后)
                        day_dates.append(date_str)
                        day_means.append(float(np.mean(means_d)))
                        day_maxs.append(float(np.max(maxs_d)))
                        day_mins.append(float(np.min(mins_d)))
                        day_secs.append(secs)
                        day_miss.append(miss)
                        # 直方图累积(季度汇总)
                        cnts, _ = np.histogram(means_d, bins=hist_bins)
                        hist_counts = (cnts if hist_counts is None
                                       else hist_counts + cnts)
                        # 选定出图日：--vibration-date 优先，否则取有效秒数最多的一天
                        if args.vibration_date and date_str == args.vibration_date:
                            chart_day = (date_str, hours_d, means_d,
                                         sorted(ix1), sorted(rx1),
                                         shifts_d, gaps_d)
                        elif not args.vibration_date and secs > best_secs:
                            best_secs = secs
                            chart_day = (date_str, hours_d, means_d,
                                         sorted(ix1), sorted(rx1),
                                         shifts_d, gaps_d)
                    if not day_dates:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    # 振动只出一张时间序列图(选定一天，横轴 0~24 小时，标题含日期)
                    if chart_day and not args.skip_per_sensor \
                            and not args.skip_charts:
                        fout = os.path.join(chart_dir, sensor, feature)
                        os.makedirs(fout, exist_ok=True)
                        cdate, ch, cm, cix, crx, csh, cgp = chart_day
                        plot_daily_time_series(
                            sensor, sensor_name, feature, cdate, ch, cm,
                            os.path.join(fout, "时间序列图.png"),
                            replaced_indices=cix,
                            replaced_range_indices=crx,
                            shifts=csh, gaps=cgp, dpi=args.dpi)
                    # 季度频率分布图(按天累积)
                    if hist_counts is not None and not args.skip_per_sensor \
                            and not args.skip_charts:
                        fout = os.path.join(chart_dir, sensor, feature)
                        os.makedirs(fout, exist_ok=True)
                        plot_histogram_from_counts(
                            sensor, sensor_name, feature, hist_bins,
                            hist_counts, os.path.join(fout, "频率分布图.png"))
                    if not args.skip_stats:
                        # 连续恒0故障段（与图库“可能故障”标注同一规则）：剔除后
                        # 算极值，并记录 疑似故障时间段 供季度统计/总结引用
                        zero_runs = detect_zero_runs(
                            native_hours, native_means,
                            min_hours=zero_min_hours(feature)) \
                            if native_hours else []
                        _n_means, _n_maxs, _n_mins = native_means, native_maxs, native_mins
                        if zero_runs:
                            _nh, _n_means, _n_maxs, _n_mins = _mask_zero_run_hours(
                                native_hours, native_means,
                                native_maxs, native_mins, zero_runs)
                        stats, day_dates, day_means, day_maxs, day_mins = \
                            compute_feature_stats(
                                day_dates, day_means, day_maxs, day_mins,
                                day_secs, day_miss, None, None,
                                _n_means, _n_maxs, _n_mins)
                        if stats is None:
                            issues.append(f"无数据: {sensor}/{feature}")
                            continue
                        if zero_runs:
                            stats["疑似故障时间段"] = zero_runs
                        stats["特征"] = feature
                        stats["特征中文名"] = feature_display(feature)
                        stats["预处理"] = {
                            "尖峰替代": spike_rec,
                            "突变区间": shifts_all,
                            "数据缺失填充": gaps_all,
                        }
                        sensor_stats["特征统计"][feature] = stats
                        # 统计库: 位置 -> 测点 -> 特征(只存整体统计，不存每日统计)
                        pos_stats.setdefault(pos_name, {}).setdefault(
                            str(sensor), {})[feature] = {
                            "统计": {k: v for k, v in stats.items()
                                     if k not in ("每日统计", "特征",
                                                  "特征中文名", "预处理")},
                        }
                        _order = pos_sensor_order.setdefault(pos_name, [])
                        if str(sensor) not in _order:
                            _order.append(str(sensor))
                    daily_by_feat[feature] = (day_dates, day_means)
                    pos_daily.setdefault(pos_name, {}).setdefault(
                        str(sensor), {})[feature] = (list(day_dates),
                                                     list(day_means))
                    continue
                # ---------- 小时级数据(一天 24 个点) ----------
                (hours, hmeans, hmaxs, hmins,
                 day_dates, day_means, day_maxs, day_mins,
                 day_secs, day_miss) = read_hourly_series(
                     resolve_feature_dirs(
                         args.daily_root, sensor, feature))
                if not hours:
                    issues.append(f"无数据: {sensor}/{feature}")
                    continue
                keep = [i for i, h in enumerate(hours)
                        if (not args.start
                            or h.date().isoformat() >= args.start)
                        and (not args.end
                             or h.date().isoformat() <= args.end)]
                if not keep:
                    issues.append(f"无数据: {sensor}/{feature}")
                    continue
                hours = [hours[i] for i in keep]
                hmeans = [hmeans[i] for i in keep]
                hmaxs = [hmaxs[i] for i in keep]
                hmins = [hmins[i] for i in keep]

                # 尖峰清洗: 均值/最大/最小 三个序列各自检测替代
                spike_rec = []
                spike_idx = []
                vrange = feature_range(feature)
                spike_k = (0 if _is_direction_feature(feature)
                           else args.spike_threshold)
                hmeans, r1, ix1, rx1 = clean_series_value(
                    hours, hmeans, "小时均值", spike_k,
                    hour_level=True, vrange=vrange,
                    max_spikes=args.max_spikes, dist_k=args.dist_k,
                    max_dist_outliers=args.max_dist_outliers,
                    max_total_removals=args.max_removals)
                hmaxs, r2, ix2, rx2 = clean_series_value(
                    hours, hmaxs, "小时最大值", spike_k,
                    hour_level=True, vrange=vrange,
                    max_spikes=args.max_spikes, dist_k=args.dist_k,
                    max_dist_outliers=args.max_dist_outliers,
                    max_total_removals=args.max_removals)
                hmins, r3, ix3, rx3 = clean_series_value(
                    hours, hmins, "小时最小值", spike_k,
                    hour_level=True, vrange=vrange,
                    max_spikes=args.max_spikes, dist_k=args.dist_k,
                    max_dist_outliers=args.max_dist_outliers,
                    max_total_removals=args.max_removals)
                spike_rec = r1 + r2 + r3
                # 图上只标均值序列的剔除点；最大/最小序列的清洗记录仍写入 JSON
                spike_idx = sorted(ix1)
                range_idx = sorted(rx1)

                # 突变段检测(小时级，按"当天多数小时偏离"判定；风向除外)
                shifts = []
                if (args.shift_threshold > 0
                        and not _is_direction_feature(feature)):
                    shifts = _cap_shifts(
                        detect_level_shifts(
                            hours, hmeans, args.shift_min_days,
                            args.shift_threshold),
                        args.max_shifts)
                # 连续异常段(> max_run 点)：不剔除，只在图上标注
                if not _is_direction_feature(feature):
                    block_k = (args.dist_k if args.dist_k > 0
                               else DEFAULT_DIST_K)
                    blocks = detect_deviation_blocks(
                        hours, hmeans, k=block_k)
                    if blocks:
                        shifts = _cap_shifts(shifts + blocks,
                                             args.max_shifts)

                # 数据缺失(>=24h)检测: 图上标注，并用线性插值填充绘图序列
                gaps = []
                plot_hours, plot_means = hours, hmeans
                plot_maxs, plot_mins = hmaxs, hmins
                if args.gap_fill_hours > 0:
                    (plot_hours, plot_means, plot_maxs, plot_mins), gaps = \
                        fill_long_gaps(hours, hmeans, hmaxs, hmins,
                                       args.gap_fill_hours)
                # 原序列下标 -> 填充后下标(尖峰标记位置对齐)
                filled_map = {}
                fi = 0
                for oi, h in enumerate(hours):
                    while plot_hours[fi] != h:
                        fi += 1
                    filled_map[oi] = fi
                    fi += 1
                spike_idx_plot = [filled_map[i] for i in spike_idx]
                range_idx_plot = [filled_map[i] for i in range_idx]

                # 统计值用"真实数据(已去尖峰, 不含填充值)"
                # 有效/缺失秒数沿用 read_hourly_series 的逐日统计
                # (风速 10min / 振动秒级等非小时粒度下也正确)
                _raw_secs = dict(zip(day_dates, day_secs))
                _raw_miss = dict(zip(day_dates, day_miss))
                day_dates, day_means, day_maxs, day_mins = \
                    aggregate_daily_from_hours(hours, hmeans, hmaxs, hmins)[:4]
                day_secs = [_raw_secs.get(d, 0) for d in day_dates]
                day_miss = [_raw_miss.get(d, 0) for d in day_dates]
                stats = None
                if not args.skip_stats:
                    # 连续恒0故障段剔除（与图库“可能故障”标注同一规则）
                    zero_runs = detect_zero_runs(
                        hours, hmeans, min_hours=zero_min_hours(feature)) \
                        if hours else []
                    _n_means, _n_maxs, _n_mins = hmeans, hmaxs, hmins
                    if zero_runs:
                        _nh, _n_means, _n_maxs, _n_mins = _mask_zero_run_hours(
                            hours, hmeans, hmaxs, hmins, zero_runs)
                    stats, day_dates, day_means, day_maxs, day_mins = \
                        compute_feature_stats(
                            day_dates, day_means, day_maxs, day_mins,
                            day_secs, day_miss, None, None,
                            _n_means, _n_maxs, _n_mins)
                    if stats is None:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    if zero_runs:
                        stats["疑似故障时间段"] = zero_runs
                    stats["特征"] = feature
                    stats["特征中文名"] = feature_display(feature)
                    stats["预处理"] = {
                        "尖峰替代": spike_rec,
                        "突变区间": shifts,
                        "数据缺失填充": gaps,
                    }
                    if sensor == TRAFFIC_SENSOR:
                        # 交通荷载: 数值 = 期内累计通过车辆数(小时计数求和)
                        stats["数值"] = round(float(sum(hmeans)), 1)
                    sensor_stats["特征统计"][feature] = stats
                    # 统计库: 位置 -> 测点 -> 特征(只存整体统计，不存每日统计)
                    pos_stats.setdefault(pos_name, {}).setdefault(
                        str(sensor), {})[feature] = {
                        "统计": {k: v for k, v in stats.items()
                                 if k not in ("每日统计", "特征",
                                              "特征中文名", "预处理")},
                    }
                    _order = pos_sensor_order.setdefault(pos_name, [])
                    if str(sensor) not in _order:
                        _order.append(str(sensor))
                daily_by_feat[feature] = (day_dates, day_means)
                pos_daily.setdefault(pos_name, {}).setdefault(
                    str(sensor), {})[feature] = (list(day_dates),
                                                 list(day_means))

                # 逐传感器图（--skip-per-sensor 时跳过，merged 模式直接出图）
                if (not args.skip_per_sensor and not args.skip_charts
                        and sensor != TRAFFIC_SENSOR):
                    fout = os.path.join(chart_dir, sensor, feature)
                    os.makedirs(fout, exist_ok=True)
                    plot_time_series(sensor, sensor_name, feature, plot_hours,
                                     plot_means,
                                     os.path.join(fout, "时间序列图.png"),
                                     shifts=shifts,
                                     replaced_indices=spike_idx_plot,
                                     replaced_range_indices=range_idx_plot,
                                     gaps=gaps,
                                     hour_level=True)
                    plot_histogram(sensor, sensor_name, feature, hmeans,
                                   os.path.join(fout, "频率分布图.png"))
                if not args.skip_stats and stats and len(day_dates) < 2:
                    stats["提示"] = "数据不足，仅 1 天"
                    issues.append(f"数据不足: {sensor}/{feature} 仅 1 天")
            except Exception as exc:
                issues.append(f"错误: {sensor}/{feature}: {exc}")
                print(f"[警告] {sensor}/{feature} 处理失败: {exc}", flush=True)

        # 交通荷载: 车道比例 = 车道数值 / 总共数值 * 100
        if sensor == TRAFFIC_SENSOR and not args.skip_stats:
            total_val = (sensor_stats["特征统计"].get(
                TRAFFIC_TOTAL_FEATURE) or {}).get("数值")
            if total_val:
                for feat, st in sensor_stats["特征统计"].items():
                    if feat == TRAFFIC_TOTAL_FEATURE or not st.get("数值"):
                        continue
                    st["比例"] = round(float(st["数值"]) / float(total_val) * 100, 2)
                    rec = pos_stats.get(pos_name, {}).get(
                        str(sensor), {}).get(feat)
                    if rec:
                        rec["统计"]["比例"] = st["比例"]

        # 相关性分析：同一传感器内两两特征，按日期对齐（仅 per_sensor 模式）
        if args.mode == "per_sensor" and args.correlation and len(daily_by_feat) >= 2:
            feats_list = list(daily_by_feat)
            for i in range(len(feats_list)):
                for j in range(i + 1, len(feats_list)):
                    fa, fb = feats_list[i], feats_list[j]
                    da, ma = daily_by_feat[fa]
                    db, mb = daily_by_feat[fb]
                    common = sorted(set(da) & set(db))
                    if len(common) < 10:
                        continue
                    ia = {d: i for i, d in enumerate(da)}
                    ib = {d: i for i, d in enumerate(db)}
                    x = [ma[ia[d]] for d in common]
                    y = [mb[ib[d]] for d in common]
                    out = os.path.join(chart_dir, sensor,
                                       f"相关性_{fa}_{fb}.png")
                    r = plot_correlation(fa, fb, x, y, sensor_name, sensor,
                                         out)
                    if r is not None:
                        corr_results[f"{fa} ~ {fb}"] = r
        sensor_stats["相关性"] = corr_results

        # 选择性重新生成(--features)时，保留该传感器其他特征的旧统计值
        if args.features:
            old_path = os.path.join(stats_dir, f"{sensor}.json")
            if os.path.isfile(old_path):
                try:
                    with open(old_path, "r", encoding="utf-8") as f:
                        old = json.load(f)
                    for k, v in (old.get("特征统计") or {}).items():
                        sensor_stats["特征统计"].setdefault(k, v)
                    if "相关性" in old and not sensor_stats["相关性"]:
                        sensor_stats["相关性"] = old["相关性"]
                except Exception:  # noqa: BLE001
                    pass

        if not args.skip_stats:
            # 统计值 JSON 只写整体统计字段(不写“每日统计”/“有效天数”/
            # “缺失天数”等明细)；计算过程仍保留，报告运行时按整体统计取值
            for _fstats in (sensor_stats.get("特征统计") or {}).values():
                if isinstance(_fstats, dict):
                    _fstats.pop("每日统计", None)
                    _fstats.pop("有效天数", None)
                    _fstats.pop("缺失天数", None)
            with open(os.path.join(stats_dir, f"{sensor}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(sensor_stats, f, ensure_ascii=False, indent=2)

        overview.append({
            "编号": sensor, "名称": sensor_name, "桥名": bridge,
            "特征": list(sensor_stats["特征统计"]),
        })
        if idx % 20 == 0 or idx == len(sensors):
            el = time.time() - t0
            print(f"  进度 {idx}/{len(sensors)}  已用 {el:.0f}s", flush=True)

    if not args.skip_stats:
        # 应变-温度联合统计：同一位置（可能不同传感器）的应变与温度
        # 按日对齐回归，剔除温度效应后写入应变特征(YB(rsg))的统计，
        # 供报告应变表 “剔除温度最大值/剔除温度最小值/相关性系数” 列使用。
        # 位置名可能因“左幅/右幅”与“上游/下游”写法不一致而对不上，
        # 先精确配对，失败时按“核心部位归一化”跨位置配对。
        pos_feats_all = {}
        for pos_name in pos_daily:
            pf = {}
            for sid, feats in pos_daily[pos_name].items():
                for feat, (dd, mm) in feats.items():
                    pf.setdefault(feat, []).append((sid, dd, mm))
            pos_feats_all[pos_name] = pf

        def _pair_temp(pos_name, pf):
            """用 pf 里的温度序列给 YB(rsg) 各测点算剔除温度统计。"""
            if "YB(rsg)" not in pf:
                return
            temp_keys = [f for f in pf
                         if f in ("WD(temp)", "WSD(temp)")]
            if not temp_keys:
                return
            # 取同位置温度序列：优先选非恒值(有正常波动)的温度传感器，
            # 避免取到“整季恒0”的故障传感器导致相关系数失真
            temp_entries = pf[temp_keys[0]]
            _t_sid, t_dates, t_means = temp_entries[0]
            for _tsid, _tdd, _tmm in temp_entries:
                if _tmm and (max(_tmm) - min(_tmm)) > 1e-9:
                    _t_sid, t_dates, t_means = _tsid, _tdd, _tmm
                    break
            for s_sid, s_dates, s_means in pf["YB(rsg)"]:
                te = _temp_effect_stats(s_dates, s_means,
                                        t_dates, t_means)
                if not te:
                    continue
                rec = pos_stats.get(pos_name, {}).get(str(s_sid), {}).get(
                    "YB(rsg)")
                if rec:
                    rec["统计"].update(te)

        # 第一遍：精确位置配对
        for pos_name, pf in pos_feats_all.items():
            _pair_temp(pos_name, pf)
        # 第二遍：核心部位归一化配对（顶板左幅 <-> 顶板上游 等）。
        # 仅对配置名单内的大桥生效（洣水河编号文件应变/温度方位命名不一致），
        # 其他桥仍只做精确配对，避免不同方位传感器被错误跨配。
        cross_side = any(v in STRAIN_TEMP_CROSS_SIDE_BRIDGES
                         for v in _bridge_variants(bridge))
        if cross_side:
            norm_index = {}
            for pos_name, pf in pos_feats_all.items():
                if any(f in ("WD(temp)", "WSD(temp)") for f in pf):
                    norm_index.setdefault(
                        _norm_strain_temp_pos(pos_name), []).append(pos_name)
            for pos_name, pf in pos_feats_all.items():
                if "YB(rsg)" not in pf:
                    continue
                if any(f in ("WD(temp)", "WSD(temp)") for f in pf):
                    continue   # 已精确配对
                core = _norm_strain_temp_pos(pos_name)
                for t_pos in norm_index.get(core, []):
                    if t_pos == pos_name:
                        continue
                    merged = dict(pf)
                    merged.update(pos_feats_all[t_pos])
                    _pair_temp(pos_name, merged)
                    break
        # 位置统计库(与图库目录结构对齐):
        #   统计值_<期>/<桥名>/位置统计/<位置>/<特征>.json
        #   内容: {位置: {测点X: {统计, 传感器编号}}}（只存整体统计）
        #   相关性: 位置统计/<位置>/相关性_<特征A>-<特征B>.json
        pos_stats_dir = os.path.join(stats_dir, "位置统计")
        os.makedirs(pos_stats_dir, exist_ok=True)
        for pos_name in sorted(pos_stats):
            pos_safe = _safe_dirname(pos_name)
            pos_dir = os.path.join(pos_stats_dir, pos_safe)
            os.makedirs(pos_dir, exist_ok=True)
            if pos_name == TRAFFIC_SENSOR:
                # 交通荷载：单特征文件 {交通荷载: {车道X: {统计, 传感器编号, 特征}}}，
                # 车道X 即“测点”键，供报告 cell.vehicle_count.车道X.* 索引
                payload = {pos_name: {}}
                for sid in pos_sensor_order.get(pos_name, []):
                    for feat, v in pos_stats[pos_name].get(sid, {}).items():
                        if feat == TRAFFIC_TOTAL_FEATURE:
                            continue
                        payload[pos_name][feat] = dict(
                            v, 传感器编号=sid, 特征=TRAFFIC_STAT_FEATURE)
                with open(os.path.join(pos_dir, "交通荷载.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                continue
            # 按特征聚合: 特征 -> 测点X -> {统计, 传感器编号}
            feat_points = {}
            for sid in pos_sensor_order.get(pos_name, []):
                feats = pos_stats[pos_name].get(sid, {})
                if not feats:
                    continue
                for feat, v in feats.items():
                    feat_points.setdefault(feat, {})
                    pt_key = f"测点{len(feat_points[feat]) + 1}"
                    feat_points[feat][pt_key] = dict(v, 传感器编号=sid)
            for feat, points in feat_points.items():
                # 单特征 JSON: {位置: {测点X: {统计, 传感器编号, 特征}}}
                payload = {pos_name: {
                    pt: dict(v, 特征=feat) for pt, v in points.items()
                }}
                with open(os.path.join(
                        pos_dir, _safe_dirname(feat) + ".json"),
                        "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
        # 把 剔除温度/相关性系数 写回逐传感器统计 JSON
        # （报告运行时按 统计值_<期>/<桥名>/<编号>.json 取值；
        #   相关性键按测点号区分，如 相关性_WD(temp)-YB(rsg)测点1）
        for pos_name, pts in pos_stats.items():
            order = [str(x) for x in pos_sensor_order.get(pos_name, [])]
            for sid, feats in pts.items():
                yb = feats.get("YB(rsg)") or {}
                te = yb.get("统计") or {}
                if "剔除温度最大值" not in te:
                    continue
                sensor_path = os.path.join(stats_dir, f"{sid}.json")
                if not os.path.isfile(sensor_path):
                    continue
                try:
                    with open(sensor_path, "r", encoding="utf-8") as f:
                        sd = json.load(f)
                    fs = sd.setdefault("特征统计", {}).setdefault("YB(rsg)", {})
                    for k in ("剔除温度最大值", "剔除温度最小值", "相关性系数"):
                        if k in te:
                            fs[k] = te[k]
                    if str(sid) in order:
                        pt_no = order.index(str(sid)) + 1
                        fs[f"相关性_WD(temp)-YB(rsg)测点{pt_no}"] = te.get("相关性系数")
                    with open(sensor_path, "w", encoding="utf-8") as f:
                        json.dump(sd, f, ensure_ascii=False, indent=2)
                except Exception as exc:  # noqa: BLE001
                    print(f"[警告] 写回应变-温度统计失败 {sid}: {exc}")
        print(f"  位置统计库已写出: {pos_stats_dir} "
              f"({len(pos_stats)} 个位置)")
        with open(os.path.join(stats_dir, "总览.json"), "w",
                  encoding="utf-8") as f:
            json.dump({
                "说明": "全部传感器-特征图库总览",
                "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "传感器数量": len(overview),
                "传感器": overview,
            }, f, ensure_ascii=False, indent=2)

    # ---------- 交通荷载：跨车道合并图（累计数量/比例/频率分布） ----------
    # 与普通特征不同：不生成 车道N 子文件夹，直接在 交通荷载/ 下出三张图；
    # --features 交通荷载 时只出该桥交通荷载相关目录
    if not args.skip_charts and TRAFFIC_SENSOR in sensor_feats:
        if (not args.features or _traffic_selected(args.features)
                or any(f.strip().startswith("车道")
                       for f in str(args.features or "").split(","))):
            try:
                _build_traffic_charts(args, sensor_feats, chart_dir, issues)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"交通荷载图错误: {exc}")
                print(f"[警告] 交通荷载图失败: {exc}", flush=True)

    # ---------- 合并图库（按监测部位分组，一张图多测点/多特征） ----------
    if args.mode == "merged" and not args.skip_charts:
        print("开始生成合并图库（按监测部位分组）...")
        pos_map = {}
        if args.position_map and os.path.isfile(args.position_map):
            pos_map = load_position_map(args.position_map)
        else:
            nd_dir = os.path.join(DEFAULT_SENSOR_MAP_DIR, "传感器名称对照")
            if not os.path.isdir(nd_dir):
                old_nd = os.path.join(stats_dir, "传感器名称对照")
                if os.path.isdir(old_nd):
                    nd_dir = old_nd
            if os.path.isdir(nd_dir):
                files = [f for f in sorted(os.listdir(nd_dir))
                         if f.endswith(".json")]
                # 只加载当前桥的对照文件(文件名以桥名开头)，避免把
                # 其它桥的位置也拉进来(如湘江特跑出洞庭湖/矮寨的位置)
                if bridge:
                    picks = [f for f in files if f[:-5].startswith(bridge)]
                    if picks:
                        files = picks
                for f in files:
                    pm = load_position_map(os.path.join(nd_dir, f))
                    for k, v in pm.items():
                        pos_map.setdefault(k, []).extend(v)
                pos_map = {k: sorted(set(v)) for k, v in pos_map.items()}
                # 安全网：按对照表里的桥名再过滤一遍
                if bridge:
                    def _same_bridge(info_bridge):
                        return (bridge in (info_bridge or "")
                                or (info_bridge or "") in bridge)
                    pos_map = {
                        k: [p for p in v
                            if _same_bridge(sensor_map.get(p[0], {}).get("桥名", ""))]
                        for k, v in pos_map.items()
                    }
                    pos_map = {k: v for k, v in pos_map.items() if v}
        if not pos_map:
            pos_map = load_position_map("", map_path)
        if not pos_map:
            print("[警告] 未找到位置-传感器映射，合并图库跳过")
        else:
            allowed = set(sensors) if args.limit_sensors else None
            merged_ok = 0
            merged_fail = 0
            for pos, pairs in sorted(pos_map.items()):
                if allowed is not None:
                    pairs = [(s, f) for s, f in pairs if s in allowed]
                if args.features:
                    pairs = [(s, f) for s, f in pairs
                             if _feature_selected(f, args.features)]
                if not pairs:
                    continue
                groups = defaultdict(list)
                for sensor, feat in pairs:
                    # 特征以 daily 下该传感器实际目录为准(如对照表写
                    # DZJSD(xJsd)，但实际数据是 EZJSD(xJsd)/EZJSD(yJsd))，
                    # 避免合并图文件夹/标签与 per_sensor 图、统计库对不上；
                    # daily 无该传感器数据时回退用对照表特征(便于报“无数据”)
                    actual_feats = sensor_feats.get(str(sensor))
                    if actual_feats:
                        for af in actual_feats:
                            pair = (sensor, af)
                            gk = feature_group(af)
                            if pair not in groups[gk]:
                                # 同一传感器多特征时会重复进入本循环，去重，
                                # 避免合并图出现两个相同测点(如 431 出现两次)
                                groups[gk].append(pair)
                    else:
                        pair = (sensor, feat)
                        gk = feature_group(feat)
                        if pair not in groups[gk]:
                            groups[gk].append(pair)
                pos_series = []   # 位置内全部特征序列（用于跨特征相关性散点图）
                for g, gf_pairs in sorted(groups.items()):
                    uniq_sensors = {s for s, _ in gf_pairs}
                    uniq_feats = {f for _, f in gf_pairs}
                    out_dir = os.path.join(chart_dir, _safe_dirname(pos),
                                           _safe_dirname(g))
                    try:
                        # 振动(秒级)合并图：按天出图(横轴小时、标题含日期)，
                        # 每天只加载当天数据，避免整季秒级数据过大无法出图
                        if all(_is_second_level_feature(f)
                               for _, f in gf_pairs):
                            _build_merged_daily_charts(
                                args, pos, g, gf_pairs, out_dir, issues)
                            merged_ok += 1
                            continue
                        # 单传感器单特征：默认复制 per_sensor 图；
                        # --skip-per-sensor 时由 merged 路径直接出图
                        if (len(uniq_sensors) == 1 and len(uniq_feats) == 1
                                and not args.skip_per_sensor):
                            copied = _copy_per_sensor_charts(
                                chart_dir, next(iter(uniq_sensors)),
                                next(iter(uniq_feats)), out_dir,
                                fallback_charts_dir=os.path.join(
                                    args.lib_root, "图库", bridge))
                            if not copied:
                                merged_fail += 1
                                continue
                            gs = _build_merged_series(
                                args.daily_root, gf_pairs, args.start, args.end,
                                args.spike_threshold, args.max_spikes,
                                args.gap_fill_hours, args.shift_min_days,
                                args.shift_threshold, args.dist_k,
                                args.max_dist_outliers, args.max_shifts,
                                args.max_removals)
                            pos_series.extend(gs)
                            merged_ok += 1
                            continue
                        # 其余情况（单传感器多特征 / 多传感器）：
                        # 读取数据 -> 生成一张含多个子图的合并图（不再分子特征文件夹）
                        series = _build_merged_series(
                            args.daily_root, gf_pairs, args.start, args.end,
                            args.spike_threshold, args.max_spikes,
                            args.gap_fill_hours, args.shift_min_days,
                            args.shift_threshold, args.dist_k,
                            args.max_dist_outliers, args.max_shifts,
                            args.max_removals)
                        if not series:
                            merged_fail += 1
                            continue
                        pos_series.extend(series)
                        os.makedirs(out_dir, exist_ok=True)
                        plot_group_time_series(
                            pos, g, series,
                            os.path.join(out_dir, "时间序列图.png"),
                            dpi=args.dpi)
                        plot_group_histogram(
                            pos, g, series,
                            os.path.join(out_dir, "频率分布图.png"),
                            dpi=args.dpi)
                        if len(uniq_feats) >= 2:
                            plot_group_correlation(
                                pos, g, series,
                                os.path.join(out_dir, "相关性图.png"),
                                dpi=args.dpi)
                        with open(os.path.join(out_dir, "预处理记录.json"),
                                  "w", encoding="utf-8") as f:
                            json.dump({
                                "位置": pos,
                                "特征组": g,
                                "生成时间": dt.datetime.now().strftime(
                                    "%Y-%m-%d %H:%M:%S"),
                                "测点": [{
                                    "标签": s["label"],
                                    "传感器": s["sensor"],
                                    "特征": s["feature"],
                                    "清洗记录": s["records"],
                                    "数据缺失时段": s["gaps"],
                                    "突变区间": s.get("shifts", []),
                                } for s in series],
                            }, f, ensure_ascii=False, indent=2)
                        merged_ok += 1
                    except Exception as exc:  # noqa: BLE001
                        merged_fail += 1
                        issues.append(f"合并图错误: {pos}/{g}: {exc}")
                        print(f"[警告] 合并图失败 {pos}/{g}: {exc}", flush=True)
                # 位置级跨特征相关性散点图（如 结构应变-温度）
                if len({s["feature"] for s in pos_series}) >= 2:
                    try:
                        plot_position_correlation(
                            pos, pos_series,
                            os.path.join(chart_dir, _safe_dirname(pos)),
                            dpi=args.dpi)
                    except Exception as exc:  # noqa: BLE001
                        issues.append(f"相关性图错误: {pos}: {exc}")
                        print(f"[警告] 相关性图失败 {pos}: {exc}", flush=True)
            print(f"合并图库完成: 成功 {merged_ok} 组，失败 {merged_fail} 组")

    # 失败/数据不足记录（--skip-charts 时写到统计值目录，图库目录不存在）
    issue_path = os.path.join(
        chart_dir if not args.skip_charts else stats_dir,
        "生成失败记录.txt")
    with open(issue_path, "w", encoding="utf-8") as f:
        f.write(f"图库生成记录\n生成时间: "
                f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"问题总数: {len(issues)}\n")
        f.write("=" * 50 + "\n")
        f.write("\n".join(issues) if issues else "无\n")
    print(f"问题记录已写入: {issue_path} ({len(issues)} 条)")

    if args.skip_per_sensor and args.skip_stats:
        print(f"[完成] 跳过逐传感器处理(共 {len(sensors)} 个传感器)，"
              f"仅生成合并图，总耗时 {time.time()-t0:.0f}s")
    else:
        print(f"[完成] 共处理 {len(sensors)} 个传感器，"
              f"总耗时 {time.time()-t0:.0f}s")
    if not args.skip_per_sensor and not args.skip_charts:
        _copy_per_sensor_dirs(args.lib_root, chart_dir, bridge)
    _write_status_dirs(args.lib_root, chart_dir, stats_dir,
                       update_charts=not args.skip_charts)
    print(f"  图库目录: {chart_dir}")
    print(f"  统计值目录: {stats_dir}")


if __name__ == "__main__":
    main()
