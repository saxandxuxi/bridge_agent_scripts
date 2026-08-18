# -*- coding: utf-8 -*-
"""真实监测数据适配器：直接读取“桥数据预处理”产出的统计值 JSON 与图库图片。

桥数据预处理项目（D:/Code/桥数据预处理/）会产出：
  统计值/<传感器编号>.json        每个传感器的分特征统计（中文键）
  统计值/总览.json                全部传感器-特征总览
  统计值_<期>/<桥名>/位置统计/<位置>.json
                              位置统计库：{位置: {测点X: {特征: {统计,
                              传感器编号}}}}（与图库位置目录一致）
  统计值_<期>/<桥名>/季度总结/     季度/年度聚合统计（季度统计.json、年度统计.json）
  传感器对照/传感器编号名称.json    编号 -> 中文监测部位对照（固定产物）
  传感器对照/传感器名称对照/<桥名>.json  中文名称 -> 编号/特征（固定产物）
  图库/<传感器编号>/<特征>/时间序列图.png / 频率分布图.png / 相关性_*.png

本模块把这些产物变成报告生成引擎的“数据源”：
  - resolve_cell()        解析 {{cell.<指标>.<测点>.<统计量>}} 占位符
  - resolve_metric_stat() 解析 {{stats.<指标>.<统计量>}} 占位符
  - resolve_chart()       把 {{chart.<ID>}} 映射到图库中的真实图片
  - coverage()            给 Web 管理台提供数据覆盖度 / 待补清单

测点 -> 传感器编号的匹配顺序：
  1. 配置中的 sensor_aliases 精确映射
  2. 编号（纯数字）直接命中
  3. 与“名称 / 监测部位”全等匹配
  4. 模糊包含匹配（长度加权相似度 >= fuzzy_threshold）
  5. 匹配不到 -> 回退到该指标全传感器聚合值
"""

import datetime as dt
import json
import logging
import math
import os
import re
import statistics
import difflib
from typing import Dict, List, Optional

log = logging.getLogger("report-agent.bridge")


def _bridge_name_match(a, b):
    """桥名兼容匹配：洣水河特大桥 <-> 洣水河、矮寨大桥 <-> 矮寨 都算同一桥。
    传感器对照表的桥名来自原始文档（可能不带“大桥/特大桥”），
    配置 bridge_name 用全称，二者必须兼容匹配，否则传感器会被全部过滤。"""
    if not a or not b:
        return False

    def _strip(x):
        x = str(x)
        for s in ("特大桥", "大桥"):
            if x.endswith(s):
                return x[: -len(s)]
        return x

    x, y = _strip(a), _strip(b)
    return x in y or y in x


# ---------------------------------------------------------------------------
# 常量与工具
# ---------------------------------------------------------------------------

# 模板统计量 -> 预处理统计值 JSON 的中文键
STAT_KEY_MAP = {
    "max": "最大值",
    "min": "最小值",
    "avg": "平均值",
    "mean": "平均值",
    "median": "中位数",
    "std": "标准差",
    "range": "差值",
    "abs_max": "绝对最大值",
    "rms": "均方根值",
    "value": "平均值",
    "temp_rm_max": "剔除温度最大值",
    "temp_rm_min": "剔除温度最小值",
    "temp_rm_range": "剔除温度差值",
    "corr": "相关性系数",
    "剔除温度最大值": "剔除温度最大值",
    "剔除温度最小值": "剔除温度最小值",
    "相关性系数": "相关性系数",
    "count": "覆盖天数",
    "days": "覆盖天数",
    "最大值": "最大值",
    "最小值": "最小值",
    "平均值": "平均值",
    "中位数": "中位数",
    "标准差": "标准差",
    "差值": "差值",
    "绝对最大值": "绝对最大值",
    "均方根值": "均方根值",
}

# 中文统计键 -> 英文规范键（用于 _aggregate_daily 的分支判断）
CN_STAT_MAP = {
    "最大值": "max",
    "最小值": "min",
    "平均值": "avg",
    "中位数": "median",
    "标准差": "std",
    "差值": "range",
    "绝对最大值": "abs_max",
    "均方根值": "rms",
    "覆盖天数": "days",
}

# 图表类型 -> 图库文件名（kind 归一化）
CHART_KIND_FILE = {
    "trend": "时间序列图.png",
    "timeseries": "时间序列图.png",
    "time_series": "时间序列图.png",
    "histogram": "频率分布图.png",
    "hist": "频率分布图.png",
    "bar": "时间序列图.png",
    "box": "频率分布图.png",
}


def _pick_chart_file(dirpath: str, base_name: str) -> Optional[str]:
    """取图表文件：优先 base_name；振动按天出图时退化为
    base_YYYY-MM-DD.png 中日期最新的一个(如 时间序列图_2026-03-31.png)。"""
    p = os.path.join(dirpath, base_name)
    if os.path.isfile(p):
        return p
    if os.path.isdir(dirpath):
        prefix = base_name.rsplit(".", 1)[0] + "_"
        cand = sorted(fn for fn in os.listdir(dirpath)
                      if fn.startswith(prefix) and fn.endswith(".png"))
        if cand:
            return os.path.join(dirpath, cand[-1])
    return None


# 轴/方向分量 -> 同一特征组（与 build_chart_library.feature_group 保持一致）
_AXIS_INNER = {"Δx", "Δy", "Δz", "x", "y", "z"}


def _axis_inner(feature: str) -> str:
    """提取特征括号内的轴编码，如 GNSS(Δx) -> Δx；非轴特征返回空串。"""
    m = re.match(r"^[A-Za-z0-9]+\(([^)]+)\)$", str(feature or ""))
    if not m:
        return ""
    inner = m.group(1)
    if inner in _AXIS_INNER or inner.lower() in _AXIS_INNER:
        return inner
    if inner.lower().endswith(("jd", "jsd")):
        return inner
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return inner
    return ""


# 默认指标 -> 特征名（可在 config.bridge_data.metrics 中覆盖）
DEFAULT_METRIC_FEATURES = {
    "temperature": "WSD(temp)",
    "humidity": "WSD(rh)",
    "wind_speed": "WSD(ws)",
}


def _norm(text: str) -> str:
    """归一化名称：全角转半角、去空格、统一小写。"""
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip().lower().replace(" ", "").replace("（", "(").replace("）", ")")


def _safe_dir(path_seg: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(path_seg)).strip()


def _position_similarity(a: str, b: str) -> float:
    """位置名相似度(0~1)：归一化后按公共子序列/字符重合度评估。

    用于模板占位符位置与名称对照表/图库目录名的模糊匹配，
    容忍“内/侧/梁”等修饰字差异和词序不同(如
    “上游随州侧边跨跨中箱梁顶板” vs “随州侧边跨跨中箱梁内顶板上游”)。
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    try:
        from difflib import SequenceMatcher
    except Exception:  # noqa: BLE001
        return 1.0 if (na in nb or nb in na) else 0.0
    sm = SequenceMatcher(None, na, nb)
    ratio = sm.ratio()
    # 公共子序列覆盖度：保证长位置的主要词序一致
    lcs = sum(blk.size for blk in sm.get_matching_blocks())
    cover = 2.0 * lcs / max(len(na), len(nb))
    return max(ratio, cover)


def _position_side_words(text: str) -> set:
    """提取位置里的方向/部位关键方位词(上游/下游/左/右/顶/底等)。"""
    t = _norm(text)
    out = set()
    for w in ("上游", "下游", "左幅", "右幅", "左侧", "右侧", "左", "右",
              "顶板", "底板", "顶", "底"):
        if w in t:
            out.add(w)
    # “上游侧/下游侧”归一为“上游/下游”，保证与图库/对照表命名一致
    for w in ("上游侧", "下游侧"):
        if w in t:
            out.add(w.replace("侧", ""))
    return out


def feature_group(feature: str) -> str:
    """特征编码归组：GNSS(Δx/y/z)、EZJD(xJd/yJd) 等轴分量同组；WSD(rh)/WSD(temp) 各自成组。"""
    m = re.match(r"^([A-Za-z0-9]+)\(([^)]+)\)$", feature)
    if not m:
        return feature
    prefix, inner = m.group(1), m.group(2)
    if inner in _AXIS_INNER or inner.lower() in _AXIS_INNER:
        return prefix
    if inner.lower().endswith(("jd", "jsd")):
        return prefix
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return f"{prefix}({inner[-1].lower()})"
    return feature


def _similarity(a: str, b: str) -> float:
    """包含关系加权相似度，用于模糊匹配测点名称。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b:
        return 0.5 + 0.5 * len(a) / len(b)
    if b in a:
        return 0.5 + 0.5 * len(b) / len(a)
    return 0.0


def _canon_stat(stat: str) -> str:
    """把模板统计量统一成英文规范键。"""
    return CN_STAT_MAP.get(stat, stat)


def _fuzzy_find(query: str, candidates: List[str], threshold: float) -> Optional[str]:
    best, best_score = None, 0.0
    for cand in candidates:
        score = _similarity(query, cand)
        if score > best_score:
            best, best_score = cand, score
    if best is not None and best_score >= threshold:
        return best
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class BridgeData:
    """真实监测数据适配器。"""

    def __init__(self, cfg: Optional[Dict] = None, base_dir: str = ""):
        cfg = cfg or {}
        self.cfg = cfg
        self.base_dir = base_dir
        self.bridge_name = cfg.get("bridge_name", "")
        self.fuzzy_threshold = float(cfg.get("fuzzy_threshold", 0.7))
        self.period_aggregate = bool(cfg.get("period_aggregate", True))

        self.stats_dir = self._resolve(cfg.get("stats_dir", ""))
        self.charts_dir = self._resolve(cfg.get("charts_dir", ""))
        self.sensor_map_path = self._resolve(cfg.get("sensor_map", ""))
        self.overview_path = self._resolve(cfg.get("overview", ""))
        self.name_dict_path = self._resolve(cfg.get("name_dict", ""))

        # 指标 -> 特征 映射（用户可配置）
        self.metrics: Dict[str, Dict] = {}
        for name, mcfg in (cfg.get("metrics", {}) or {}).items():
            self.metrics[name] = dict(mcfg)
            self.metrics[name].setdefault("feature", DEFAULT_METRIC_FEATURES.get(name, ""))
        # 把默认指标补进去，未配置的指标也保留（feature 可能为空）
        for name, feat in DEFAULT_METRIC_FEATURES.items():
            self.metrics.setdefault(name, {"feature": feat})

        # 传感器别名：测点描述 -> 传感器编号
        self.sensor_aliases: Dict[str, str] = cfg.get("sensor_aliases", {}) or {}
        # 图表占位符 -> 传感器编号（“20% 待完善”的人工映射表）
        self.chart_map: Dict[str, str] = cfg.get("chart_map", {}) or {}
        # 排除的传感器：编号 或 名称子串（用于绕过明显异常的数据）
        self.sensor_exclude: List[str] = [str(x) for x in (cfg.get("sensor_exclude", []) or [])]
        # 指标 -> 监测类别（回退聚合时只在该类别内取传感器，避免跨类别污染）
        self.metric_category = {
            "temperature": "温湿度", "humidity": "温湿度", "structure_temperature": "结构温度",
            "wind_speed": "风荷载", "cable_force": "索力", "displacement": "空间变位",
            "deflection": "挠度", "strain": "应变", "vibration": "振动",
            "earthquake_load": "地震",
            "rotation": "倾角", "crack": "裂缝",
        }
        # 总结段落“XXX监测数据正常稳定”的指标标签
        self._status_labels = {
            "structure_temperature": "结构温度监测数据",
            "strain": "结构应变监测数据",
            "vibration": "振动监测数据",
            "displacement": "空间变位监测数据",
            "deflection": "挠度监测数据",
            "temperature": "环境温度监测数据",
            "humidity": "环境湿度监测数据",
            "earthquake_load": "地震监测数据",
            "wind_speed": "风速监测数据",
            "cable_force": "索力监测数据",
            "rotation": "倾角监测数据",
            "crack": "裂缝监测数据",
        }

        # 运行时状态
        self.overview: Optional[List[Dict]] = None       # 总览列表
        self.sensor_map: Dict[str, Dict] = {}            # 编号 -> 名称/部位
        self.name_dict: Dict[str, List[Dict]] = {}       # 名称 -> [{编号, 特征}]（人工对照表）
        self.point_map: Dict = {}                        # 测点映射：表类 -> [{断面位置, 测点}]
        self.table_map: Dict = {}                        # 表格映射：表类 -> 墩/位置 -> {编号, 特征}
        self._category_sensors: Dict[str, List[str]] = {}  # 类别 -> 编号列表（从名称对照表）
        self._stats_cache: Dict[str, Dict] = {}          # 编号 -> 统计值 JSON
        self._agg_cache: Optional[Dict] = None            # 季度/年度统计.json 缓存
        self._sensor_features: Dict[str, List[str]] = {} # 编号 -> 特征列表
        self._match_stats = {"name_dict": 0, "alias": 0, "sensor_map": 0, "fuzzy": 0, "metric_fallback": 0}
        self._chart_seq: Dict = {}                       # (metric,位置) -> 已分配序号
        self.loaded = False
        self.load_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 初始化 / 加载
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        if self.base_dir:
            return os.path.join(self.base_dir, path)
        return path

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def load(self, force: bool = False) -> Dict:
        """加载传感器总览与对照表；返回加载状态摘要。"""
        if self.loaded and not force:
            return self.status()
        self._stats_cache.clear()
        self._pos_stats = {}     # 传感器编号 -> {位置, 测点, 特征统计}（来自位置统计库）
        self.overview = None
        self.sensor_map = {}
        self._sensor_features = {}
        self.name_dict = {}
        self.point_map = {}
        self.table_map = {}
        self._category_sensors = {}
        self._match_stats = {"name_dict": 0, "alias": 0, "sensor_map": 0, "fuzzy": 0, "metric_fallback": 0}
        self._chart_seq = {}

        try:
            # 1. 总览（传感器 -> 特征）
            if self.overview_path and os.path.isfile(self.overview_path):
                with open(self.overview_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("传感器", []) or []:
                    sid = str(item.get("编号", ""))
                    feats = list(item.get("特征", []) or [])
                    self._sensor_features[sid] = feats
                    self.sensor_map[sid] = {
                        "名称": item.get("名称", ""),
                        "桥名": item.get("桥名", ""),
                        "监测部位": item.get("名称", ""),
                    }

            # 2. 编号 -> 名称对照表（更完整时以它为准）
            if self.sensor_map_path and os.path.isfile(self.sensor_map_path):
                with open(self.sensor_map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for sid, info in (data.get("传感器", {}) or {}).items():
                    name = info.get("名称", "") or info.get("监测部位", "")
                    self.sensor_map[str(sid)] = {
                        "名称": name,
                        "桥名": info.get("桥名", ""),
                        "监测部位": info.get("监测部位", "") or name,
                        "类别": info.get("类别", ""),
                    }
                    # 特征列表（传感器编号名称.json 用“特征编码”字段；
                    # 兼容旧格式“特征”）——缺了它 sensors_for_metric 会退化为全量传感器
                    feats = list(info.get("特征编码", []) or info.get("特征", []) or [])
                    if feats:
                        self._sensor_features[str(sid)] = feats

            # 2b. 人工维护的“传感器名称 -> 编号/特征”对照表
            self._load_name_dict()

            # 2c. 位置统计库：统计值_<期>/<桥名>/位置统计/<位置>.json
            # （以“位置→测点→特征→统计”为准，传感器编号 JSON 已弃用）
            self._load_position_stats()

            self.loaded = True
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            log.exception("桥数据加载失败: %s", exc)
        return self.status()

    def _load_name_dict(self) -> None:
        """加载 传感器对照/传感器名称对照/<桥名>.json（名称 -> 编号/特征）。
        对照表是固定产物，统一放 preprocess/传感器对照/；旧布局
        (统计值_<期>/<桥名>/传感器名称对照/)仍兼容回退。"""
        path = self.name_dict_path
        if not path:
            base = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "preprocess", "传感器对照", "传感器名称对照")
            for cand in (f"{self.bridge_name}大桥.json",
                         f"{self.bridge_name}.json"):
                p = os.path.join(base, cand)
                if os.path.isfile(p):
                    path = p
                    break
        if not path and self.stats_dir:
            base = os.path.join(self.stats_dir, "传感器名称对照")
            for cand in (f"{self.bridge_name}大桥.json", f"{self.bridge_name}.json"):
                p = os.path.join(base, cand)
                if os.path.isfile(p):
                    path = p
                    break
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("传感器名称", {}) or {}
            for name, entries in raw.items():
                key = _norm(name)
                if not key:
                    continue
                self.name_dict[key] = list(entries or [])
            # 测点映射（应变/振动表：断面位置 -> 测点N -> 编号）与表格映射（位移/倾角/裂缝等）
            self.point_map = data.get("测点映射", {}) or {}
            self.table_map = data.get("表格映射", {}) or {}
            # 类别 -> 编号索引（用于指标回退时限定同类别传感器）
            cat_index = {}
            for name, entries in raw.items():
                for e in entries or []:
                    cat = e.get("特征", "")
                    sid = str(e.get("编号", ""))
                    if cat and sid:
                        cat_index.setdefault(cat, set()).add(sid)
            self._category_sensors = {k: sorted(v, key=lambda x: int(x) if x.isdigit() else x)
                                      for k, v in cat_index.items()}
            log.info("加载传感器名称对照表: %s（%d 个名称）", path, len(self.name_dict))
        except Exception as exc:  # noqa: BLE001
            log.warning("加载传感器名称对照表失败 %s: %s", path, exc)
            self.name_dict = {}

    def _pick_from_name_dict(self, key: str, metric: str) -> Optional[str]:
        """名称对照表命中后，优先选与指标特征匹配的编号。"""
        entries = self.name_dict.get(key) or []
        if not entries:
            return None
        feat = self.metrics.get(metric, {}).get("feature", "")
        for e in entries:
            sid = str(e.get("编号", ""))
            if not sid or self._is_excluded(sid):
                continue
            if feat and feat in self._sensor_features.get(sid, []):
                return sid
        for e in entries:
            sid = str(e.get("编号", ""))
            if sid and not self._is_excluded(sid):
                return sid
        return None

    def _sensors_at_position(self, pos: str, metric: str) -> List[str]:
        """返回某监测部位中支持该指标的传感器编号（按名称对照顺序）。

        指标有特征时按特征编码过滤；无特征时按“类别”过滤（如 挠度/应变/风荷载），
        避免同一位置混装多种传感器时取错（如 5#塔梁交接处主梁 同时有结构温度/应变/挠度）。
        """
        key = _norm(pos)
        entries = self.name_dict.get(key) or []
        if not entries:
            # 兼容全角/半角数字等写法差异（如 “6号” vs “六号”）
            merged = []
            for k, v in self.name_dict.items():
                kn = _norm(k)
                # 精确/包含匹配；“4#墩墩顶主梁梁端”是“…左侧/右侧”的前缀时合并两侧
                if kn == key or (len(key) >= 2 and key in kn) or (len(kn) >= 2 and kn in key):
                    merged.extend(v)
            if merged:
                entries = merged
        feat = self.metrics.get(metric, {}).get("feature", "")
        cat = self.metric_category.get(metric, "")
        sids = []
        cat_sids = []
        for e in entries:
            sid = str(e.get("编号", ""))
            if not sid or self._is_excluded(sid):
                continue
            feats = [str(x) for x in (e.get("特征编码") or [])]
            if feat and feat in feats:
                sids.append(sid)
                # 同位置混装多种传感器且共享特征编码时（如 58#墩墩顶截面
                # 同时有 地震/振动 传感器都写 DZJSD(xJsd)），按监测类别优先：
                # 振动表取“振动”传感器、地震表取“地震”传感器，避免取错。
                if cat and e.get("特征") == cat:
                    cat_sids.append(sid)
            elif not feat and cat and e.get("特征") == cat:
                sids.append(sid)
            elif not feat and not cat:
                sids.append(sid)
        if cat_sids:
            sids = cat_sids
        if not sids:
            # 墩顶支座倾角表：位置如 “4#墩墩顶主梁支座左侧Y” -> 墩号+左/右+X/Y
            if metric == "rotation" and "墩顶支座倾角表" in (self.table_map or {}):
                m = re.search(r"(\d+)#[^左右]*?(左|右)[^xyXY]*?([xyXY])", key)
                if m:
                    entry = (self.table_map["墩顶支座倾角表"].get(m.group(1) + "#")
                             or self.table_map["墩顶支座倾角表"].get(m.group(1)) or {})
                    want = _norm(m.group(2) + m.group(3))
                    e = None
                    for ek, ev in entry.items():
                        if _norm(str(ek)) == want:
                            e = ev
                            break
                    if e and e.get("编号"):
                        return [str(e["编号"])]
        if not sids:
            # 梁端支座位移表：位置如 “4#墩墩顶主梁梁端” -> 墩号 -> 左/右 传感器
            if "梁端" in key and "梁端支座位移表" in (self.table_map or {}):
                m = re.search(r"(\d+)#", key)
                if m:
                    row = (self.table_map["梁端支座位移表"].get(m.group(1) + "#")
                           or self.table_map["梁端支座位移表"].get(m.group(1)) or {})
                    out = []
                    # 位置已带方向（如 “4#墩墩顶主梁梁端左侧”）时只取对应侧，
                    # 避免左侧/右侧都合并成同一组传感器导致图注错位
                    if "左侧" in key or ("左" in key and "右" not in key):
                        sides = ("左",)
                    elif "右侧" in key or ("右" in key and "左" not in key):
                        sides = ("右",)
                    else:
                        sides = ("左", "右")
                    for side in sides:
                        e = row.get(side)
                        if e and e.get("编号"):
                            out.append(str(e["编号"]))
                    if out:
                        return out
        if not sids:
            # 表格映射兜底（结构温度表/温湿度表/裂缝监测表等的位置 -> 传感器列表）
            mkey = {
                "structure_temperature": "结构温度表",
                "temperature": "温湿度表",
                "humidity": "温湿度表",
                "crack": "裂缝监测表",
            }.get(metric, "")
            if mkey and mkey in (self.table_map or {}):
                for k, v in (self.table_map[mkey] or {}).items():
                    if _norm(k) == key:
                        return [str(x) for x in v]
        if not sids:
            # 传感器对照表兜底：位置名精确匹配（如 7LX（S）-22 索力位置）
            feat = self.metrics.get(metric, {}).get("feature", "")
            cat = self.metric_category.get(metric, "")
            for sid, info in self.sensor_map.items():
                nm = _norm(info.get("名称") or "") or _norm(info.get("监测部位") or "")
                if nm != key or self._is_excluded(sid):
                    continue
                feats = self._sensor_features.get(sid, [])
                if feat and feat in feats:
                    sids.append(str(sid))
                elif not feat and cat and info.get("类别") == cat:
                    sids.append(str(sid))
                elif not feat and not cat:
                    sids.append(str(sid))
        if not sids:
            # 位置名不一致（如 “58#墩顶部截面” vs “58#墩墩顶截面”）时，
            # 按相似度在全部位置里找有该指标传感器的相近位置，避免振动/
            # 温度等指标因位置名差异取不到数据。
            feat = self.metrics.get(metric, {}).get("feature", "")
            best_pos, best_score = None, 0.0
            for cand, entries2 in self.name_dict.items():
                cand_n = _norm(cand)
                sc = difflib.SequenceMatcher(None, key, cand_n).ratio()
                if sc <= best_score:
                    continue
                has_feat = False
                for e in entries2 or []:
                    feats2 = [str(x) for x in (e.get("特征编码") or [])]
                    if feat and feat in feats2:
                        has_feat = True
                        break
                    if (not feat and cat and e.get("特征") == cat):
                        has_feat = True
                        break
                    if not feat and not cat:
                        has_feat = True
                        break
                if has_feat:
                    best_pos, best_score = cand, sc
            if best_pos is not None and best_score >= 0.6:
                return self._sensors_at_position(best_pos, metric)
        if not sids:
            # 名称对照特征编码与实际统计库不一致（如对照表 DZJSD、
            # 实际 SZJSD）时，按实际统计库特征收集该位置的传感器。
            feat = self.metrics.get(metric, {}).get("feature", "")
            feat_inner = _axis_inner(feat)
            for sid, rec in self._pos_stats.items():
                if _norm(rec.get("位置", "")) != key:
                    continue
                if self._is_excluded(sid):
                    continue
                fstats = rec.get("特征统计") or {}
                hit = False
                if feat and feat in fstats:
                    hit = True
                elif feat_inner:
                    hit = any(_axis_inner(f) == feat_inner
                              for f in fstats)
                elif fstats:
                    hit = True
                if hit and str(sid) not in sids:
                    sids.append(str(sid))
        return sids

    def _axis_features_at_position(self, pos: str, metric: str) -> List[str]:
        """返回某位置中该指标的轴分量特征（按 X/Y/Z 顺序）。

        如 displacement -> GNSS(Δx/Δy/Δz)、earthquake_load/vibration ->
        SZJSD(xJsd/yJsd/zJsd) 或 DZJSD(xJsd)。按指标特征的前缀组收集，
        只返回括号内编码属于轴集合的特征；无轴分量返回空列表。
        """
        key = _norm(pos)
        entries = self.name_dict.get(key) or []
        if not entries:
            for k, v in self.name_dict.items():
                kn = _norm(k)
                if kn == key or (len(key) >= 2 and key in kn) \
                        or (len(kn) >= 2 and kn in key):
                    entries = list(v)
                    break
        feat = self.metrics.get(metric, {}).get("feature", "")
        # 指标特征前缀组：如 GNSS(Δx) -> GNSS、SZJSD(xJsd) -> SZJSD
        fm = re.match(r"^([A-Za-z0-9]+)\(", str(feat or ""))
        prefix = fm.group(1) if fm else ""
        feat_inner = _axis_inner(feat)

        def _candidates():
            """返回 (特征, 是否前缀匹配) 序列：先统计库后名称对照。"""
            for sid, rec in self._pos_stats.items():
                if _norm(rec.get("位置", "")) != key:
                    continue
                for f in (rec.get("特征统计") or {}):
                    yield f, (prefix and f.startswith(prefix + "("))
            for e in entries or []:
                for f in (e.get("特征编码") or []):
                    yield str(f), (prefix and str(f).startswith(prefix + "("))

        # 第一轮：前缀匹配优先（GNSS(Δx/Δy/Δz)）
        out = []
        seen = set()
        for f, is_prefix in _candidates():
            if not is_prefix or not _axis_inner(f) or f in seen:
                continue
            seen.add(f)
            out.append(f)
        if len(out) < 2:
            # 第二轮：括号内编码同族回退（如对照表 DZJSD 但数据 SZJSD）。
            # 按特征前缀分组（DZJSD/SZJSD/GNSS），取包含请求轴编码、且轴
            # 集合最完整的组——DZJSD 只有 xJsd，而 SZJSD 有 xJsd/yJsd/zJsd，
            # 地震表按方向取三行必须用 SZJSD 组。
            groups: Dict[str, List[str]] = {}
            for f, _is_prefix in _candidates():
                inner = _axis_inner(f)
                if not inner:
                    continue
                pre = re.match(r"^([A-Za-z0-9]+)\(", str(f))
                g = pre.group(1) if pre else str(f)
                groups.setdefault(g, []).append(str(f))
            best_group = []
            for gfs in groups.values():
                uniq = []
                for x in gfs:
                    if x not in uniq:
                        uniq.append(x)
                axis = [x for x in uniq if _axis_inner(x)]
                if not feat_inner:
                    continue
                if not any(_axis_inner(x) and _axis_inner(x).lower()
                           == feat_inner.lower() for x in axis):
                    continue
                if len(axis) > len(best_group):
                    best_group = axis
            if len(best_group) > len(out):
                out = best_group
        if not out and feat and feat_inner:
            out = [feat]
        # X/Y/Z 顺序稳定排序
        def _order(f):
            i = _axis_inner(f) or ""
            for pos_i, token in enumerate(("Δx", "Δy", "Δz", "x", "y", "z",
                                           "xJsd", "yJsd", "zJsd",
                                           "xJd", "yJd")):
                if token.lower() == i.lower():
                    return pos_i
            return 99
        return sorted(out, key=_order)

    def status(self) -> Dict:
        """返回加载状态摘要（供 Web 端展示）。"""
        stats_ok = bool(self.stats_dir) and os.path.isdir(self.stats_dir)
        charts_ok = bool(self.charts_dir) and os.path.isdir(self.charts_dir)
        return {
            "enabled": self.enabled,
            "loaded": self.loaded,
            "bridge_name": self.bridge_name,
            "stats_dir": self.stats_dir,
            "charts_dir": self.charts_dir,
            "stats_dir_ok": stats_ok,
            "charts_dir_ok": charts_ok,
            "sensor_count": len(self.sensor_map),
            "sensor_map_path": self.sensor_map_path,
            "name_dict_path": self.name_dict_path or (
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "preprocess", "传感器对照", "传感器名称对照")
                if os.path.isdir(os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "preprocess", "传感器对照", "传感器名称对照")) else ""),
            "name_dict_count": len(self.name_dict),
            "match_stats": dict(self._match_stats),
            "error": self.load_error,
        }

    # ------------------------------------------------------------------
    # 传感器定位
    # ------------------------------------------------------------------

    def sensors_for_metric(self, metric: str) -> List[str]:
        """返回支持某指标特征的传感器编号（按编号排序）。"""
        feat = self.metrics.get(metric, {}).get("feature", "")
        sids = []
        if feat:
            for sid, feats in self._sensor_features.items():
                if self._is_excluded(sid):
                    continue
                if feat in feats:
                    # 只取当前桥的传感器（对照表包含多座桥）
                    if self.bridge_name:
                        bname = self.sensor_map.get(sid, {}).get("桥名", "")
                        if bname and not _bridge_name_match(
                                bname, self.bridge_name):
                            continue
                    sids.append(sid)
        if not sids:
            # 特征未知时：优先用监测类别限定（振动/应变/索力…），避免跨类别污染
            cat = self.metric_category.get(metric, "")
            if cat and cat in self._category_sensors:
                sids = [sid for sid in self._category_sensors[cat] if not self._is_excluded(sid)]
        if not sids:
            # 最后才退化为全部传感器
            sids = [sid for sid in self.sensor_map
                    if not self._is_excluded(sid)
                    and (not self.bridge_name
                         or _bridge_name_match(
                             self.sensor_map[sid].get("桥名", ""),
                             self.bridge_name))]
        return sorted(sids, key=lambda x: int(x) if x.isdigit() else x)

    def _is_excluded(self, sensor_id: str) -> bool:
        if sensor_id in self.sensor_exclude:
            return True
        info = self.sensor_map.get(sensor_id, {})
        name = f"{info.get('名称', '')} {info.get('监测部位', '')}"
        return any(x and x in name for x in self.sensor_exclude if not x.isdigit())

    def find_sensor(self, metric: str, column: str) -> Optional[str]:
        """把 (指标, 测点描述) 解析成传感器编号。找不到返回 None。"""
        if not column:
            return None
        col = str(column).strip()

        if col.isdigit() and self._is_excluded(col):
            return None

        # 1. 配置别名
        alias = self.sensor_aliases.get(col) or self.sensor_aliases.get(_norm(col))
        if alias:
            self._match_stats["alias"] += 1
            return str(alias)

        # 2. 人工名称对照表（传感器对照/传感器名称对照/<桥名>.json）——精确命中率最高
        key = _norm(col)
        sid = self._pick_from_name_dict(key, metric) if key else None
        if sid:
            self._match_stats["name_dict"] += 1
            return sid

        # 3. 纯编号
        if col.isdigit():
            if col in self.sensor_map and not self._is_excluded(col):
                self._match_stats["sensor_map"] += 1
                return col
            return None

        # 候选：本桥传感器，优先看名称/监测部位
        candidates = []
        for sid, info in self.sensor_map.items():
            if self._is_excluded(sid):
                continue
            if self.bridge_name and info.get("桥名") and not _bridge_name_match(
                    info.get("桥名"), self.bridge_name):
                continue
            names = [info.get("名称", ""), info.get("监测部位", "")]
            if any(_norm(col) == _norm(n) for n in names if n):
                self._match_stats["sensor_map"] += 1
                return sid
            candidates.append((sid, names))

        # 4. 模糊包含匹配（候选含名称对照表的键，提高简称/变体命中率）
        flat = [(sid, n) for sid, names in candidates for n in names if n]
        for nk in self.name_dict:
            nsid = self._pick_from_name_dict(nk, metric)
            if nsid:
                flat.append((nsid, nk))
        best_sid, best_score = None, 0.0
        for sid, n in flat:
            if not sid:
                continue
            score = _similarity(col, n)
            if score > best_score:
                best_sid, best_score = sid, score
        if best_sid is not None and best_score >= self.fuzzy_threshold:
            self._match_stats["fuzzy"] += 1
            return best_sid

        # 5. 特征限定：在支持该指标特征的传感器里再找一次（名称可能更规范）
        metric_sids = set(self.sensors_for_metric(metric))
        if metric_sids:
            best_sid, best_score = None, 0.0
            for sid in metric_sids:
                info = self.sensor_map.get(sid, {})
                for n in (info.get("名称", ""), info.get("监测部位", "")):
                    if not n:
                        continue
                    score = _similarity(col, n)
                    if score > best_score:
                        best_sid, best_score = sid, score
            if best_sid is not None and best_score >= self.fuzzy_threshold:
                self._match_stats["fuzzy"] += 1
                return best_sid
        return None

    # ------------------------------------------------------------------
    # 统计值解析
    # ------------------------------------------------------------------

    def _load_sensor_stats(self, sensor_id: str) -> Optional[Dict]:
        if sensor_id in self._stats_cache:
            return self._stats_cache[sensor_id]
        # 位置统计库优先（位置 -> 测点N -> 特征 -> 统计）
        rec = self._pos_stats.get(str(sensor_id))
        if rec:
            data = {
                "编号": str(sensor_id),
                "名称": rec.get("位置", ""),
                "桥名": self.bridge_name,
                "特征统计": rec.get("特征统计", {}),
            }
            self._stats_cache[str(sensor_id)] = data
            return data
        path = os.path.join(self.stats_dir, f"{sensor_id}.json")
        if not os.path.isfile(path):
            self._stats_cache[sensor_id] = None
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._stats_cache[sensor_id] = data
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("读取统计值 %s 失败: %s", path, exc)
            self._stats_cache[sensor_id] = None
            return None

    def _load_position_stats(self) -> None:
        """加载 位置统计库（与图库目录结构对齐）。

        新结构:
          统计值_<期>/<桥名>/位置统计/<位置>/<特征>.json
          内容: {位置: {测点N: {"统计": {...}, "传感器编号": sid}}}
          相关性: 位置统计/<位置>/相关性_<特征A>-<特征B>.json
        兼容旧结构: 位置统计/<位置>.json
          内容: {位置: {测点N: {特征: {"统计": {...}, "传感器编号": sid}}}}
        建 传感器编号 -> (位置, 测点, 特征统计) 索引供运行时取值。
        """
        pos_dir = os.path.join(self.stats_dir, "位置统计")
        if not os.path.isdir(pos_dir):
            return
        def _index_pos(pos, points):
            """索引测点结构。

            新结构(单特征 JSON):  {位置: {测点X: {"统计": {...}, "传感器编号": sid}}}
            旧结构(多特征 JSON):  {位置: {测点X: {特征: {"统计": {...}, "传感器编号": sid}}}}
            """
            for pt, feats in (points or {}).items():
                if not isinstance(feats, dict):
                    continue
                if "统计" in feats and isinstance(feats["统计"], dict):
                    # 新结构: 单个特征，统计直接在测点下
                    sid = str(feats.get("传感器编号") or "")
                    if not sid:
                        continue
                    feat_name = str(feats.get("特征") or "")
                    self._pos_stats.setdefault(sid, {
                        "位置": str(pos), "测点": str(pt),
                        "特征统计": {},
                    })["特征统计"][feat_name] = dict(feats["统计"])
                    continue
                # 旧结构: 多个特征
                feat_stats = {}
                sid = ""
                for feat, v in feats.items():
                    if isinstance(v, dict) and "统计" in v:
                        feat_stats[str(feat)] = dict(v["统计"])
                        if not sid and v.get("传感器编号"):
                            sid = str(v["传感器编号"])
                if not sid:
                    continue
                self._pos_stats[sid] = {
                    "位置": str(pos),
                    "测点": str(pt),
                    "特征统计": feat_stats,
                }

        # 新结构: 位置统计/<位置>/<特征>.json
        try:
            pos_items = [d for d in os.listdir(pos_dir)
                         if os.path.isdir(os.path.join(pos_dir, d))]
        except OSError:
            pos_items = []
        new_loaded = 0
        for pname in pos_items:
            pdir = os.path.join(pos_dir, pname)
            try:
                for fn in os.listdir(pdir):
                    if not fn.endswith(".json"):
                        continue
                    with open(os.path.join(pdir, fn), "r",
                              encoding="utf-8") as f:
                        data = json.load(f)
                    for pos, points in (data or {}).items():
                        _index_pos(pos, points)
                        new_loaded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("读取位置统计目录 %s 失败: %s", pdir, exc)
        # 兼容旧结构: 位置统计/<位置>.json
        if not new_loaded:
            try:
                files = [f for f in os.listdir(pos_dir)
                         if f.endswith(".json")]
            except OSError:
                files = []
            for fn in files:
                try:
                    with open(os.path.join(pos_dir, fn), "r",
                              encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取位置统计 %s 失败: %s", fn, exc)
                    continue
                for pos, points in (data or {}).items():
                    _index_pos(pos, points)
        if self._pos_stats:
            log.info("位置统计库: %s（%d 个传感器）", pos_dir, len(self._pos_stats))

    def _load_aggregate_stats(self) -> Dict:
        """加载 季度统计.json / 年度统计.json(按监测部位聚合)，用于血缘回退。

        新布局: 统计值_<期>/<桥名>/季度总结/季度统计.json；
        同时兼容旧布局 统计值_<期>/<桥名>/季度统计.json。
        """
        if self._agg_cache is not None:
            return self._agg_cache
        agg = {}
        candidates = []
        for fn in ("季度统计.json", "年度统计.json"):
            candidates.append((fn, os.path.join(self.stats_dir, "季度总结", fn)))
            candidates.append((fn, os.path.join(self.stats_dir, fn)))  # 旧布局回退
        for fn, p in candidates:
            if fn in agg or not os.path.isfile(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    agg[fn] = json.load(f)
            except Exception as exc:  # noqa: BLE001
                log.warning("读取聚合统计 %s 失败: %s", p, exc)
        self._agg_cache = agg
        return agg

    def _aggregate_sensor_stat(self, sensor_id: str, metric: str, stat: str,
                               feature: str = "") -> Optional[float]:
        """通过 季度/年度统计文件(按桥+监测部位+特征)查找统计值。"""
        info = self.sensor_map.get(str(sensor_id), {})
        bridge = info.get("桥名", "") or ""
        loc = info.get("监测部位") or info.get("名称") or ""
        feat = feature or self.metrics.get(metric, {}).get("feature", "")
        if not bridge or not loc or not feat:
            return None
        agg = self._load_aggregate_stats()
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            b = next((bk for bk in bridges if bridge in bk or bk in bridge), None)
            if not b:
                continue
            # 新格式: 特征为最高键 -> {全桥统计, 位置:{位置:{测点X:{统计}}}}
            fe = bridges[b].get(feat)
            v = None
            if isinstance(fe, dict):
                if stat in ("max", "min", "avg", "abs_max", "rms", "range"):
                    v = (fe.get("全桥统计") or {}).get(
                        STAT_KEY_MAP.get(stat, stat))
                if v is None:
                    # 该位置指定测点
                    pts = ((fe.get("位置") or {}).get(loc) or {})
                    for _pt, rec in pts.items():
                        if str(rec.get("传感器编号", "")) == str(sensor_id):
                            v = (rec.get("统计") or {}).get(
                                STAT_KEY_MAP.get(stat, stat))
                            if v is not None:
                                break
            if v is None:
                # 旧格式: 位置为最高键 -> {位置:{特征:{统计}}}
                pos = bridges[b].get(loc)
                if isinstance(pos, dict):
                    fe2 = pos.get(feat)
                    if isinstance(fe2, dict):
                        v = (fe2.get("统计") or {}).get(
                            STAT_KEY_MAP.get(stat, stat))
            if v is not None:
                return v
        return None

    def _agg_feature_location(self, feature: str, stat: str) -> str:
        """从季度/年度统计 全桥统计 里取极值对应监测部位（如 最大值位置）。

        build_quarterly_stats 已把 最大值/最小值/最大值_实测/最小值_实测/
        绝对最大值/差值/剔除温度差值 等极值的位置写入 JSON，总结段落
        “对应测点为…”直接引用，避免运行时逐传感器重算。
        """
        if not feature:
            return ""
        key = STAT_KEY_MAP.get(stat, stat)
        pos_key = f"{key}位置"
        agg = self._load_aggregate_stats()
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            for b in bridges.values():
                if not isinstance(b, dict):
                    continue
                fe = b.get(feature)
                if isinstance(fe, dict):
                    v = (fe.get("全桥统计") or {}).get(pos_key)
                    if v:
                        return str(v)
        return ""

    def abnormal_positions(self, metric: str, period: Dict) -> List[str]:
        """返回该指标下存在缺失数据的监测部位。

        判定：位置统计里任一测点“缺失天数 > 0”（整日缺失），或名称对照
        里属于该特征、但统计库完全没有记录的监测部位（完全无数据）。
        """
        feat = self.metrics.get(metric, {}).get("feature", "")
        if not feat:
            return []
        out = []
        agg = self._load_aggregate_stats()
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            for b in bridges.values():
                if not isinstance(b, dict):
                    continue
                fe = b.get(feat)
                if not isinstance(fe, dict):
                    continue
                pos_entries = fe.get("位置") or {}
                if not isinstance(pos_entries, dict):
                    continue
                for pos, points in pos_entries.items():
                    if not isinstance(points, dict):
                        continue
                    for _pt, rec in points.items():
                        st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                        try:
                            miss_days = float(st.get("缺失天数") or 0)
                        except (TypeError, ValueError):
                            miss_days = 0
                        if miss_days > 0:
                            out.append(str(pos))
                            break
        # 名称对照里属于该特征、但统计库完全没有记录的监测部位（完全缺失）
        with_data = {str(rec.get("位置"))
                     for rec in self._pos_stats.values()
                     if feat in (rec.get("特征统计") or {})}
        for key, entries in (self.name_dict or {}).items():
            for e in entries or []:
                feats = [str(x) for x in (e.get("特征编码") or [])]
                if feat not in feats:
                    continue
                sid = str(e.get("编号", ""))
                info = self.sensor_map.get(sid, {}) or {}
                if self.bridge_name and info.get("桥名") \
                        and not _bridge_name_match(
                            info.get("桥名"), self.bridge_name):
                    continue
                if key not in with_data and key not in out:
                    out.append(str(key))
                break
        return out

    def resolve_data_status(self, metric: str, period: Dict) -> str:
        """总结段落状态句：无缺失 -> “XXX监测数据正常稳定”；
        有缺失 -> “位置A、位置B位置数据异常，其余XXX监测数据正常稳定”。"""
        label = (self._status_labels.get(metric)
                 or f"{self.metrics.get(metric, {}).get('label', '')}监测数据")
        abnormal = self.abnormal_positions(metric, period)
        if not abnormal:
            return f"{label}正常稳定"
        return "、".join(abnormal) + "位置数据异常，其余" + label + "正常稳定"

    def resolve_abnormal_clause(self, metric: str, period: Dict) -> str:
        """总结段落异常句首：有缺失 -> “本季度内，位置A、位置B数据出现异常，
        由设备设置错误引起。其余”；无缺失 -> 空串。"""
        abnormal = self.abnormal_positions(metric, period)
        if not abnormal:
            return ""
        return ("本季度内，" + "、".join(abnormal)
                + "数据出现异常，由设备设置错误引起。其余")

    def build_feature_summary(self, metric: str, period: Dict,
                              llm_cfg: Optional[Dict] = None) -> str:
        """基于季度/年度统计生成某指标的结论性总结（≤100 字）。

        数据源：季度总结/季度统计.json（或年度统计.json）里该特征键的
        全桥统计（极值 + 对应位置）+ 各位置缺失/持续为 0 情况。
        LLM 可用时由 LLM 生成（重点突出缺失与极值特殊位置），
        否则用规则化兜底文本。
        """
        mcfg = self.metrics.get(metric) or {}
        feat = mcfg.get("feature", "")
        label = mcfg.get("label", metric)
        unit = mcfg.get("unit", "")
        if not feat:
            return ""
        digest = self._feature_summary_digest(feat, label, unit, period, metric)
        if not digest:
            return ""
        from .llm_classifier import LLMClassifier
        text = LLMClassifier(llm_cfg or {}).summarize_feature(
            digest["prompt"], max_chars=100)
        if text:
            return text
        return digest["fallback"]

    def _feature_summary_digest(self, feature: str, label: str, unit: str,
                                period: Dict, metric: str = "") -> Optional[Dict]:
        """组装特征统计摘要（给 LLM）与规则化兜底句。"""
        agg = self._load_aggregate_stats()
        fe = None
        for data in agg.values():
            for b in (data.get("桥") or {}).values():
                if isinstance(b, dict) and feature in b:
                    fe = b.get(feature)
                    break
            if fe is not None:
                break
        if not isinstance(fe, dict):
            return None
        gs = fe.get("全桥统计") or {}
        pos_entries = fe.get("位置") or {}
        if not isinstance(pos_entries, dict):
            pos_entries = {}

        def _f(key):
            try:
                v = float(gs.get(key))
                return v if v == v else None
            except (TypeError, ValueError):
                return None

        avg = _f("平均值")
        miss_h = _f("缺失小时数")
        days = gs.get("覆盖天数") or ""
        # 极值/位置与 {{stats.<metric>.max|min[.loc]}} 同一口径（逐传感器聚合 +
        # 季度统计位置键），保证总结句与正文统计占位符一致；
        # 解析不到时回退到 全桥统计 的极值与位置键
        max_v = min_v = None
        max_loc = min_loc = ""
        if metric:
            mv, dmax = self.resolve_metric_stat_detail(metric, "max", period)
            nv, dmin = self.resolve_metric_stat_detail(metric, "min", period)
            if mv is not None:
                max_v = float(mv)
                max_loc = str((dmax or {}).get("位置") or "")
            if nv is not None:
                min_v = float(nv)
                min_loc = str((dmin or {}).get("位置") or "")
        if max_v is None:
            max_v = _f("最大值")
            max_loc = str(gs.get("最大值位置") or "")
        if min_v is None:
            min_v = _f("最小值")
            min_loc = str(gs.get("最小值位置") or "")

        # 缺失位置：缺失天数 > 0（整日缺失必报），或缺失小时数达到阈值
        # （默认 72h，bridge_data.summary_miss_hours 可调），或完全无数据
        try:
            miss_hours_thr = float(self.cfg.get("summary_miss_hours", 72) or 72)
        except (TypeError, ValueError):
            miss_hours_thr = 72.0
        miss_pos = []
        for pos, points in pos_entries.items():
            if not isinstance(points, dict):
                continue
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    mh = float(st.get("缺失小时数") or 0)
                    md = float(st.get("缺失天数") or 0)
                except (TypeError, ValueError):
                    continue
                if md > 0 or mh >= miss_hours_thr:
                    miss_pos.append(str(pos))
                    break
        # 补充完全无数据的监测部位（名称对照里属于该特征但统计库无记录）
        for p in (self.abnormal_positions(metric, period) if metric else []):
            if p not in miss_pos:
                miss_pos.append(p)
        abnormal = miss_pos
        # 持续为 0（疑似故障）位置：某位置测点 最大值==最小值==0
        zero_pos = []
        for pos, points in pos_entries.items():
            if not isinstance(points, dict):
                continue
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    mx = float(st.get("最大值"))
                    mn = float(st.get("最小值"))
                except (TypeError, ValueError):
                    continue
                if mx == 0.0 and mn == 0.0:
                    zero_pos.append(str(pos))
                    break

        prompts = [f"指标：{label}；报告期：{period.get('start')} ~ {period.get('end')}"]
        if days:
            try:
                days_txt = str(int(float(days)))
            except (TypeError, ValueError):
                days_txt = str(days)
            prompts.append(f"覆盖{days_txt}天")
        if avg is not None:
            prompts.append(f"平均值{avg:g}{unit}")
        if max_v is not None:
            prompts.append(f"最大值{max_v:g}{unit}" + (f"（位置：{max_loc}）" if max_loc else ""))
        if min_v is not None:
            prompts.append(f"最小值{min_v:g}{unit}" + (f"（位置：{min_loc}）" if min_loc else ""))
        if miss_h and miss_h >= miss_hours_thr:
            prompts.append(f"全桥缺失小时数合计{miss_h:g}")
        if abnormal:
            prompts.append("数据缺失位置：" + "、".join(abnormal))
        if zero_pos:
            prompts.append("持续为0疑似故障位置：" + "、".join(zero_pos))
        digest_text = "；".join(prompts) + "。"

        # 规则化兜底句（≤100字；恒0与缺失分开表述，避免把“0℃故障”当真实极值）
        parts = []
        if max_v is not None:
            parts.append(f"最高{max_v:g}{unit}" + (f"（{max_loc}）" if max_loc else ""))
        if min_v is not None:
            parts.append(f"最低{min_v:g}{unit}" + (f"（{min_loc}）" if min_loc else ""))
        if not parts:
            parts.append("整体正常")
        head = "、".join(parts)
        special = []
        if zero_pos:
            special.append("恒0疑似故障位置：" + "、".join(zero_pos[:2])
                           + ("等" if len(zero_pos) > 2 else ""))
        if abnormal:
            special.append("数据缺失位置：" + "、".join(abnormal[:2])
                           + ("等" if len(abnormal) > 2 else ""))
        if not special:
            fallback = f"{label}监测数据整体正常，{head}。"
        else:
            fallback = f"{head}；{'；'.join(special)}，其余测点正常，需关注。"
        if len(fallback) > 100:
            # 超长时去掉尾句，仍超长则按分号边界截断，不切断词
            fallback = f"{head}；{'；'.join(special)}。"
        if len(fallback) > 100:
            cut = fallback[:100].rfind("；")
            fallback = (fallback[:cut] if cut > 20 else fallback[:100]) + "。"
        return {"prompt": digest_text, "fallback": fallback}

    @staticmethod
    def _constant_faulty(fstats: Dict, feature: str) -> bool:
        """恒值传感器判定：整季 最大值==最小值（如一直为0/恒值）视为故障或
        无效数据，对应表格行应填“—”，且不参与 stats.* 聚合。
        例外：裂缝(LF)/挠度(ND)/风速(spfs,szfs) 等“0为正常值”的特征，
        恒为 0 属正常状态，不算故障。"""
        if not isinstance(fstats, dict):
            return False
        try:
            mx = float(fstats.get("最大值"))
            mn = float(fstats.get("最小值"))
        except (TypeError, ValueError):
            return False
        if abs(mx - mn) > 1e-9:
            return False
        m = re.search(r"\(([^)]+)\)$", str(feature or ""))
        code = (m.group(1) if m else "").lower()
        zero_ok = (code in ("nd", "spfs", "szfs")
                   or str(feature or "").upper().startswith("LF"))
        if zero_ok and mx == 0.0:
            return False
        return True

    def _feature_stats(self, sensor_id: str, metric: str, feature: str = "") -> Optional[Dict]:
        data = self._load_sensor_stats(sensor_id)
        feat = feature or self.metrics.get(metric, {}).get("feature", "")
        if data:
            stats = data.get("特征统计", {}) or {}
            if feat and feat in stats:
                return stats[feat]
            # 特征编码同族回退：对照表写 DZJSD(xJsd) 但实际统计库是
            # SZJSD(xJsd)（括号内编码一致）时，按括号内编码匹配取该传感器
            # 的真实统计，避免掉到“季度聚合回退”拿到全桥同一值。
            if feat:
                want_inner = _axis_inner(feat)
                if want_inner:
                    for _f, _st in stats.items():
                        _in = _axis_inner(_f)
                        if _in and _in.lower() == want_inner.lower():
                            return _st
            # 特征没对上时，取唯一特征；多特征则取第一个（保证有值可读）
            if len(stats) == 1:
                return next(iter(stats.values()))
            if stats and not feat:
                return next(iter(stats.values()))
        # 旧版 <编号>.json 缺失时，从位置统计库读取
        # (统计值_<期>/<桥名>/位置统计/<位置>.json -> 测点X -> 特征)
        info = self.sensor_map.get(str(sensor_id), {})
        loc = info.get("监测部位") or info.get("名称") or ""
        if loc:
            rec = self._position_stat_feature(loc, str(sensor_id), feat)
            if rec:
                fstats = dict(rec.get("统计") or {})
                dl = rec.get("每日统计")
                if isinstance(dl, list):
                    fstats["每日统计"] = dl
                return fstats
        return None

    def _position_stat_feature(self, pos: str, sensor_id: str,
                               feature: str) -> Optional[Dict]:
        """从位置统计库读取 位置/测点X/特征 的记录。"""
        if not self.stats_dir:
            return None
        import re as _re
        safe = _re.sub(r'[\\/:*?"<>|]', "_", str(pos)).strip()
        p = os.path.join(self.stats_dir, "位置统计", f"{safe}.json")
        if not os.path.isfile(p):
            # 位置名匹配图库目录时带“内/侧”差异，做模糊匹配
            pdir = os.path.join(self.stats_dir, "位置统计")
            if os.path.isdir(pdir):
                best, best_score = None, 0.0
                for fn in os.listdir(pdir):
                    if not fn.endswith(".json"):
                        continue
                    cand = fn[:-5]
                    sc = _similarity(pos, cand)
                    if sc > best_score:
                        best, best_score = fn, sc
                if best and best_score >= 0.72:
                    p = os.path.join(pdir, best)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        for _pos, points in (data or {}).items():
            if not isinstance(points, dict):
                continue
            for _pt, feats in points.items():
                if not isinstance(feats, dict):
                    continue
                for feat, rec in feats.items():
                    if not isinstance(rec, dict):
                        continue
                    if str(rec.get("传感器编号", "")) == str(sensor_id) \
                            and (not feature or feature == feat):
                        return rec
        return None

    def _sensor_stat(self, sensor_id: str, metric: str, stat: str, period: Dict,
                     feature: str = "") -> Optional[float]:
        """读取单个传感器（可指定特征）在报告期内的统计值。"""
        fstats = self._feature_stats(sensor_id, metric, feature=feature)
        if fstats and self._constant_faulty(
                fstats, feature or self.metrics.get(metric, {}).get("feature", "")):
            # 恒值传感器视为故障/无效：整行填“—”，不参与聚合
            return None
        if not fstats:
            return self._aggregate_sensor_stat(sensor_id, metric, stat,
                                               feature=feature)
        # 预计算型统计量（剔除温度/相关性系数）：只读 JSON 字段，不做逐日聚合
        if _canon_stat(stat) in ("temp_rm_max", "temp_rm_min", "corr",
                                 "剔除温度最大值", "剔除温度最小值", "相关性系数"):
            return self._full_period_stats(fstats, stat)
        # 差值/极差：报告期与 JSON 起止一致时直接读预处理好的“差值”字段，
        # 不重新聚合（避免日最大-日最小把毛刺算进去）
        if _canon_stat(stat) == "range" and self._json_period_matches(fstats, period):
            v = self._full_period_stats(fstats, "range")
            if v is not None:
                return v
        daily = self._period_daily(fstats, period)
        if daily:
            return self._aggregate_daily(daily, stat, fstats=fstats)
        return self._full_period_stats(fstats, stat)

    def _json_period_matches(self, fstats: Dict, period: Dict) -> bool:
        """统计值 JSON 的 起始日期/结束日期 是否恰好覆盖报告期。"""
        try:
            js = dt.date.fromisoformat(str(fstats.get("起始日期")))
            je = dt.date.fromisoformat(str(fstats.get("结束日期")))
            return js == period.get("start") and je == period.get("end")
        except (TypeError, ValueError):
            return False

    def _stat_detail(self, sensor_id: str, metric: str, stat: str, period: Dict,
                     feature: str = "") -> Optional[Dict]:
        """单个传感器统计 + 数据来源明细；读不到返回 None。"""
        fstats = self._feature_stats(sensor_id, metric, feature=feature)
        if fstats and self._constant_faulty(
                fstats, feature or self.metrics.get(metric, {}).get("feature", "")):
            # 恒值传感器视为故障/无效：整行填“—”，不参与聚合
            return None
        if not fstats:
            v = self._aggregate_sensor_stat(sensor_id, metric, stat,
                                            feature=feature)
            if v is None:
                return None
            info = self.sensor_map.get(str(sensor_id), {})
            return {
                "传感器编号": str(sensor_id),
                "监测部位": info.get("名称") or info.get("监测部位") or "",
                "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                "统计文件": os.path.join(self.stats_dir, "季度总结",
                                       "季度统计.json"),
                "数据来源": "季度/年度聚合统计",
                "天数": 0,
                "值": v,
            }
        info = self.sensor_map.get(str(sensor_id), {})
        feat_resolved = feature or self.metrics.get(metric, {}).get("feature", "")
        src_file = self._actual_stats_path(sensor_id, feature=feat_resolved)
        # 预计算型统计量（剔除温度/相关性系数）：只读 JSON 字段
        canon_stat = _canon_stat(stat)
        if canon_stat in ("temp_rm_max", "temp_rm_min", "temp_rm_range",
                          "corr", "剔除温度最大值", "剔除温度最小值",
                          "剔除温度差值", "相关性系数"):
            if canon_stat in ("temp_rm_range", "剔除温度差值"):
                mx = fstats.get("剔除温度最大值")
                mn = fstats.get("剔除温度最小值")
                if mx is not None and mn is not None:
                    v = float(mx) - float(mn)
                    return {
                        "传感器编号": str(sensor_id),
                        "监测部位": info.get("名称") or info.get("监测部位") or "",
                        "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                        "统计文件": src_file,
                        "数据来源": "统计值JSON预计算字段（剔除温度残差差值）",
                        "天数": 0,
                        "值": v,
                    }
                return None
            v = self._full_period_stats(fstats, stat)
            if v is not None:
                return {
                    "传感器编号": str(sensor_id),
                    "监测部位": info.get("名称") or info.get("监测部位") or "",
                    "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                    "统计文件": src_file,
                    "数据来源": "统计值JSON预计算字段",
                    "天数": 0,
                    "值": v,
                }
            return None
        # 差值/极差：报告期与 JSON 起止一致时直接读预处理好的“差值”字段
        if _canon_stat(stat) == "range" and self._json_period_matches(fstats, period):
            v = self._full_period_stats(fstats, "range")
            if v is not None:
                return {
                    "传感器编号": str(sensor_id),
                    "监测部位": info.get("名称") or info.get("监测部位") or "",
                    "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                    "统计文件": src_file,
                    "数据来源": "统计值JSON预计算差值（预处理清洗后口径）",
                    "天数": int(fstats.get("覆盖天数") or 0),
                    "值": v,
                }
        daily = self._period_daily(fstats, period)
        if daily:
            v = self._aggregate_daily(daily, stat, fstats=fstats)
            source = "报告期逐日聚合"
            days = len(daily)
        else:
            v = self._full_period_stats(fstats, stat)
            source = "统计值JSON整体统计（报告期内无逐日数据）"
            days = 0
        if v is None:
            return None
        return {
            "传感器编号": str(sensor_id),
            "监测部位": info.get("名称") or info.get("监测部位") or "",
            "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
            "统计文件": src_file,
            "数据来源": source,
            "天数": days,
            "值": v,
        }

    def _actual_stats_path(self, sensor_id: str, feature: str = "") -> str:
        """返回该传感器统计实际来源文件。

        现在统计库已改为“位置统计/<位置>/<特征>.json”结构，逐传感器的
        <编号>.json 已不再生成；血缘日志的“统计文件”应指向真实读取的文件，
        避免显示成不存在的 <编号>.json。
        """
        rec = self._pos_stats.get(str(sensor_id))
        if rec:
            loc = str(rec.get("位置") or "")
            feats = rec.get("特征统计") or {}
            if not loc:
                return os.path.join(self.stats_dir, "位置统计")
            safe = re.sub(r'[\\/:*?"<>|]', "_", loc)
            # 精确特征 -> 同族特征（DZJSD(xJsd) 实际 SZJSD(xJsd)）-> 唯一特征
            feat = ""
            if feature:
                for f in feats:
                    if f == feature:
                        feat = f
                        break
                if not feat:
                    want = _axis_inner(feature)
                    for f in feats:
                        if want and _axis_inner(f) \
                                and _axis_inner(f).lower() == want.lower():
                            feat = f
                            break
            if not feat and len(feats) == 1:
                feat = next(iter(feats))
            if feat:
                p = os.path.join(self.stats_dir, "位置统计", safe, f"{feat}.json")
                if os.path.isfile(p):
                    return p
            return os.path.join(self.stats_dir, "位置统计", safe)
        return os.path.join(self.stats_dir, f"{sensor_id}.json")

    def _traffic_lane_stat(self, lane: str) -> Optional[Dict]:
        """从 位置统计/交通荷载/交通荷载.json 取 车道X 的整体统计。

        结构: {交通荷载: {车道1: {"统计": {...}, "传感器编号": "车道1", ...}}}。
        """
        if not self.stats_dir:
            return None
        p = os.path.join(self.stats_dir, "位置统计",
                         "交通荷载", "交通荷载.json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        for _pos, points in (data or {}).items():
            if not isinstance(points, dict):
                continue
            rec = points.get(str(lane))
            if isinstance(rec, dict):
                st = rec.get("统计") or {}
                return st if isinstance(st, dict) else None
        return None

    @staticmethod
    def _extract_dunhao(title: str) -> str:
        """从表格标题提取墩号，如 '4#墩墩顶主梁梁端支座位移监测统计' -> '4'。"""
        m = re.search(r"(\d+)\s*#\s*墩", title)
        return m.group(1) if m else ""

    @staticmethod
    def _match_position(query: str, positions: List[str], threshold: float = 0.6) -> Optional[str]:
        """在候选断面位置里找与标题/列名最像的一个。

        位置词（上游/下游/左/右）常被挪到句首（“上游58#墩墩顶截面”），
        而名称对照里在句尾（“58#墩墩顶截面上游”）。SequenceMatcher 把
        主体当最长公共块后，方向词分居两侧找不到匹配，导致上游/下游
        打成平手、先遍历到的候选获胜。因此先剥离方向词匹配主体，
        再按方向词是否一致加减分。
        """
        dir_words = ("上游", "下游", "左侧", "右侧", "左", "右")

        def _direction(text: str) -> str:
            for w in dir_words:
                if w in text:
                    return w
            return ""

        qd = _direction(query)
        q_body = query.replace(qd, "") if qd else query
        best, best_score = None, -1.0
        for pos in positions:
            pd = _direction(pos)
            body = pos.replace(pd, "") if pd else pos
            score = difflib.SequenceMatcher(None, _norm(q_body), _norm(body)).ratio()
            if qd and pd:
                score += 0.15 if qd == pd else -0.25
            elif qd and not pd:
                score -= 0.10
            if score > best_score:
                best, best_score = pos, score
        return best if best is not None and best_score >= threshold else None

    def _position_temp_stat(self, pos: str, temp_stat: str,
                            period: Dict) -> Optional[float]:
        """该监测断面结构温度(WD(temp))传感器的最大/最小（多传感器跨取极值）。"""
        ids: List[str] = []
        pn = _norm(pos)
        tm = self.table_map.get("结构温度表", {}) or {}
        for k, v in tm.items():
            if _norm(str(k)) == pn:
                ids = [str(x) for x in v]
                break
        if not ids:
            for k, v in self.name_dict.items():
                if _norm(str(k)) != pn:
                    continue
                for e in v or []:
                    if "WD(temp)" in [str(x) for x in (e.get("特征编码") or [])]:
                        ids.append(str(e.get("编号")))
                if ids:
                    break
        vals = []
        for sid in ids:
            v = self._sensor_stat(sid, "structure_temperature", temp_stat,
                                  period, feature="WD(temp)")
            if v is not None:
                vals.append(v)
        if not vals:
            return None
        return max(vals) if temp_stat == "max" else min(vals)

    def _point_plan_for_row(self, plans: List[Dict], title: str, column: str,
                            row_index: int = 0):
        """在测点映射里找与“表格标题 + 行标签”匹配的断面位置。

        行标签可能是：
          - 顶板测点1 / 底板测点3 / 腹板测点2（部位词 + 测点号）
          - 上游 / 下游（纯方位行）
          - 测点2（纯测点号）
        返回 (断面位置, 传感器编号) 或 None。
        """
        if not plans:
            return None
        t = _norm(title)
        direction = ""
        for w in ("上游", "下游"):
            if w in t:
                direction = w
                break
        col = _norm(column)
        part = ""
        point_no = ""
        # column 可能是完整位置（如“上游随州侧边跨跨中箱梁顶板测点1”），
        # 也可能是简单形式（如“顶板测点1”）：用搜索而非整串匹配
        m = re.search(r"(顶板|底板|腹板|翼板)测点\s*(\d+)$", col)
        if m:
            part, point_no = m.group(1), f"测点{m.group(2)}"
        else:
            m2 = re.match(r"^(顶板|底板|腹板|翼板)(测点\s*\d+)$", col)
            if m2:
                part, point_no = m2.group(1), m2.group(2)
        # 完整位置里也可能带方位（上游/下游），从 column 提取方向补充
        if not direction:
            for w in ("上游", "下游"):
                if w in col:
                    direction = w
                    break
        elif re.match(r"^(上游|下游|左|右)$", col):
            direction = col
        elif re.match(r"^测点\s*\d+$", col):
            point_no = col
        elif not point_no:
            point_no = f"测点{row_index + 1}"
        # 标题基座：去掉 方向 / 应变监测统计 等，再取核心段
        base = re.sub(r"(上游|下游)", "", title)
        base = re.sub(r"(结构)?(应变|振动).*$", "", base)
        base = base.replace("监测", "").replace("统计", "").strip()
        core = ""
        cm = re.search(r"(.+?)(?:截面|箱梁|断面|梁段)", base)
        if cm:
            core = cm.group(1)
        elif len(base) >= 2:
            core = base
        # 1) 严格匹配：基座核心 + 部位词 + 方向 都命中
        for plan in plans:
            pos = str(plan.get("断面位置") or "")
            pn = _norm(pos)
            if direction and direction not in pn:
                continue
            if part and part not in pn:
                continue
            if core and core not in pn:
                continue
            pts = plan.get("测点") or {}
            sid = pts.get(point_no) if point_no else None
            if sid:
                return pos, str(sid)
        # 2) 模糊匹配：difflib 相似度（如 顶部 vs 墩顶）
        if core:
            for plan in plans:
                pos = str(plan.get("断面位置") or "")
                pn = _norm(pos)
                if direction and direction not in pn:
                    continue
                if part and part not in pn:
                    continue
                if difflib.SequenceMatcher(None, core, pn).ratio() < 0.5:
                    continue
                pts = plan.get("测点") or {}
                sid = pts.get(point_no) if point_no else None
                if sid:
                    return pos, str(sid)
        return None

    def _resolve_cell_by_table(self, metric: str, column: str, stat: str,
                               period: Dict, title: str,
                               row_index: int = 0,
                               trace: Optional[Dict] = None) -> Optional[float]:
        """按表格标题上下文解析单元格（测点映射 / 表格映射）。
        trace: 传入字典时记录命中的分支与传感器编号（供血缘日志）。"""
        if not title:
            return None
        t = title.replace("{{", "").replace("}}", "")

        # 1) 结构应变 / 结构振动表：断面位置 -> 测点N -> 编号
        for kw, mkey in (("应变", "结构应变监测表"), ("振动", "结构振动监测表")):
            if kw in t and mkey in self.point_map:
                plans = self.point_map[mkey]
                found = self._point_plan_for_row(plans, t, column, row_index)
                if found:
                    pos, sid = found
                    # 剔除温度最大/最小：取该断面结构温度(WD(temp))的极值，
                    # 不属于应变特征本身
                    if stat in ("temp_rm_max", "temp_rm_min",
                                "剔除温度最大值", "剔除温度最小值"):
                        temp_stat = ("max" if stat in ("temp_rm_max", "剔除温度最大值")
                                     else "min")
                        tv = self._position_temp_stat(pos, temp_stat, period)
                        if trace is not None:
                            trace.update({"branch": "应变-剔除温度→结构温度极值",
                                          "position": pos, "sensor_id": sid,
                                          "column": column})
                        return tv
                    if trace is not None:
                        trace.update({"branch": "测点映射表", "position": pos,
                                      "sensor_id": sid, "column": column})
                    return self._sensor_stat(str(sid), metric, stat, period)

        # 2) 梁端支座位移表：墩号 + 左/右
        if "位移" in t and "支座" in t and "梁端支座位移表" in self.table_map:
            dun = self._extract_dunhao(t)
            side = "左" if "左" in column else "右"
            entry = self.table_map["梁端支座位移表"]
            row = (entry.get(dun + "#") or entry.get(dun) or {})
            entry = row.get(side)
            if entry:
                if trace is not None:
                    trace.update({"branch": "梁端支座位移表", "position": f"{dun}#墩{side}",
                                  "sensor_id": str(entry.get("编号", "")), "column": column})
                return self._sensor_stat(str(entry.get("编号", "")), metric, stat, period,
                                         feature=str(entry.get("特征", "")))

        # 3) 墩顶支座倾角表：墩号 + 左/右 + X/Y
        if "倾角" in t and "墩顶支座倾角表" in self.table_map:
            dun = self._extract_dunhao(t)
            side = "左" if "左" in column else "右"
            axis = "Y" if "Y" in column else "X"
            entry = self.table_map["墩顶支座倾角表"]
            row = (entry.get(dun + "#") or entry.get(dun) or {})
            entry = row.get(side + axis)
            if entry:
                if trace is not None:
                    trace.update({"branch": "墩顶支座倾角表", "position": f"{dun}#墩{side}{axis}",
                                  "sensor_id": str(entry.get("编号", "")), "column": column})
                return self._sensor_stat(str(entry.get("编号", "")), metric, stat, period,
                                         feature=str(entry.get("特征", "")))

        # 4) 裂缝监测表：列名 -> 断面位置 -> 按表格行号取对应传感器
        if "裂缝" in t and "裂缝监测表" in self.table_map:
            pos = self._match_position(column, list(self.table_map["裂缝监测表"].keys()))
            if pos:
                ids = [str(x) for x in self.table_map["裂缝监测表"][pos]]
                if not ids:
                    return None
                # 一个监测部位有多个传感器时，按表格行号取对应传感器
                # （第1行 -> 第1个传感器，第2行 -> 第2个传感器…）
                sid = ids[row_index % len(ids)]
                if trace is not None:
                    trace.update({"branch": "裂缝监测表", "position": pos,
                                  "sensor_id": sid, "column": column})
                return self._sensor_stat(sid, metric, stat, period)

        # 5) 温湿度表 / 结构温度表：位置 -> 编号列表
        for kw, mkey in (("结构温度", "结构温度表"), ("温湿度", "温湿度表")):
            if kw in t and mkey in self.table_map:
                pos = self._match_position(column, list(self.table_map[mkey].keys()))
                if pos is None:
                    # column 带“上游…箱梁顶板测点1/底板测点3”等组合时，
                    # 去掉“测点N”后缀，再在表格映射里模糊匹配位置
                    col_no_pt = re.sub(r"(顶板|底板|腹板|翼板)?测点\s*\d+$", "",
                                       column).strip(" 、，,和及")
                    if col_no_pt and len(col_no_pt) >= 2:
                        pos = self._match_position(
                            col_no_pt, list(self.table_map[mkey].keys()))
                if pos:
                    ids = [str(x) for x in self.table_map[mkey][pos]]
                    feat = "WD(temp)" if mkey == "结构温度表" else ""
                    if not ids:
                        return None
                    # 一个监测部位有多个传感器时，按表格行号取对应传感器
                    # （第1行 -> 第1个传感器，第2行 -> 第2个传感器…）
                    sid = ids[row_index % len(ids)]
                    if trace is not None:
                        trace.update({"branch": f"{mkey}", "position": pos,
                                      "sensor_id": sid, "column": column})
                    return self._sensor_stat(sid, metric, stat, period, feature=feat)
        return None

    def _period_daily(self, fstats: Dict, period: Dict) -> List[Dict]:
        """取报告期内的每日统计列表。"""
        start = period.get("start")
        end = period.get("end")
        daily = fstats.get("每日统计", []) or []
        if not self.period_aggregate or not start or not end:
            return daily
        out = []
        for d in daily:
            try:
                day = dt.date.fromisoformat(str(d.get("日期", "")))
            except (ValueError, TypeError):
                continue
            if start <= day <= end:
                out.append(d)
        return out

    def _clean_daily(self, daily: List[Dict], fstats: Optional[Dict], stat: str) -> List[Dict]:
        """剔除缺失/异常日：全零日、0 污染日、以及数量级异常的尖峰日。"""
        if not (self.cfg.get("zero_cleanup", True) or self.cfg.get("spike_cleanup", True)):
            return daily
        overall_avg = None
        if fstats:
            try:
                overall_avg = float(fstats.get("平均值"))
            except (TypeError, ValueError):
                overall_avg = None
        # 尖峰阈值：同时用 绝对倍数 与 稳健 MAD 两种规则
        spike_thr = None
        if self.cfg.get("spike_cleanup", True):
            abs_maxs = []
            for d in daily:
                for k in ("最大值", "最小值"):
                    try:
                        v = abs(float(d.get(k)))
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        abs_maxs.append(v)
            if abs_maxs:
                sorted_v = sorted(abs_maxs)
                med = sorted_v[len(sorted_v) // 2]
                p95 = sorted_v[min(len(sorted_v) - 1, int(len(sorted_v) * 0.95))]
                mad = statistics.median([abs(v - med) for v in abs_maxs])
                thr_abs = max(med * 1000.0, p95 * 10.0)
                thr_mad = med + max(30.0 * mad, p95 * 3.0)
                # 取更严格（较小）的阈值：任一规则命中即视为尖峰
                spike_thr = max(min(thr_abs, thr_mad), 1e-9)
        out = []
        for d in daily:
            try:
                mx, mn, av = float(d.get("最大值")), float(d.get("最小值")), float(d.get("平均值"))
            except (TypeError, ValueError):
                continue
            if self.cfg.get("zero_cleanup", True) and mx == 0 and mn == 0 and av == 0:
                continue  # 全天无数据（缺失记 0）
            if self.cfg.get("zero_cleanup", True) and stat == "max" and overall_avg is not None and overall_avg < 0 and mx == 0:
                continue  # 负值传感器：每日最大值 0 来自缺失小时
            if self.cfg.get("zero_cleanup", True) and stat == "min" and overall_avg is not None and overall_avg > 0 and mn == 0:
                continue  # 正值传感器：每日最小值 0 来自缺失小时
            if spike_thr is not None and (abs(mx) > spike_thr or abs(mn) > spike_thr):
                continue  # 数量级异常的尖峰日（传感器数据毛刺）
            out.append(d)
        return out

    def _aggregate_daily(self, daily: List[Dict], stat: str,
                         fstats: Optional[Dict] = None) -> Optional[float]:
        """把每日统计聚合成报告期统计量。"""
        stat = _canon_stat(stat)
        daily = self._clean_daily(daily, fstats, stat)
        if not daily:
            return None
        means = [float(d["平均值"]) for d in daily if d.get("平均值") is not None]
        maxs = [float(d["最大值"]) for d in daily if d.get("最大值") is not None]
        mins = [float(d["最小值"]) for d in daily if d.get("最小值") is not None]
        if stat in ("avg", "mean", "value"):
            vals = means or maxs or mins
            return sum(vals) / len(vals) if vals else None
        if stat == "max":
            return max(maxs) if maxs else (max(means) if means else None)
        if stat == "min":
            return min(mins) if mins else (min(means) if means else None)
        if stat == "abs_max":
            vals = [(m, "max") for m in maxs] + [(n, "min") for n in mins]
            if not vals:
                return None
            return max(vals, key=lambda p: abs(p[0]))[0]
        if stat == "range":
            if maxs and mins:
                return max(maxs) - min(mins)
            return None
        if stat == "rms":
            vals = means or []
            return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else None
        if stat == "median":
            vals = means or []
            return statistics.median(vals) if vals else None
        if stat == "std":
            vals = means or []
            return statistics.pstdev(vals) if len(vals) > 1 else 0.0
        if stat in ("count", "days"):
            return float(len(daily))
        return None

    def _full_period_stats(self, fstats: Dict, stat: str) -> Optional[float]:
        """报告期不可用时，用 JSON 内的整体统计值。"""
        key = STAT_KEY_MAP.get(stat)
        if key and fstats.get(key) is not None:
            return float(fstats[key])
        return None

    def resolve_cell(self, metric: str, column: str, stat: str, period: Dict,
                     table_title: str = "", row_index: int = 0) -> Optional[float]:
        """解析 {{cell.<metric>.<column>.<stat>}}，可带表格标题上下文。"""
        value, _detail = self.resolve_cell_detail(metric, column, stat, period,
                                                  table_title=table_title,
                                                  row_index=row_index)
        return value

    def resolve_cell_detail(self, metric: str, column: str, stat: str, period: Dict,
                            table_title: str = "", row_index: int = 0):
        """解析 {{cell.<metric>.<column>.<stat>}}，返回 (值, 数据链路明细)。"""
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        trace: Dict = {}
        val = None
        # 0) 交通荷载(车辆计数)：cell.vehicle_count.车道X.count / .ratio
        #    数据源 统计值_<期>/<桥名>/位置统计/交通荷载/交通荷载.json（车道X 为键）
        if metric == "vehicle_count":
            st = self._traffic_lane_stat(column)
            if st:
                if stat in ("count", "数值"):
                    val = st.get("数值")
                elif stat in ("ratio", "比例"):
                    val = st.get("比例")
            if val is not None:
                detail = {
                    "占位符": f"cell.{metric}.{column}.{stat}",
                    "指标": metric, "统计量": stat,
                    "报告期": f"{period.get('start')} ~ {period.get('end')}",
                    "分支": "交通荷载位置统计库(车道X)",
                    "监测部位": "交通荷载", "表格标题": table_title,
                    "表格行号": row_index + 1,
                    "传感器": {"传感器编号": column, "监测部位": "交通荷载",
                              "特征": "交通荷载",
                              "统计文件": os.path.join(
                                  self.stats_dir, "位置统计",
                                  "交通荷载", "交通荷载.json"),
                              "数据来源": "位置统计库(交通荷载)",
                              "天数": 0, "值": val},
                    "最终值": val,
                }
                return val, detail
            return None, {
                "占位符": f"cell.{metric}.{column}.{stat}",
                "结果": "未找到",
                "原因": f"交通荷载 {column} 无 数值/比例 统计"
                        f"（位置统计/交通荷载/交通荷载.json）",
                "分支": "交通荷载位置统计库", "监测部位": column,
                "表格标题": table_title, "表格行号": row_index + 1,
            }
        # 0) 表格上下文映射（测点N / 左侧右侧 / 左X右X 等）
        if table_title:
            val = self._resolve_cell_by_table(metric, column, actual, period, table_title,
                                              row_index=row_index, trace=trace)
            if val is not None:
                self._match_stats["table_map"] = self._match_stats.get("table_map", 0) + 1
        # 1) 通用位置多传感器：column 为监测部位名时按表格行号取该位置第 N 个传感器
        if val is None:
            pos = self._match_position(column, list(self.name_dict.keys()))
            if pos:
                # 方向行（X/Y/Z 向）优先按“轴特征”选择：
                # 如 displacement 的 GNSS(Δx/Δy/Δz)、earthquake_load 的
                # SZJSD(xJsd/yJsd/zJsd) —— 同一传感器的多个轴分量特征，
                # 表格第 N 行对应第 N 个轴特征，避免三行都取第一个特征。
                axis_feats = self._axis_features_at_position(pos, metric)
                if axis_feats and len(axis_feats) >= 2:
                    idx = row_index % len(axis_feats)
                    feat = axis_feats[idx]
                    sids = self._sensors_at_position(pos, metric)
                    if sids:
                        sid = str(sids[row_index % len(sids)])
                        trace.update({"branch": "位置-轴特征按方向取",
                                      "position": pos, "sensor_id": sid,
                                      "feature": feat, "column": column})
                        val = self._sensor_stat(sid, metric, actual, period,
                                                feature=feat)
                        if val is not None:
                            self._match_stats["name_dict"] = \
                                self._match_stats.get("name_dict", 0) + 1
                if val is None:
                    sids = self._sensors_at_position(pos, metric)
                    if sids:
                        # 同一位置多个传感器时优先按行号取，若该传感器无对应
                        # 特征统计值（如对照表写 DZJSD 但实际数据是 SZJSD），
                        # 顺延到该位置下一个有值的传感器；第一轮只接受真实
                        # 逐传感器统计（跳过“季度/年度聚合”回退值，回退值会
                        # 把全桥聚合复制到每个缺失传感器上，导致多行同值）。
                        order = sids[row_index % len(sids):] + sids[:row_index % len(sids)]
                        if val is None:
                            for sid in order:
                                d_try = self._stat_detail(sid, metric, actual, period)
                                if d_try is not None and \
                                        d_try.get("数据来源") != "季度/年度聚合统计":
                                    sid = str(sid)
                                    trace.update({"branch": "名称对照位置-按行取传感器",
                                                  "position": pos, "sensor_id": sid,
                                                  "column": column})
                                    val = d_try["值"]
                                    break
                        if val is None:
                            for sid in order:
                                v_try = self._sensor_stat(sid, metric, actual, period)
                                if v_try is not None:
                                    sid = str(sid)
                                    trace.update({"branch": "名称对照位置-按行取传感器(回退聚合)",
                                                  "position": pos, "sensor_id": sid,
                                                  "column": column})
                                    val = v_try
                                    break
                        if val is not None:
                            self._match_stats["name_dict"] = self._match_stats.get("name_dict", 0) + 1
        # 2) find_sensor 直接命中
        if val is None:
            sensor_id = self.find_sensor(metric, column)
            if sensor_id:
                trace.update({"branch": "find_sensor", "sensor_id": str(sensor_id),
                              "column": column})
                val = self._sensor_stat(sensor_id, metric, actual, period)
                if val is not None:
                    trace.update({"position": self._position_for_sensor(sensor_id)})
        # 3) 找不到传感器：不聚合全指标（否则每行都填同一个值），
        #    返回 None 由上层填“—”并在血缘日志写明缺失原因
        if val is None:
            detail = {
                "占位符": f"cell.{metric}.{column}.{stat}",
                "结果": "未找到",
                "原因": (f"表格[{table_title}] 行[{row_index + 1}] 找不到 "
                         f"{metric}/{column} 对应的传感器或统计值（已关闭全指标回退）"),
                "分支": trace.get("branch", "未命中任何映射"),
                "表格标题": table_title,
                "表格行号": row_index + 1,
            }
            return None, detail
        # 组装常规明细
        sensor_id = trace.get("sensor_id")
        d = None
        if sensor_id:
            d = self._stat_detail(sensor_id, metric, actual, period)
        detail = {
            "占位符": f"cell.{metric}.{column}.{stat}",
            "指标": metric,
            "统计量": stat,
            "报告期": f"{period.get('start')} ~ {period.get('end')}",
            "分支": trace.get("branch", ""),
            "监测部位": trace.get("position") or column,
            "表格标题": table_title,
            "表格行号": row_index + 1,
            "传感器": d,
            "最终值": val,
        }
        return val, detail

    def resolve_metric_stat(self, metric: str, stat: str, period: Dict) -> Optional[float]:
        """解析 {{stats.<metric>.<stat>}}：对该指标全部传感器聚合。"""
        value, _detail = self.resolve_metric_stat_detail(metric, stat, period)
        return value

    def resolve_metric_stat_detail(self, metric: str, stat: str, period: Dict):
        """解析 {{stats.<metric>.<stat>}}，返回 (值, 数据链路明细)。

        明细包含：报告期、聚合规则、每个传感器的统计文件/天数/数值，
        供 data_lineage 日志使用。
        """
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        per_sensor = []
        # 方向化指标（如 displacement_x/y/z、vibration_x 等）：
        # 只统计对应方向特征（GNSS(Δx/Δy/Δz)、SZJSD/DZJSD(x/y/z)）
        metric_dir = ""
        mdir = re.match(r"^(.*)_([xyz])$", metric)
        if mdir:
            metric_dir = mdir.group(2).upper()
            metric = mdir.group(1)
        dir_feature = ""
        if metric_dir:
            dir_feature = {
                "X": ("GNSS(Δx)", "SZJSD(xJsd)", "DZJSD(xJsd)", "EZJD(xJd)"),
                "Y": ("GNSS(Δy)", "SZJSD(yJsd)", "DZJSD(yJsd)", "EZJD(yJd)"),
                "Z": ("GNSS(Δz)", "SZJSD(zJsd)", "DZJSD(zJsd)"),
            }.get(metric_dir, ())
        for sid in self.sensors_for_metric(metric):
            if metric_dir:
                feats = self._sensor_features.get(str(sid), []) or []
                # 方向 x/y/z 匹配括号内编码：Δx/x/xJsd/yJsd/zJsd 等
                # X->Δx/x/xJsd；Y->Δy/y/yJsd；Z->Δz/z/zJsd
                # 比较时统一小写（Δ 大写保持，避免 δx 误判）
                axis_want = {"X": {"Δx", "x", "xjsd", "xjd"},
                             "Y": {"Δy", "y", "yjsd", "yjd"},
                             "Z": {"Δz", "z", "zjsd"}}[metric_dir]
                if not any(
                        _axis_inner(f) and
                        _axis_inner(f).lower().replace("δ", "Δ") in axis_want
                        for f in feats):
                    continue
                # 找到该传感器对应方向的实际特征（GNSS(Δx)/SZJSD(xJsd)…）
                dir_feat = next((f for f in feats
                                 if _axis_inner(f) and
                                 _axis_inner(f).lower().replace("δ", "Δ") in axis_want), "")
            else:
                dir_feat = ""
            d = self._stat_detail(sid, metric, actual, period,
                                  feature=dir_feat)
            if d:
                per_sensor.append(d)
        if not per_sensor:
            return None, {
                "占位符": f"stats.{metric}.{stat}",
                "结果": "未找到",
                "原因": f"指标 {metric} 无可用传感器统计值",
            }
        vals = [d["值"] for d in per_sensor]
        if actual in ("temp_rm_max", "剔除温度最大值"):
            value, rule = max(vals), "跨传感器取剔除温度残差最大"
        elif actual in ("temp_rm_min", "剔除温度最小值"):
            value, rule = min(vals), "跨传感器取剔除温度残差最小"
        elif actual == "max":
            value, rule = max(vals), "跨传感器取最大"
        elif actual == "min":
            value, rule = min(vals), "跨传感器取最小"
        elif actual == "abs_max":
            value, rule = max(vals, key=abs), "跨传感器取绝对值最大"
        elif actual == "range":
            value, rule = max(vals), "跨传感器取最大差值（各传感器报告期内最大-最小）"
        elif actual in ("temp_rm_range", "剔除温度差值"):
            value, rule = max(vals), "跨传感器取剔除温度残差最大差值"
        elif actual == "sum":
            value, rule = sum(vals), "跨传感器求和"
        elif actual in ("count", "days"):
            value, rule = sum(vals), "跨传感器求和"
        else:
            value, rule = sum(vals) / len(vals), "跨传感器取平均"
        detail = {
            "占位符": f"stats.{metric}.{stat}",
            "指标": metric,
            "统计量": stat,
            "报告期": f"{period.get('start')} ~ {period.get('end')}",
            "聚合规则": rule,
            "传感器数": len(per_sensor),
            "逐传感器": per_sensor,
            "最终值": value,
        }
        # 最值/差值对应的监测部位（供 {{stats.<metric>.<stat>.loc}} 使用）
        # 优先读季度/年度统计 全桥统计 里的“<统计量>位置”键（build_quarterly_stats
        # 生成，与总结段落“对应测点为…”一致）；读不到再按逐传感器数据反推，
        # 且跳过“季度/年度聚合统计”回退项——回退项会把聚合值复制到每个缺失
        # 传感器的头上，导致多个传感器同值、最值位置取到第一个而失真。
        feat_for_loc = (dir_feat if metric_dir
                        else self.metrics.get(metric, {}).get("feature", ""))
        loc = self._agg_feature_location(feat_for_loc, actual)
        if not loc:
            real = [d for d in per_sensor
                    if d.get("数据来源") != "季度/年度聚合统计"]
            pool = real or per_sensor
            for d in pool:
                if abs(d["值"] - value) < 1e-9:
                    loc = d.get("监测部位") or ""
                    break
        if loc:
            detail["位置"] = loc
        return value, detail

    def estimate_days(self, period: Dict) -> int:
        """估算报告期内的数据覆盖天数（用于 {{stats.days}} 等占位符）。"""
        best = 0
        for metric, mcfg in self.metrics.items():
            if not mcfg.get("feature"):
                continue
            for sid in self.sensors_for_metric(metric)[:3]:
                fstats = self._feature_stats(sid, metric)
                if not fstats:
                    continue
                daily = self._period_daily(fstats, period)
                if daily:
                    best = max(best, len(daily))
        return best

    # ------------------------------------------------------------------
    # 图表解析
    # ------------------------------------------------------------------

    def _parse_chart_id(self, chart_id: str):
        """解析图表占位符：<metric>_<kind>_<n>。指标名可能含下划线，按已知指标优先匹配。"""
        for metric in sorted(self.metrics, key=len, reverse=True):
            prefix = metric + "_"
            if chart_id.startswith(prefix):
                rest = chart_id[len(prefix):]
                m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", rest)
                if m:
                    return metric, m.group("kind"), int(m.group("n"))
        m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", chart_id)
        if m:
            kind = m.group("kind")
            if kind.startswith("chart_"):
                kind = kind[len("chart_"):]
            return None, kind, int(m.group("n"))
        return None

    def display_name_for(self, sensor_id: str, chart_id: str, kind: str = "",
                         metric_for_label: str = "") -> str:
        """生成图名：监测部位 + 指标名 + 图型（如“第6跨跨中断面主梁箱内环境温度时程曲线图”）。"""
        info = self.sensor_map.get(str(sensor_id), {})
        loc = info.get("名称") or info.get("监测部位") or str(sensor_id)
        parsed = self._parse_chart_id(chart_id)
        metric = metric_for_label or (parsed[0] if parsed else None)
        label = self.metrics.get(metric, {}).get("label", "") if metric else ""
        if not label:
            label = info.get("类别", "")
        if not kind and parsed:
            kind = parsed[1]
        type_name = {
            "trend": "时程曲线图",
            "timeseries": "时程曲线图",
            "time_series": "时程曲线图",
            "histogram": "频率分布直方图",
            "hist": "频率分布直方图",
            "scatter": "散点图",
            "bar": "柱状图",
            "box": "箱线图",
        }.get(kind, "时程曲线图")
        return f"{loc}{label}{type_name}"

    def _metric_alias_hit(self, caption: str) -> Optional[str]:
        """在图注/上下文中找指标名（含配置的 label / feature / aliases）。"""
        for name, mcfg in self.metrics.items():
            aliases = [name, mcfg.get("label", ""), mcfg.get("feature", "")]
            aliases += list(mcfg.get("aliases", []) or [])
            if any(a and len(a) >= 2 and _norm(a) in _norm(caption) for a in aliases):
                return name
        return None

    @staticmethod
    def _has_spans(location: str) -> bool:
        """位置词是否为多跨组合（如 “第6、7跨跨中断面”）。"""
        m = re.search(r"第([\d、，,和及]+)跨", location)
        return bool(m and len(re.findall(r"\d+", m.group(1))) >= 2)

    def _location_sensors(self, metric: str, location: str) -> List[str]:
        """返回某位置相关的传感器编号（优先表格映射，其次名称匹配）。"""
        loc_n = _norm(location)
        out = []
        # 0) 多跨组合位置（“第6、7跨跨中断面”）-> 逐跨展开（第6跨->102，第7跨->105）
        m = re.search(r"第([\d、，,和及]+)跨(.{0,14})", location)
        if m:
            spans = re.findall(r"\d+", m.group(1))
            suffix = m.group(2)
            if len(spans) >= 2:
                for s in spans:
                    for sid, info in self.sensor_map.items():
                        if self._is_excluded(sid):
                            continue
                        names = [info.get("名称", ""), info.get("监测部位", "")]
                        if any(n and f"第{s}跨" in _norm(n) and _norm(suffix) in _norm(n)
                               for n in names):
                            out.append(sid)
                            break
                if out:
                    return out
        # 墩顶支座倾角表：位置含 '#墩' 时按墩号取 左X/右X
        if "墩" in loc_n:
            m = re.search(r"(\d+)#", location)
            if m and "墩顶支座倾角表" in self.table_map:
                dun = m.group(1)
                for k in ("左X", "右X"):
                    entry = self.table_map["墩顶支座倾角表"]
                    row = (entry.get(dun + "#") or entry.get(dun) or {})
                    e = row.get(k)
                    if e:
                        out.append(str(e.get("编号", "")))
        if out:
            return out
        # 裂缝监测表：位置 -> 该位置全部裂缝传感器（图按顺序分配）
        if metric == "crack" and "裂缝监测表" in self.table_map:
            pos = self._match_position(location, list(self.table_map["裂缝监测表"].keys()))
            if pos:
                return [str(x) for x in self.table_map["裂缝监测表"][pos]]
        # 包含匹配：收集该位置的全部传感器（按指标特征过滤），
        # 如“跨中断面” -> 第6跨主梁箱内、第7跨主梁箱内、第7跨桥面右侧
        feat = self.metrics.get(metric, {}).get("feature", "")
        for sid, info in self.sensor_map.items():
            if self._is_excluded(sid):
                continue
            if feat:
                feats = self._sensor_features.get(sid, [])
                if feat not in feats:
                    continue
            names = [info.get("名称", ""), info.get("监测部位", "")]
            if any(n and len(n) >= 2 and (
                (_norm(n) in loc_n) or (len(loc_n) >= 2 and loc_n in _norm(n))
            ) for n in names):
                out.append(sid)
        return sorted(set(out), key=lambda x: int(x) if x.isdigit() else x)

    def _chart_sensor_id(self, chart_id: str, caption: str = "",
                         context=None) -> Optional[str]:
        """按占位符 ID / 图注推断传感器编号。"""
        if chart_id in self.chart_map:
            return str(self.chart_map[chart_id])

        # 0) 精确传感器占位符：chart_sensor_<编号>_<图型>
        #    （如 chart_sensor_304_trend / chart_sensor_184_histogram，
        #      由“304(xJsd)_时程曲线”等行识别生成）
        m0 = re.match(r"^chart_sensor_(\d+)_([a-z_]+)$", chart_id)
        if m0:
            sid = str(m0.group(1))
            if sid in self.sensor_map or sid in self._sensor_features:
                return sid

        # 0) 位置化占位符：<metric>_<监测部位>_<kind>_<n>（如 strain_4#墩底部_trend_1）
        pp = self._parse_position_chart_id(chart_id)
        if pp:
            metric, pos, _kind, n = pp
            sids = self._sensors_at_position(pos, metric)
            if sids:
                return sids[(n - 1) % len(sids)]

        ctx_texts = context if isinstance(context, (list, tuple)) else ([context] if context else [])
        text = " ".join([caption] + [str(x) for x in ctx_texts if x]).strip()
        parsed = self._parse_chart_id(chart_id)
        metric_from_id = parsed[0] if parsed else None
        # 图注/上下文里识别指标（如“倾角” -> rotation）
        found_metric = self._metric_alias_hit(text) or metric_from_id

        # 1) 从图注/上下文提取位置（如 “4#墩墩顶主梁支座” / “第6、7跨跨中断面”）。
        #    多跨组合优先取上下文（“第6、7跨…如下图所示”这句），其次图注本身，
        #    最后上下文里的描述性图注。
        location = ""
        for ctx in ctx_texts:
            loc = self._extract_location(str(ctx))
            if loc and self._has_spans(loc):
                location = loc
                break
        if not location:
            location = self._extract_location(caption)
        if not location:
            for ctx in ctx_texts:
                location = self._extract_location(str(ctx))
                if location:
                    break
        if location:
            loc_sids = self._location_sensors(found_metric or "temperature", location)
            if loc_sids:
                # 按 (指标, 位置, 图型) 分别计数：
                # 时程图 1/2 -> 传感器 1/2，直方图 3/4 -> 传感器 1/2
                kind = parsed[1] if parsed else "trend"
                key = (found_metric or "?", _norm(location), kind)
                idx = self._chart_seq.get(key, 0)
                self._chart_seq[key] = idx + 1
                # 同一位置多个传感器时按监测部位分组再取序号，
                # 避免 5#/6#/7#/8#塔梁交接处主梁 等场景前两个序号都落在 5#
                by_pos = {}
                for sid in loc_sids:
                    p = self._position_for_sensor(sid)
                    by_pos.setdefault(p, []).append(sid)
                ordered = [v[0] for _, v in sorted(by_pos.items())]
                pool = ordered if ordered else loc_sids
                return pool[idx % len(pool)]

        # 2) 指标序号顺序分配（如 temperature_trend_2 -> 温度传感器第2个）
        if parsed and metric_from_id in self.metrics:
            sids = self.sensors_for_metric(metric_from_id)
            if sids and 1 <= parsed[2] <= len(sids):
                return sids[parsed[2] - 1]

        # 3) 泛型序号 + 指标回退（如 chart_trend_35 + 倾角 -> rotation 第35个，越界则失败）
        if parsed and parsed[0] is None and found_metric:
            sids = self.sensors_for_metric(found_metric)
            # 按监测部位分组，先位置后传感器（避免同位置多传感器重复占用前几个序号）
            by_pos = {}
            for sid in sids:
                p = self._position_for_sensor(sid)
                by_pos.setdefault(p, []).append(sid)
            ordered = [v[0] for _, v in sorted(by_pos.items())]
            if ordered and 1 <= parsed[2] <= len(ordered):
                return ordered[parsed[2] - 1]
            if sids and 1 <= parsed[2] <= len(sids):
                return sids[parsed[2] - 1]

        # 兜底：图注包含传感器名称/部位
        if caption:
            for sid, info in self.sensor_map.items():
                if self._is_excluded(sid):
                    continue
                names = [info.get("名称", ""), info.get("监测部位", "")]
                if any(n and len(n) >= 4 and _norm(n) in _norm(caption) for n in names):
                    return sid
        return None

    def _parse_position_chart_id(self, chart_id: str):
        """解析 位置化图表ID：<metric>_<监测部位>_<kind>_<n>。

        如 strain_4#墩底部_trend_1 -> ("strain", "4#墩底部", "trend", 1)。
        找不到返回 None。
        """
        if not chart_id or not self.name_dict:
            return None
        cands = self._position_candidates()
        for metric in sorted(self.metrics, key=len, reverse=True):
            prefix = metric + "_"
            if not chart_id.startswith(prefix):
                continue
            rest = chart_id[len(prefix):]
            rest_norm = _norm(rest)
            for pos in cands:
                pn = _norm(pos)
                if not rest_norm.startswith(pn + "_"):
                    continue
                after = rest[len(pos) + 1:]
                m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", after)
                if m:
                    return metric, pos, m.group("kind"), int(m.group("n"))
            # 精确前缀匹配失败：模糊匹配候选位置。
            # 模板位置与名称对照可能存在“内/侧”等修饰字差异或词序不同，
            # 但关键方位词(上游/下游/左/右/顶/底)必须一致，避免顶/底或上下游串位。
            pos_part = re.match(r"^(?P<loc>.+?)_(?P<kind>[a-z_]+)_(?P<n>\d+)$",
                                rest)
            if pos_part:
                loc_raw = pos_part.group("loc")
                kind = pos_part.group("kind")
                n = int(pos_part.group("n"))
                loc_words = _position_side_words(loc_raw)
                best = None
                best_score = 0.0
                for pos in cands:
                    cand_words = _position_side_words(pos)
                    # 关键方位词必须一致：模板有“上游”候选必须有“上游”，
                    # 模板没有的方位词候选也不得有(顶/底板除外，见下)
                    if loc_words and cand_words:
                        if not loc_words.issubset(cand_words):
                            continue
                    elif loc_words and not cand_words:
                        continue
                    elif not loc_words and cand_words:
                        # 模板没提方位但候选带方位时仍可接受(去方位词派生)
                        pass
                    # 顶板/底板、左幅/右幅等部位词必须一致
                    for kw in ("顶板", "底板", "左幅", "右幅"):
                        if kw in loc_raw and kw not in pos:
                            continue
                    score = _position_similarity(loc_raw, pos)
                    if score > best_score:
                        best_score = score
                        best = pos
                if best and best_score >= 0.72:
                    return metric, best, kind, n
        return None

    def _position_candidates(self) -> List[str]:
        """位置化图表 ID 可匹配的监测位置候选集（按长度降序）。

        覆盖：传感器名称对照表 + 表格映射位置（结构温度表/裂缝监测表等）
        + 测点映射断面 + 传感器对照表的 名称/监测部位（如 7LX（S）-22）。

        另外从带方位词的名称派生“去方位词”的通用位置（如
        “随州侧边跨跨中截面上游” -> “随州侧边跨跨中截面”），供模板中
        不带方位词的图表占位符（如 temperature_随州侧边跨跨中截面_trend_1）
        匹配；传感器查找时 _sensors_at_position 会自动合并上游/下游。
        """
        cands = set(self.name_dict.keys())
        for tname, m in (self.table_map or {}).items():
            if isinstance(m, dict):
                cands.update(str(k) for k in m.keys())
                # 梁端支座位移表：补 “4#墩墩顶主梁梁端” 组合位置
                if "梁端支座位移表" in str(tname):
                    for dun in m:
                        cands.add(f"{dun}墩墩顶主梁梁端")
        # 从名称对照已有的“左侧x/右侧x”派生“左侧Y/右侧Y”位置（字符保持一致）
        extra = set()
        for c in list(cands):
            if re.search(r"(左|右)侧x$", _norm(c)):
                extra.add(c[:-1] + "Y")
        cands.update(extra)
        for m in (self.point_map or {}).values():
            if isinstance(m, list):
                for pl in m:
                    p = (pl or {}).get("断面位置")
                    if p:
                        cands.add(str(p))
        for info in self.sensor_map.values():
            for f in ("名称", "监测部位"):
                v = info.get(f)
                if v:
                    cands.add(str(v))
        # 派生去方位词的通用位置（上游/下游/左/右/左幅/右幅/左侧/右侧）
        _SIDE_WORDS = ("上游", "下游", "左幅", "右幅", "左侧", "右侧",
                       "左", "右")
        derived = set()
        for c in cands:
            cn = _norm(c)
            for w in _SIDE_WORDS:
                if cn.endswith(w) and len(cn) > len(w):
                    derived.add(c[: len(c) - len(w)])
                    break
        cands.update(derived)
        return sorted(cands, key=len, reverse=True)

    def _extract_location(self, text: str) -> str:
        """从图注/上下文提取监测位置关键词。"""
        if not text:
            return ""
        # 0) 多跨组合位置：保留跨号并截断到指标词（如 “第6、7跨跨中断面”），供逐跨展开
        m = re.search(
            r"第\s*([\d、，,和及]+)\s*跨(?P<loc>[^，。：\s]{0,10}?)"
            r"(?=(?:环境温度|环境湿度|结构温度|温度|湿度|风速|风向|倾角|裂缝|应变|振动|"
            r"位移|挠度|索力|监测|时程|频率|分布|变化|统计|如下图所示|$))",
            text,
        )
        if m and len(re.findall(r"\d+", m.group(1))) >= 2:
            return f"第{m.group(1).replace(' ', '')}跨{m.group('loc')}"
        # 1) 墩号定位（倾角/位移表）：如 “4#墩墩顶主梁支座倾角变化时程曲线图” -> “4#墩”
        m = re.search(r"(\d+)#\s*墩", text)
        if m:
            return m.group(0).replace(" ", "")
        # 2) 传感器名称完整出现在文本中（最长优先）
        best = ""
        for info in self.sensor_map.values():
            for n in (info.get("名称", ""), info.get("监测部位", "")):
                if n and len(n) > len(best) and _norm(n) in _norm(text):
                    best = n
        if best:
            return best
        # 3) 去掉指标/图型词后的残余位置词
        t = text
        for w in ("时程曲线图", "频率分布直方图", "时间序列图", "时间序列", "直方图", "曲线图",
                  "如下图所示", "如下", "变化趋势", "变化", "监测统计", "监测数据", "统计",
                  "监测", "数据", "测点布置图", "布置图", "示意图", "平面图",
                  "倾角", "结构温度", "温度", "湿度", "风速", "风向", "位移", "应变", "振动",
                  "挠度", "索力", "裂缝", "环境", "结构", "截面", "分布"):
            t = t.replace(w, "")
        t = re.sub(r"[a-z_]+", " ", t)
        t = re.sub(r"[、，。：；！？\s]+", " ", t).strip()
        t = " ".join(t.split())
        # 保留位置里的数字（如“第五跨L/4处主梁”），纯数字会被下面的中文检查剔除
        if len(t) < 2 or not re.search(r"[\u4e00-\u9fa5]", t):
            return ""
        return t

    def resolve_chart(self, chart_id: str, caption: str = "", context: str = "") -> Optional[str]:
        """把图表占位符解析为图库图片路径；找不到返回 None。"""
        info = self.resolve_chart_info(chart_id, caption, context)
        return info["path"] if info else None

    def resolve_chart_info(self, chart_id: str, caption: str = "", context=None,
                           metric_hint: str = "", sensor_hint: str = "",
                           feature_hint: str = "") -> Optional[Dict]:
        """解析图表，返回 {path, sensor_id, kind, display}；找不到返回 None。"""
        if not self.charts_dir:
            return None
        sensor_id = sensor_hint or self._chart_sensor_id(chart_id, caption, context)
        if not sensor_id:
            return None

        # 确定特征与图片文件名
        parsed = self._parse_chart_id(chart_id)
        pp = self._parse_position_chart_id(chart_id)
        ms = re.match(r"^chart_sensor_\d+_([a-z_]+)$", chart_id)
        if pp:
            metric_from_id, _pos, kind = pp[0], pp[1], pp[2]
        elif ms:
            metric_from_id = None
            kind = ms.group(1)
        else:
            metric_from_id = parsed[0] if parsed else None
            kind = parsed[1] if parsed else "trend"
        if kind not in CHART_KIND_FILE:
            if kind in ("scatter", "correlation"):
                # 相关性散点图：图库没有对应图时返回 None（由上层生成占位图），
                # 不要退化为时程图导致“散点图位置插入时程图”
                return None
            kind = "trend"

        # 优先：合并图库（图库/<监测部位>/<特征组>/<图型>.png）
        # 特征选择：节上下文推断的指标(metric_hint) > chart_id 前缀指标 > 特征提示
        metric_feature = ""
        if metric_hint and metric_hint in self.metrics:
            metric_feature = self.metrics[metric_hint].get("feature", "")
        if not metric_feature and metric_from_id and metric_from_id in self.metrics:
            metric_feature = self.metrics[metric_from_id].get("feature", "")
        if not metric_feature and feature_hint:
            metric_feature = feature_hint
        merged = self._merged_chart_path(sensor_id, kind, metric_feature)
        if merged:
            display_metric = metric_hint or self._metric_alias_hit(
                caption
            ) or None
            return {
                "path": merged,
                "sensor_id": sensor_id,
                "kind": kind,
                "display": self.display_name_for(sensor_id, chart_id, kind,
                                                  metric_for_label=display_metric),
            }

        feat_dir = self._feature_dir_for_sensor(sensor_id, chart_id, kind,
                                                feature_hint=feature_hint)
        if not feat_dir:
            return None
        fname = CHART_KIND_FILE.get(kind, "时间序列图.png")
        if kind in ("scatter", "correlation"):
            # 相关性图在特征目录下: 相关性_<特征A>_<特征B>.png（此处不强行匹配）
            fname = "相关性_" + feat_dir + ".png"
        path = _pick_chart_file(
            os.path.join(self.charts_dir, sensor_id, feat_dir), fname)
        if not path:
            return None
        display_metric = metric_hint or self._metric_alias_hit(caption) or None
        return {
            "path": path,
            "sensor_id": sensor_id,
            "kind": kind,
            "display": self.display_name_for(sensor_id, chart_id, kind,
                                              metric_for_label=display_metric),
        }

    def chart_png_for(self, sensor_id: str, kind: str, metric: str = "") -> Optional[str]:
        """按传感器编号 + 图型直接取图库图片（用于缺图自动补齐，不消耗顺序计数）。"""
        if not self.charts_dir:
            return None
        kind = kind if kind in CHART_KIND_FILE else "trend"
        metric_feature = self.metrics.get(metric, {}).get("feature", "") if metric else ""
        merged = self._merged_chart_path(sensor_id, kind, metric_feature)
        if merged:
            return merged
        feat_dir = ""
        feat = self.metrics.get(metric, {}).get("feature", "") if metric else ""
        if feat and os.path.isdir(os.path.join(self.charts_dir, str(sensor_id), feat)):
            feat_dir = feat
        else:
            feat_dir = self._feature_dir_for_sensor(
                str(sensor_id), f"{metric}_{kind}_1" if metric else f"{kind}_1", kind) or ""
        if not feat_dir:
            return None
        path = os.path.join(self.charts_dir, str(sensor_id), feat_dir, CHART_KIND_FILE.get(kind, "时间序列图.png"))
        return path if os.path.isfile(path) else None

    def _feature_dir_for_sensor(self, sensor_id: str, chart_id: str, kind: str,
                                feature_hint: str = "") -> Optional[str]:
        """确定传感器目录下用哪个特征子目录。"""
        # 0) 精确特征提示（如 “xJsd” -> DZJSD(xJsd)）：按包含关系匹配
        if feature_hint:
            base = os.path.join(self.charts_dir, str(sensor_id))
            hint_n = _norm(feature_hint)
            if os.path.isdir(base):
                for d in os.listdir(base):
                    if hint_n and hint_n in _norm(d):
                        return d
        # 优先：图表占位符中的指标对应的特征
        parsed = self._parse_chart_id(chart_id)
        if parsed and parsed[0] in self.metrics:
            feat = self.metrics[parsed[0]].get("feature", "")
            if feat and os.path.isdir(os.path.join(self.charts_dir, sensor_id, feat)):
                return feat
        # 其次：传感器已有特征里选一个（优先该指标特征，否则第一个）
        feats = self._sensor_features.get(sensor_id, [])
        if feats:
            return feats[0]
        base = os.path.join(self.charts_dir, sensor_id)
        if os.path.isdir(base):
            subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
            return subs[0] if subs else None
        return None

    def chart_siblings(self, path: str) -> List[str]:
        """同一图表拆分出的多张图(时间序列图_2.png / _3.png ...)，
        按序号返回；找不到返回空列表。"""
        if not path:
            return []
        base, ext = os.path.splitext(path)
        out = []
        k = 2
        while True:
            p = f"{base}_{k}{ext}"
            if os.path.isfile(p):
                out.append(p)
                k += 1
            else:
                break
        return out

    def _position_for_sensor(self, sensor_id: str) -> str:
        """从名称对照表反查传感器所在位置名（与合并图库目录同名）。
        找不到时回退到传感器对照表的 名称/监测部位。"""
        sid = str(sensor_id)
        for pos, entries in self.name_dict.items():
            for e in entries or []:
                if str(e.get("编号", "")) == sid:
                    return pos
        info = self.sensor_map.get(sid, {})
        return info.get("名称") or info.get("监测部位") or ""

    def _merged_chart_path(self, sensor_id: str, kind: str,
                           metric_feature: str = "") -> Optional[str]:
        """合并图库路径，按优先级查找：
          1) 图库/<监测部位>/<特征组>/<图型>.png（多传感器子图）
          2) 图库/<监测部位>/<特征组>/<特征>/<图型>.png（单传感器多特征复制布局）
         没有指标特征时(如 chart_sensor_304_trend 直接按编号取图)，
         自动在位置目录下按传感器特征选特征组子目录。
         精确目录不存在时，在图库位置目录中做模糊匹配（关键方位词一致、
         容忍“内/侧”等修饰字差异），避免“名称对照多一个字”导致找不到图。
        找不到返回 None。"""
        if not self.charts_dir:
            return None
        pos = self._position_for_sensor(sensor_id)
        if not pos:
            return None
        fname = CHART_KIND_FILE.get(kind, "时间序列图.png")
        base_dir = self._fuzzy_position_dir(pos)
        if metric_feature:
            g = feature_group(metric_feature)
            p = _pick_chart_file(
                os.path.join(base_dir, _safe_dir(g)), fname)
            if not p:
                # 特征提示(如 xJsd)与实际特征组目录(如 DZJSD)不一致时，
                # 按传感器特征组逐个尝试
                feats = self._sensor_features.get(str(sensor_id), []) or []
                for f in feats:
                    g2 = feature_group(f)
                    if g2 == g:
                        continue
                    p = _pick_chart_file(
                        os.path.join(base_dir, _safe_dir(g2)), fname)
                    if p:
                        break
        else:
            # 无指标特征：按传感器特征组逐个尝试，找不到再扫描位置目录
            p = None
            feats = self._sensor_features.get(str(sensor_id), []) or []
            for f in feats:
                g = feature_group(f)
                p = _pick_chart_file(
                    os.path.join(base_dir, _safe_dir(g)), fname)
                if p:
                    break
            if not p and os.path.isdir(base_dir):
                for sub in sorted(os.listdir(base_dir)):
                    subp = os.path.join(base_dir, sub)
                    if os.path.isdir(subp):
                        p = _pick_chart_file(subp, fname)
                        if p:
                            break
        if p:
            return p
        if metric_feature:
            p2 = _pick_chart_file(
                os.path.join(base_dir, _safe_dir(g),
                             _safe_dir(metric_feature)), fname)
            if p2:
                return p2
        return None

    def _fuzzy_position_dir(self, pos: str) -> str:
        """返回图库中与 pos 最匹配的位置目录；精确目录优先，找不到做模糊匹配。"""
        exact = os.path.join(self.charts_dir, _safe_dir(pos))
        if os.path.isdir(exact):
            return exact
        if not os.path.isdir(self.charts_dir):
            return exact
        loc_words = _position_side_words(pos)
        best = None
        best_score = 0.0
        try:
            names = os.listdir(self.charts_dir)
        except OSError:
            return exact
        for name in names:
            if not os.path.isdir(os.path.join(self.charts_dir, name)):
                continue
            cand_words = _position_side_words(name)
            if loc_words and cand_words and not loc_words.issubset(cand_words):
                continue
            for kw in ("顶板", "底板", "左幅", "右幅"):
                if kw in pos and kw not in name:
                    continue
            score = _position_similarity(pos, name)
            if score > best_score:
                best_score = score
                best = name
        if best and best_score >= 0.72:
            return os.path.join(self.charts_dir, best)
        return exact

    # ------------------------------------------------------------------
    # 待补图表占位图
    # ------------------------------------------------------------------

    def make_placeholder_chart(self, chart_id: str, reason: str, out_dir: str) -> str:
        """为解析不到的图表生成一张明显的占位图，避免生成流程中断。"""
        os.makedirs(out_dir, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", str(chart_id))
        path = os.path.join(out_dir, f"pending_{safe}.png")
        try:
            from PIL import Image, ImageDraw, ImageFont
            w, h = 1200, 700
            img = Image.new("RGB", (w, h), "white")
            draw = ImageDraw.Draw(img)
            draw.rectangle([8, 8, w - 8, h - 8], outline="#b0b0b0", width=4)
            font = self._pick_font(size=40)
            small = self._pick_font(size=28)
            draw.text((w / 2, h / 2 - 60), "图表待补充", font=font, fill="#c0392b", anchor="mm")
            draw.text((w / 2, h / 2 + 30), f"占位符: {chart_id}", font=small, fill="#555555", anchor="mm")
            draw.text((w / 2, h / 2 + 90), reason or "未匹配到图库图片", font=small, fill="#888888", anchor="mm")
            img.save(path)
            return path
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _pick_font(size: int):
        for fp in (
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ):
            if os.path.isfile(fp):
                try:
                    from PIL import ImageFont
                    return ImageFont.truetype(fp, size)
                except Exception:  # noqa: BLE001
                    continue
        from PIL import ImageFont
        return ImageFont.load_default()

    # ------------------------------------------------------------------
    # 覆盖度 / 待补清单
    # ------------------------------------------------------------------

    def coverage(self) -> Dict:
        """生成数据覆盖度报告（供 Web 端展示）。"""
        if not self.loaded:
            self.load()
        metrics_out = []
        scan_cap = int(self.cfg.get("coverage_scan_cap", 100))
        for metric, mcfg in self.metrics.items():
            feat = mcfg.get("feature", "")
            sids = self.sensors_for_metric(metric)
            sampled = False
            scan_sids = sids
            if not feat and len(sids) > scan_cap:
                scan_sids = sids[:scan_cap]
                sampled = True
            entry = {
                "metric": metric,
                "label": mcfg.get("label", metric),
                "feature": feat,
                "unit": mcfg.get("unit", ""),
                "sensor_count": len(sids),
                "sampled": sampled,
                "scanned": len(scan_sids),
                "with_data": 0,
                "first_day": None,
                "last_day": None,
            }
            for sid in scan_sids:
                fstats = self._feature_stats(sid, metric)
                if not fstats:
                    continue
                entry["with_data"] += 1
                fd = fstats.get("起始日期") or fstats.get("覆盖天数")
                ld = fstats.get("结束日期")
                if fd and (entry["first_day"] is None or str(fd) < str(entry["first_day"])):
                    entry["first_day"] = fd
                if ld and (entry["last_day"] is None or str(ld) > str(entry["last_day"])):
                    entry["last_day"] = ld
            metrics_out.append(entry)
        return {
            "bridge_name": self.bridge_name,
            "status": self.status(),
            "metrics": metrics_out,
            "sensor_count": len(self.sensor_map),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
