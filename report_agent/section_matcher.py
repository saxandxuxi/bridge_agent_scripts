# -*- coding: utf-8 -*-
"""章节标题 -> 传感器名称对照表键 的匹配器。

用途：审查时给 LLM 提供“该小节对应的表格映射键 / 传感器名称键 / 指标”，
避免 LLM 凭感觉判断某位置是否有测点（例如跨中1/2截面温湿度分左右幅，
但结构温度不分左右，只有上游/下游——插结构温度图时不能按左右幅模糊匹配）。

匹配策略（按优先级）：
  1) 关键词规则：章节标题里的指标词 -> 表格映射键 / 指标；
  2) 频次统计：该小节文本里出现次数最多的 传感器名称 键（归一化子串计数）；
  3) LLM 语义匹配（可选）：结构化输出 {表格映射, 传感器名称, 指标}。
"""

import json
import re
from typing import Dict, List, Optional


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", str(t or "")).lower()


def metric_for_section(title: str) -> str:
    """章节标题 -> 指标名（与 config.bridge_data.metrics 的键一致）。"""
    t = str(title or "")
    if "结构温度" in t:
        return "structure_temperature"
    if "温湿度" in t or ("温度" in t and "湿度" in t):
        return "temperature"      # 环境温湿度（湿度见下方规则）
    if "湿度" in t:
        return "humidity"
    if "温度" in t:
        return "temperature"
    if "应变" in t:
        return "strain"
    if "位移" in t or "空间变位" in t:
        return "displacement"
    if "支座位移" in t:
        return "bearing_displacement"
    if "挠度" in t:
        return "deflection"
    if "振动" in t:
        return "vibration"
    if "倾角" in t or "转角" in t:
        return "rotation"
    if "风速" in t or "风向" in t or "风荷载" in t:
        return "wind_speed"
    if "索力" in t:
        return "cable_force"
    if "裂缝" in t:
        return "crack"
    if "地震" in t:
        return "earthquake_load"
    if "车辆" in t or "交通" in t:
        return "vehicle_count"
    return ""


def match_table_key(name_dict: Dict, title: str) -> str:
    """章节标题 -> 名称对照表 表格映射 键（如 温湿度表 / 结构温度表）。"""
    tm = (name_dict or {}).get("表格映射") or {}
    t = str(title or "")
    metric = metric_for_section(t)
    if metric in ("temperature", "humidity"):
        return "温湿度表" if "温湿度表" in tm else ""
    if metric == "structure_temperature":
        return "结构温度表" if "结构温度表" in tm else ""
    if metric in ("displacement", "bearing_displacement"):
        return "梁端支座位移表" if "梁端支座位移表" in tm else ""
    if metric == "rotation":
        return "墩顶支座倾角表" if "墩顶支座倾角表" in tm else ""
    if metric == "crack":
        return "裂缝监测表" if "裂缝监测表" in tm else ""
    return ""


def match_position_keys(name_dict: Dict, text: str, top: int = 5) -> List[str]:
    """按出现频次统计该小节文本里命中的 传感器名称 键（子串匹配，长键优先）。"""
    sn = (name_dict or {}).get("传感器名称") or {}
    if not isinstance(sn, dict):
        sn = name_dict or {}   # 已拍平：直接就是 位置 -> 条目
    keys = sorted(sn.keys(), key=len, reverse=True)
    nt = _norm(text)
    counts = {}
    for k in keys:
        nk = _norm(k)
        if not nk or len(nk) < 2:
            continue
        c = nt.count(nk)
        if c > 0:
            counts[k] = c
    ranked = sorted(counts.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    return [k for k, _ in ranked[:top]]


def match_section(name_dict: Dict, title: str, text: str = "",
                  llm: Optional[object] = None) -> Dict:
    """返回该小节的 {表格映射, 传感器名称, 指标, 匹配方式}。

    llm 可选：具有 summarize/chat 能力的对象（如 LLMClassifier），
    用于语义匹配；不可用或失败时退回关键词+频次。
    """
    out = {
        "表格映射": match_table_key(name_dict, title),
        "传感器名称": match_position_keys(name_dict, text or title),
        "指标": metric_for_section(title),
        "匹配方式": "关键词+频次",
    }
    if llm is not None:
        try:
            resp = _llm_semantic_match(llm, name_dict, title, text)
            if resp:
                out.update(resp)
                out["匹配方式"] = "LLM语义"
        except Exception:  # noqa: BLE001
            pass
    return out


def _llm_semantic_match(llm, name_dict: Dict, title: str, text: str) -> Optional[Dict]:
    """调用 LLM 做语义匹配，结构化输出该小节对应的对照表键。"""
    tm_keys = list(((name_dict or {}).get("表格映射") or {}).keys())
    sn_keys = list(((name_dict or {}).get("传感器名称") or {}).keys())
    system = (
        "你是桥梁报告章节与传感器名称对照表的匹配专家。给定章节标题和章节文本，"
        "从给出的表格映射键、传感器名称键里选出该章节实际对应的键，"
        "只输出 JSON：{\"表格映射\": \"键名或空\", \"传感器名称\": [键...], "
        "\"指标\": \"metric或空\"}。\n"
        "注意：环境温度/湿度章节对应 温湿度表；结构温度章节对应 结构温度表；"
        "位置名必须语义一致（跨中1/2截面温湿度分左右幅，但结构温度不分左右，"
        "只有上游/下游），不要凭想象补键。"
    )
    user = (
        f"【章节标题】{title}\n【章节文本】{(text or '')[:3000]}\n"
        f"【表格映射键】{json.dumps(tm_keys, ensure_ascii=False)}\n"
        f"【传感器名称键（前200个）】{json.dumps(sn_keys[:200], ensure_ascii=False)}"
    )
    try:
        resp = llm._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"\{.*\}", str(resp or ""), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    out = {}
    if isinstance(data.get("表格映射"), str):
        out["表格映射"] = data["表格映射"]
    if isinstance(data.get("传感器名称"), list):
        out["传感器名称"] = [str(x) for x in data["传感器名称"][:5]]
    if data.get("指标"):
        out["指标"] = str(data["指标"])
    return out
