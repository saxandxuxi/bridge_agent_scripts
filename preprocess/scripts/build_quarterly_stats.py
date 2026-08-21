#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
季度/年度统计值生成(按监测部位合并多传感器，快速模式)
================================================

默认(快速模式)：直接读取 统计值_<期>/<桥名>/位置统计/<位置>.json
   1) 每个位置文件含 测点X -> 特征 -> {统计, 每日统计, 传感器编号}；
   2) 以“特征”为最高键汇总全桥：同特征跨所有位置/测点按“日期”合并
      (平均值取均值、最大值取最大、最小值取最小)，
      再计算该特征的全桥季度/年度整体统计；
   3) 汇总结果含 全桥统计 + 每个 位置->测点 的统计与传感器编号，
      便于报告定位“最大值对应测点”等血缘信息。
   优点：不重新读 TB 级 daily 原始数据，只读已经算好的统计库 JSON。
回退(--mode daily)：仍保留旧的按小时级 daily 数据重算逻辑。

输出: 统计值_<期>/<桥名>/季度总结/季度统计.json
      或 统计值_<期>/<桥名>/季度总结/年度统计.json(--period yearly)
      季度总结单独一个文件夹，不与逐传感器统计 JSON 混在一起
结构: {桥名: {特征: {全桥统计: {...}, 位置: {位置: {测点X: {统计: {...}, 传感器编号}}}}}}

用法:
    python build_quarterly_stats.py [--daily-root ...] [--lib-root ...]
                                    [--bridge 桥名] [--period quarterly|yearly]
                                    [--start ...] [--end ...]
    年度统计时 --daily-root 传桥根目录(如 D:\preprocess_sensor_data\湘江特)，
    脚本会汇总其下所有 daily_* 子目录的数据。
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

import build_chart_library as bcl

DEFAULT_DAILY_ROOT = r"D:\preprocess_sensor_data\daily"
# 统计值目录的上级 = 项目 preprocess/ 目录（按脚本位置计算，与运行目录无关，
# 避免从项目根目录直接运行时把 ..\统计值_<期> 解析到桌面等错误位置）
DEFAULT_LIB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 传感器对照表(固定产物，不随季度变化)统一放 preprocess/传感器对照/
DEFAULT_SENSOR_MAP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "传感器对照")


def _derive_period_tag(daily_root="", start="", end=""):
    """从 daily 目录名(如 daily_2026.1~3)或 --start/--end 推导期次标签。
    返回如 "2026.1~3"，推导不到返回空串。"""
    tag = ""
    base = os.path.basename(os.path.normpath(daily_root or ""))
    if base.startswith("daily_"):
        tag = base[len("daily_"):]
    if tag:
        return tag
    try:
        if start and end:
            d0 = dt.datetime.strptime(start, "%Y-%m-%d").date()
            d1 = dt.datetime.strptime(end, "%Y-%m-%d").date()
            if d0.year == d1.year:
                return f"{d0.year}.{d0.month}~{d1.month}"
            return f"{d0.year}.{d0.month}~{d1.year}.{d1.month}"
    except (ValueError, AttributeError):
        pass
    return ""


def load_sensor_map(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("传感器", {})


def load_sensor_stats_json(stats_dir, sensor_id):
    """读取 统计值_<期>/<桥名>/<传感器编号>.json，返回 dict 或 None。"""
    p = os.path.join(stats_dir, f"{sensor_id}.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def collect_daily_from_stats(stats_dir, sensor_map):
    """从统计值 JSON 收集逐传感器逐特征的每日统计。

    返回 {传感器: {特征: {"每日统计": [{日期, 平均值, 最大值, 最小值}],
                        "整体": {...}, "监测部位": str}}}
    每日统计缺失时(新版本已精简)用“整体统计”字段回退，仍不读 daily。
    """
    out = {}
    if not os.path.isdir(stats_dir):
        return out
    for fn in sorted(os.listdir(stats_dir)):
        if not fn.endswith(".json"):
            continue
        sid = fn[:-5]
        if not sid.isdigit():
            continue
        data = load_sensor_stats_json(stats_dir, sid)
        if not data:
            continue
        info = sensor_map.get(sid, {})
        loc = info.get("监测部位") or info.get("名称") or ""
        fstats = data.get("特征统计") or {}
        feats = {}
        for feat, st in fstats.items():
            if not isinstance(st, dict):
                continue
            daily = []
            dl = st.get("每日统计")
            if isinstance(dl, list):
                for d in dl:
                    try:
                        daily.append({
                            "日期": str(d.get("日期", "")),
                            "平均值": float(d.get("平均值")),
                            "最大值": float(d.get("最大值")),
                            "最小值": float(d.get("最小值")),
                        })
                    except (TypeError, ValueError):
                        continue
            overall = {k: v for k, v in st.items()
                       if k not in ("每日统计", "特征", "特征中文名", "预处理")}
            feats[feat] = {
                "每日统计": daily,
                "整体": overall,
                "监测部位": loc,
            }
        if feats:
            out[sid] = feats
    return out


def aggregate_by_day(day_map):
    """把同一监测部位+特征下多个传感器的每日记录按日期合并。
    day_map: {传感器: {日期: (平均值, 最大值, 最小值)}}
    返回 (dates, means, maxs, mins) 按日期升序。"""
    dates = sorted({d for s in day_map.values() for d in s})
    means, maxs, mins = [], [], []
    for d in dates:
        ms = [v[0] for s in day_map.values() if d in s for v in [s[d]]]
        xs = [v[1] for s in day_map.values() if d in s for v in [s[d]]]
        ns = [v[2] for s in day_map.values() if d in s for v in [s[d]]]
        if not ms:
            continue
        means.append(float(np.mean(ms)))
        maxs.append(float(np.max(xs)))
        mins.append(float(np.min(ns)))
    return dates, means, maxs, mins


def compute_stats_from_daily(records):
    """由逐日(日期, 平均值, 最大值, 最小值)计算整体统计，返回 dict。"""
    if not records:
        return None
    dates = [r[0] for r in records]
    means = [r[1] for r in records]
    maxs = [r[2] for r in records]
    mins = [r[3] for r in records]
    arr = np.array(means, dtype=float)
    if arr.size == 0:
        return None
    return {
        "起始日期": dates[0],
        "结束日期": dates[-1],
        "覆盖天数": len(dates),
        "有效小时数": round(float(arr.size * 24), 1),
        "缺失小时数": 0.0,
        "平均值": round(float(arr.mean()), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(arr.std()), 6),
        "最大值": round(float(np.max(maxs)), 6),
        "最小值": round(float(np.min(mins)), 6),
        "差值": round(float(np.max(maxs) - np.min(mins)), 6),
        "绝对最大值": round(
            max(abs(np.max(maxs)), abs(np.min(mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
    }


def _clean_daily_records(daily, st, feature=""):
    """无小时级数据时的兜底：只剔除全天无数据的全零日（缺失记0）。

    小时级数据可用时由 _stats_from_hours 按“连续恒0时间段”精确剔除，
    不在这里按天丢 0 污染日（会误删含有效数据的半天）。"""
    if not daily:
        return daily
    out = []
    for d in daily:
        try:
            mx = float(d.get("最大值"))
            mn = float(d.get("最小值"))
            av = float(d.get("平均值"))
        except (TypeError, ValueError):
            continue
        if mx == 0 and mn == 0 and av == 0:
            continue  # 全天无数据（缺失记 0）
        out.append(d)
    return out


def _stats_from_hours(hours, means, maxs, mins, st):
    """由小时级序列计算整体统计（剔除恒0故障时间段后的剩余小时）。

    最大值/最小值取清洗后原生最大/最小序列（与报告表格口径一致）。"""
    arr = np.array(means, dtype=float)
    if arr.size == 0:
        return None
    days = len({h.date() for h in hours})
    return {
        "起始日期": hours[0].strftime("%Y-%m-%d"),
        "结束日期": hours[-1].strftime("%Y-%m-%d"),
        "覆盖天数": days,
        "有效小时数": int(arr.size),
        "缺失小时数": float(st.get("缺失小时数") or 0),
        "平均值": round(float(arr.mean()), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(arr.std()), 6),
        "最大值": round(float(np.max(arr)), 6),
        "最小值": round(float(np.min(arr)), 6),
        "差值": round(float(np.max(arr) - np.min(arr)), 6),
        "绝对最大值": round(
            max(abs(np.max(maxs)), abs(np.min(mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
        "有效天数": days,
        "缺失天数": float(st.get("缺失天数") or 0),
    }


def _mask_zero_run_hours(hours, means, maxs, mins, runs):
    """剔除落在连续恒0时间段内的小时，返回 (hours, means, maxs, mins)。"""
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


def discover_pairs(daily_root):
    """扫描 daily/<传感器>/<特征>/，返回 [(传感器, 特征)]。"""
    pairs = []
    if not os.path.isdir(daily_root):
        return pairs
    for sensor in sorted(os.listdir(daily_root)):
        sroot = os.path.join(daily_root, sensor)
        if not os.path.isdir(sroot):
            continue
        for feat in sorted(os.listdir(sroot)):
            fdir = os.path.join(sroot, feat)
            if os.path.isdir(fdir) and any(
                    f.lower().endswith(".csv") for f in os.listdir(fdir)):
                pairs.append((sensor, feat))
    return pairs


def clean_sensor_series(hours, means, maxs, mins, feature):
    """单传感器清洗：物理范围 + 尖峰(与图库一致)。返回 (hours, m,x,n, 剔除数)。"""
    vrange = bcl.feature_range(feature)
    spike_k = (0 if bcl._is_direction_feature(feature) else 5.0)
    means, r1, _, _ = bcl.clean_series_value(
        hours, means, "小时均值", spike_k, hour_level=True, vrange=vrange)
    maxs, r2, _, _ = bcl.clean_series_value(
        hours, maxs, "小时最大值", spike_k, hour_level=True, vrange=vrange)
    mins, r3, _, _ = bcl.clean_series_value(
        hours, mins, "小时最小值", spike_k, hour_level=True, vrange=vrange)
    removed = len(r1) + len(r2) + len(r3)
    return hours, means, maxs, mins, removed


def compute_quarter_stats(hours, means, maxs, mins):
    arr = np.array(means, dtype=float)
    if arr.size == 0:
        return None
    return {
        "起始时间": hours[0].strftime("%Y-%m-%d %H:%M"),
        "结束时间": hours[-1].strftime("%Y-%m-%d %H:%M"),
        "有效小时数": int(arr.size),
        "覆盖天数": len({h.date() for h in hours}),
        "数值": round(float(np.sum(arr)), 1),
        "平均值": round(float(arr.mean()), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(arr.std()), 6),
        "最大值": round(float(np.max(arr)), 6),
        "最小值": round(float(np.min(arr)), 6),
        "差值": round(float(np.max(arr) - np.min(arr)), 6),
        "绝对最大值": round(
            max(abs(np.max(maxs)), abs(np.min(mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
    }


def main():
    ap = argparse.ArgumentParser(description="季度统计值(小时级, 按监测部位合并)")
    ap.add_argument("--daily-root", default=DEFAULT_DAILY_ROOT)
    ap.add_argument("--lib-root", default=DEFAULT_LIB_ROOT)
    ap.add_argument("--sensor-map", default="",
                    help="传感器编号名称.json 路径(默认 preprocess/传感器对照/"
                         "传感器编号名称.json)")
    ap.add_argument("--bridge", default="",
                    help="大桥名称(如 赤石)；统计值输出到 <lib-root>/统计值/<桥名>/，"
                         "daily 根目录取 <daily根>/<桥名>/daily。不填按对照表自动推导")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--limit-sensors", type=int, default=0)
    ap.add_argument("--period", choices=["quarterly", "yearly"],
                    default="quarterly",
                    help="统计周期: quarterly=季度统计.json(默认)；"
                         "yearly=年度统计.json(daily-root 传桥根目录, "
                         "汇总其下所有 daily_* 子目录)")
    ap.add_argument("--mode", choices=["stats", "daily"], default="stats",
                    help="数据来源: stats=读统计值 JSON 的每日统计聚合(默认,"
                         "不读 daily，快)；daily=按小时级 daily 原始数据重算")
    args = ap.parse_args()
    period = args.period

    tag = _derive_period_tag(args.daily_root, args.start, args.end)
    stats_dir0 = (os.path.join(args.lib_root, f"统计值_{tag}")
                  if tag else os.path.join(args.lib_root, "统计值"))
    map_path = (args.sensor_map
                or os.path.join(DEFAULT_SENSOR_MAP_DIR, "传感器编号名称.json"))
    sensor_map = load_sensor_map(map_path)
    print(f"传感器名称对照: {'已加载(' + str(len(sensor_map)) + '个)' if sensor_map else '未找到'}")
    bridge = args.bridge or ""
    if not bridge:
        names = [info.get("桥名", "") for info in sensor_map.values()]
        names = [n for n in names if n]
        if names:
            bridge = max(set(names), key=names.count)
    if bridge and args.daily_root == DEFAULT_DAILY_ROOT:
        base = os.path.dirname(DEFAULT_DAILY_ROOT.rstrip("/\\"))
        daily_name = os.path.basename(DEFAULT_DAILY_ROOT.rstrip("/\\"))
        # 兼容桥名写法差异(湘江特 vs 湘江特大桥 等)，按目录存在性匹配
        resolved = False
        for v in bcl._bridge_variants(bridge):
            cand = os.path.join(base, v, daily_name)
            if os.path.isdir(cand):
                bridge = v
                args.daily_root = cand
                resolved = True
                break
        if not resolved:
            args.daily_root = os.path.join(base, bridge, daily_name)
    stats_dir = os.path.join(stats_dir0, bridge) if bridge else stats_dir0
    os.makedirs(stats_dir, exist_ok=True)

    if args.mode == "stats":
        # ---------- 快速模式：读位置统计库，按特征为最高键汇总 ----------
        pos_stats_dir = os.path.join(stats_dir, "位置统计")
        if not os.path.isdir(pos_stats_dir):
            print(f"[错误] 位置统计库不存在: {pos_stats_dir}\n"
                  f"请先运行 build_chart_library.py 生成统计库，"
                  f"或改用 --mode daily")
            sys.exit(1)
        # 读全部位置统计: 新结构 位置统计/<位置>/<特征>.json
        # (内容 {位置: {测点X: {统计, 传感器编号, 特征}}})；
        # 兼容旧结构 位置统计/<位置>.json ({位置: {测点X: {特征: {统计}}} })
        feat_tree = defaultdict(dict)   # 特征 -> 位置 -> 测点 -> 记录
        for pname in sorted(os.listdir(pos_stats_dir)):
            p = os.path.join(pos_stats_dir, pname)
            if os.path.isdir(p):
                # 新结构: 位置目录下按特征拆 json
                for fn in sorted(os.listdir(p)):
                    if not fn.endswith(".json"):
                        continue
                    with open(os.path.join(p, fn), encoding="utf-8") as f:
                        data = json.load(f)
                    for pos, points in (data or {}).items():
                        if not isinstance(points, dict):
                            continue
                        for pt, rec in points.items():
                            if not isinstance(rec, dict) \
                                    or "统计" not in rec:
                                continue
                            feat = str(rec.get("特征") or "")
                            if not feat:
                                continue
                            feat_tree[feat].setdefault(pos, {})[pt] = rec
            elif pname.endswith(".json"):
                # 旧结构: 位置.json 一个文件多特征
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                for pos, points in (data or {}).items():
                    if not isinstance(points, dict):
                        continue
                    for pt, feats in points.items():
                        if not isinstance(feats, dict):
                            continue
                        for feat, rec in feats.items():
                            if not isinstance(rec, dict):
                                continue
                            feat_tree[feat].setdefault(pos, {})[pt] = rec
        if not feat_tree:
            print("[错误] 位置统计库为空")
            sys.exit(1)
        # 逐传感器每日统计（供剔除故障时间段后重算；缺失时退回整体统计）
        daily_map = collect_daily_from_stats(stats_dir, sensor_map)
        t0 = time.time()
        _label = "年度统计值" if period == "yearly" else "季度统计值"
        result = {
            "说明": _label + "(按特征汇总全桥，取各测点整体统计比较："
                    "最大值取各测点最大、最小值取各测点最小、"
                    "绝对最大值取各测点最大、差值取各测点最大、"
                    "平均值取各测点平均；整季恒为0的疑似故障测点不参与"
                    "极值/均值统计，位置记入 持续为0位置；数据来自统计库 "
                    "位置统计/ 下的逐测点整体统计)",
            "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "桥": {},
        }
        done = 0
        for feat, pos_tree in sorted(feat_tree.items()):
            pos_entries = {}      # 位置 -> 测点 -> 整体统计
            fault_period_pts = set()   # (位置, 测点) 剔除过故障时间段
            zero_periods = {}          # "位置（测点）" -> [{起始时间,结束时间,持续小时数}]
            for pos, points in sorted(pos_tree.items()):
                pos_entries[pos] = {}
                for pt, rec in sorted(points.items()):
                    st = rec.get("统计") or {}
                    sid = str(rec.get("传感器编号") or "")
                    daily = (((daily_map.get(sid) or {}).get(feat)
                              or {}).get("每日统计")) or []
                    # 图库侧已记录的连续恒0故障段（build_chart_library 写入
                    # 位置统计 的 疑似故障时间段），直接纳入故障清单
                    st_periods = st.get("疑似故障时间段") or []
                    if st_periods:
                        fault_period_pts.add((pos, pt))
                        zero_periods[
                            f"{pos}（{pt}）" if len(points) > 1 else pos] = \
                            st_periods
                    hour_cleaned = False
                    # 疑似 0 故障（非“0为正常值”特征出现 0）：读小时级 daily，
                    # 用与生成图一致的“连续恒0时间段”判定，剔除该段后重算
                    try:
                        _mx0 = float(st.get("最大值") or 0)
                        _mn0 = float(st.get("最小值") or 0)
                    except (TypeError, ValueError):
                        _mx0 = _mn0 = None
                    _zcode = re.search(r"\(([^)]+)\)$", str(feat or ""))
                    _zcode = (_zcode.group(1) if _zcode else "").lower()
                    _zero_ok = _zcode in ("nd", "spfs", "szfs") \
                        or str(feat or "").upper().startswith("LF")
                    zero_suspect = not _zero_ok \
                        and (_mn0 == 0.0 or _mx0 == 0.0)
                    if zero_suspect:
                        try:
                            fdir = os.path.join(args.daily_root, sid, feat)
                            (hours, hmeans, hmaxs, hmins, *_rest) = \
                                bcl.read_hourly_series(fdir)
                            if hours:
                                runs = bcl.detect_zero_runs(
                                    hours, hmeans,
                                    min_hours=bcl.zero_min_hours(feat))
                                if runs:
                                    h2, m2, x2, n2 = _mask_zero_run_hours(
                                        hours, hmeans, hmaxs, hmins, runs)
                                    new_st = _stats_from_hours(
                                        h2, m2, x2, n2, st)
                                    if new_st:
                                        st = new_st
                                        hour_cleaned = True
                                        fault_period_pts.add((pos, pt))
                                        zero_periods[
                                            f"{pos}（{pt}）" if len(
                                                points) > 1 else pos] = runs
                        except Exception as _exc:  # noqa: BLE001
                            # 小时级数据缺失等：退回每日统计兜底
                            pass
                    # 非 0 疑似测点：直接采用 位置统计 里的整体统计（其极值已按
                    # 特征原生粒度清洗后计算），不再用每日统计覆盖，避免“日均值
                    # 抹平”极值。0 疑似且无小时级数据时才用每日统计兜底。
                    if daily and not hour_cleaned and zero_suspect:
                        cleaned = _clean_daily_records(daily, st, feat)
                        if cleaned and len(cleaned) < len(daily):
                            fault_period_pts.add((pos, pt))
                        if cleaned:
                            new_st = compute_stats_from_daily([
                                (d.get("日期"), d.get("平均值"),
                                 d.get("最大值"), d.get("最小值"))
                                for d in cleaned])
                            if new_st:
                                # 保留缺失/有效小时等字段口径
                                for k in ("缺失小时数", "有效小时数",
                                          "有效天数", "缺失天数"):
                                    if st.get(k) is not None:
                                        new_st[k] = st[k]
                                st = new_st
                    pos_entries[pos][pt] = {
                        "统计": st,
                        "传感器编号": sid,
                    }
            # 无每日统计: 直接比较各测点的整体统计字段
            st_records = [(pos, pt.get("统计") or {})
                          for pos, pts in pos_entries.items()
                          for pt in pts.values()]
            if not st_records:
                continue
            # GNSS 位移：排除“边坡”测点——其 GNSS(Δx/Δy/Δz) 统计是大地坐标
            # 绝对值而非桥体位移，会把全桥极值污染成几米/几百米
            if feat.startswith("GNSS("):
                st_records = [r for r in st_records if "边坡" not in r[0]]
                if not st_records:
                    continue
            # 疑似故障测点：① 整季恒值（最大值==最小值，恒0 或恒非0）；或
            # ② 非“0为正常值”的特征却出现 0 污染（如结构温度 min==0 而
            # max>0）。有每日统计的测点已在上方剔除故障时间段后重算，这里
            # 只排除“剔除后仍无有效数据”的整段故障测点；被剔除过故障时间
            # 但仍有有效数据的测点照常参与统计，并记入 疑似故障传感器位置。
            def _is_suspected_fault(st, feature=""):
                try:
                    mx = float(st.get("最大值") or 0)
                    mn = float(st.get("最小值") or 0)
                except (TypeError, ValueError):
                    return False
                m = re.search(r"\(([^)]+)\)$", str(feature or ""))
                code = (m.group(1) if m else "").lower()
                zero_ok = code in ("nd", "spfs", "szfs") \
                    or str(feature or "").upper().startswith("LF")
                if zero_ok:
                    return False
                if abs(mx - mn) <= 1e-9:
                    return True
                if mn == 0.0 and mx > 0.0:
                    return True
                return False

            def _is_zero(st):
                try:
                    return (float(st.get("最大值") or 0) == 0.0
                            and float(st.get("最小值") or 0) == 0.0)
                except (TypeError, ValueError):
                    return False

            def _is_missing_severe(st, threshold=72.0):
                try:
                    mh = float(st.get("缺失小时数") or 0)
                    md = float(st.get("缺失天数") or 0)
                except (TypeError, ValueError):
                    return False
                return md > 0 or mh >= threshold

            # 位置级故障/缺失清单：同一位置多个测点时具体到测点
            def _fmt_pos(pos, pts, all_pts):
                if not pts:
                    return []
                if len(all_pts) > 1:
                    if len(pts) >= len(all_pts):
                        return [pos]
                    return [f"{pos}（{'、'.join(sorted(pts))}）"]
                return [pos]

            fault_positions, missing_positions = [], []
            for pos, pts in sorted(pos_entries.items()):
                all_pts = list(pts.keys())
                faulty = [pt for pt, rec in pts.items()
                          if (pos, pt) in fault_period_pts
                          or _is_suspected_fault((rec.get("统计") or {}), feat)]
                missing = [pt for pt, rec in pts.items()
                           if _is_missing_severe(rec.get("统计") or {})]
                fault_positions += _fmt_pos(pos, faulty, all_pts)
                missing_positions += _fmt_pos(pos, missing, all_pts)
            zero_positions = sorted({pos for pos, st in st_records
                                     if _is_zero(st)})
            st_records = [r for r in st_records
                          if not _is_suspected_fault(r[1], feat)] \
                or st_records
            def _f(key, agg):
                vals = []
                for _pos, s in st_records:
                    try:
                        v = float(s.get(key))
                    except (TypeError, ValueError):
                        continue
                    if v == v:   # 排除 NaN
                        vals.append(v)
                return agg(vals) if vals else None

            def _extreme(key, mode="max"):
                """跨全部测点找极值，返回 (极值, 监测部位)。"""
                best_v, best_pos = None, ""
                for _pos, s in st_records:
                    try:
                        v = float(s.get(key))
                    except (TypeError, ValueError):
                        continue
                    if v != v:
                        continue
                    if best_v is None:
                        best_v, best_pos = v, _pos
                    elif mode == "max" and v > best_v:
                        best_v, best_pos = v, _pos
                    elif mode == "min" and v < best_v:
                        best_v, best_pos = v, _pos
                    elif mode == "absmax" and abs(v) > abs(best_v):
                        best_v, best_pos = v, _pos
                return best_v, best_pos

            _max_v, _max_p = _extreme("最大值", "max")
            _min_v, _min_p = _extreme("最小值", "min")
            _abs_v, _abs_p = _extreme("绝对最大值", "max")
            _diff_v, _diff_p = _extreme("差值", "max")
            _rmx_v, _rmx_p = _extreme("剔除温度最大值", "max")
            _rmn_v, _rmn_p = _extreme("剔除温度最小值", "min")
            _corr_v, _corr_p = _extreme("相关性系数", "max")
            # 剔除温度差值: 每个测点 剔除温度最大值-剔除温度最小值，取最大
            _rmdiff_v, _rmdiff_p = None, ""
            for _pos, s in st_records:
                try:
                    mx = float(s.get("剔除温度最大值"))
                    mn = float(s.get("剔除温度最小值"))
                except (TypeError, ValueError):
                    continue
                if mx != mx or mn != mn:
                    continue
                d = mx - mn
                if _rmdiff_v is None or d > _rmdiff_v:
                    _rmdiff_v, _rmdiff_p = d, _pos
            stats = {
                "起始日期": min((str(s.get("起始日期")) for _p, s in st_records
                                if s.get("起始日期")), default=""),
                "结束日期": max((str(s.get("结束日期")) for _p, s in st_records
                                if s.get("结束日期")), default=""),
                "覆盖天数": _f("覆盖天数", max) or 0,
                "有效小时数": _f("有效小时数", max) or 0,
                "缺失小时数": _f("缺失小时数", max) or 0,
                "平均值": _f("平均值", lambda vs: sum(vs) / len(vs)),
                "中位数": _f("中位数", lambda vs: sum(vs) / len(vs)),
                "标准差": _f("标准差", max),
                "最大值": _f("最大值", max),
                "最小值": _f("最小值", min),
                "差值": _f("差值", max),
                "绝对最大值": _f("绝对最大值", max),
                "均方根值": _f("均方根值", max),
                # 恒0疑似故障位置（如“…一直为0℃，为传感器故障导致”）
                "持续为0位置": zero_positions,
                # 疑似故障传感器位置 / 数据缺失严重的传感器位置
                # （位置含多个测点时具体到测点，供总结段落引用）
                "疑似故障传感器位置": fault_positions,
                "数据缺失严重的传感器位置": missing_positions,
                # 疑似故障时间段（连续恒0超过阈值，与生成图标注一致）：
                # "位置（测点）" -> [{起始时间, 结束时间, 持续小时数}]
                "疑似故障时间段": zero_periods,
                # 极值对应的监测部位（供总结段落“对应测点为…/对应位置为…”引用）
                "最大值位置": _max_p or "",
                "最小值位置": _min_p or "",
                "绝对最大值位置": _abs_p or "",
                "差值位置": _diff_p or "",
            }
            if _rmx_v is not None:
                stats["剔除温度最大值"] = _rmx_v
                stats["剔除温度最大值位置"] = _rmx_p or ""
                stats["剔除温度最小值"] = _rmn_v
                stats["剔除温度最小值位置"] = _rmn_p or ""
                stats["剔除温度差值"] = _rmdiff_v
                stats["剔除温度差值位置"] = _rmdiff_p or ""
            if _corr_v is not None:
                stats["相关性系数"] = _corr_v
                stats["相关性系数位置"] = _corr_p or ""
            # 交通荷载: 数值=期内累计通过车辆数(各车道求和)，比例取最大车道占比
            _v_num = _f("数值", sum)
            if _v_num is not None:
                stats["数值"] = round(_v_num, 1)
                _v_ratio = _f("比例", max)
                if _v_ratio is not None:
                    stats["比例"] = round(_v_ratio, 2)
            if stats.get("最大值") is None:
                continue
            result["桥"].setdefault(bridge or "未归类", {})[feat] = {
                "全桥统计": stats,
                "位置": pos_entries,
            }
            done += 1
        # 季度/年度总结独立放 "季度总结" 子文件夹
        summary_dir = os.path.join(stats_dir, "季度总结")
        os.makedirs(summary_dir, exist_ok=True)
        out_name = "年度统计.json" if period == "yearly" else "季度统计.json"
        out = os.path.join(summary_dir, out_name)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[完成] 共 {done} 个特征 -> {out} "
              f"(用时 {time.time()-t0:.1f}s)")
        return

    # 年度统计: daily-root 传桥根目录, 汇总其下所有 daily_* 子目录
    if period == "yearly":
        if not os.path.isdir(args.daily_root):
            print(f"[错误] daily 根目录不存在: {args.daily_root}")
            sys.exit(1)
        sub_roots = [os.path.join(args.daily_root, d)
                     for d in sorted(os.listdir(args.daily_root))
                     if os.path.isdir(os.path.join(args.daily_root, d))
                     and (d == "daily" or d.startswith("daily"))]
        if not sub_roots:
            sub_roots = [args.daily_root]
    else:
        sub_roots = [args.daily_root]

    pairs = set()
    for sr in sub_roots:
        pairs.update(discover_pairs(sr))
    pairs = sorted(pairs)
    if not pairs:
        print(f"[错误] daily 目录为空: {args.daily_root}")
        sys.exit(1)
    if args.limit_sensors:
        keep = set(sorted({p[0] for p in pairs})[:args.limit_sensors])
        pairs = [p for p in pairs if p[0] in keep]

    # 按 (桥, 监测部位, 特征) 分组
    groups = defaultdict(lambda: {"sensors": [], "feature": None})
    for sensor, feat in pairs:
        info = sensor_map.get(sensor, {})
        bridge = info.get("桥名", "")
        loc = info.get("监测部位", "") or sensor
        key = (bridge, loc, feat)
        groups[key]["sensors"].append(sensor)
        groups[key]["feature"] = feat

    t0 = time.time()
    _label = "年度统计值" if period == "yearly" else "季度统计值"
    result = {"说明": _label + "(小时级, 同监测部位多传感器取均值)，"
                      "统计前已做物理范围过滤与尖峰清洗",
              "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "桥": {}}
    done = 0
    for (bridge, loc, feat), g in groups.items():
        hour_map = defaultdict(lambda: {"m": [], "x": [], "n": []})
        removed_total = 0
        for sensor in g["sensors"]:
            for sr in sub_roots:
                fdir = os.path.join(sr, sensor, feat)
                if not os.path.isdir(fdir):
                    continue
                (hours, means, maxs, mins, _, _, _, _, _, _) = \
                    bcl.read_hourly_series(fdir)
                if not hours:
                    continue
                keep = [i for i, h in enumerate(hours)
                        if (not args.start
                            or h.date().isoformat() >= args.start)
                        and (not args.end
                             or h.date().isoformat() <= args.end)]
                hours = [hours[i] for i in keep]
                means = [means[i] for i in keep]
                maxs = [maxs[i] for i in keep]
                mins = [mins[i] for i in keep]
                hours, means, maxs, mins, removed = clean_sensor_series(
                    hours, means, maxs, mins, feat)
                removed_total += removed
                for h, m, x, n in zip(hours, means, maxs, mins):
                    hour_map[h]["m"].append(m)
                    hour_map[h]["x"].append(x)
                    hour_map[h]["n"].append(n)
        if not hour_map:
            continue
        hours = sorted(hour_map)
        means = [float(np.mean(hour_map[h]["m"])) for h in hours]
        maxs = [float(np.max(hour_map[h]["x"])) for h in hours]
        mins = [float(np.min(hour_map[h]["n"])) for h in hours]
        stats = compute_quarter_stats(hours, means, maxs, mins)
        if stats is None:
            continue
        entry = {
            "统计": stats,
            "传感器": g["sensors"],
            "剔除异常值数": removed_total,
        }
        result["桥"].setdefault(bridge or "未归类", {}).setdefault(
            loc, {})[feat] = entry
        done += 1
        if done % 50 == 0:
            print(f"  进度 {done} 组, 用时 {time.time()-t0:.0f}s", flush=True)

    # 季度/年度总结独立放 "季度总结" 子文件夹，不与其他统计 JSON 混放
    summary_dir = os.path.join(stats_dir, "季度总结")
    os.makedirs(summary_dir, exist_ok=True)
    out_name = "年度统计.json" if period == "yearly" else "季度统计.json"
    out = os.path.join(summary_dir, out_name)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[完成] 共 {done} 个(部位,特征)组合 -> {out}")


if __name__ == "__main__":
    main()
