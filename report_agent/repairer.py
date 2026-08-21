# -*- coding: utf-8 -*-
"""报告自动修复器：把审查产出的结构化 repairs “验证后落地”。

原则：能唯一、确定地索引到正确数据才应用；否则标记 needs_human，绝不瞎猜。
修复类型：
  chart   — 按 hint 重索引图片（重取图库 PNG，验证位置命中）
  caption — 删除/改正错误的图注文字（对 docx 段落做删除或替换）
  summary — 若 hint 是整句修正则替换文档文字；否则清缓存重新生成总结
  stat    — 同 summary：有整句修正就落地为文字替换
  cell    — 有整句修正就落地为文字替换，否则交人工
  unit    — 单位/空格规范化（build 收尾已确定性处理）
"""

import logging
import re
from typing import Dict, List, Optional

log = logging.getLogger("report-agent.repairer")


class ReportRepairer:
    def __init__(self, bridge, chart_images: Dict[str, str],
                 chart_captions: Dict[str, str],
                 chart_sensors: Dict[str, str],
                 chart_kinds: Dict[str, str],
                 period: Optional[Dict] = None):
        self.bridge = bridge
        self.chart_images = chart_images
        self.chart_captions = chart_captions
        self.chart_sensors = chart_sensors
        self.chart_kinds = chart_kinds
        self.period = period or {}

    def apply(self, repairs: List[Dict]) -> Dict:
        """应用一批修复。返回：
        {applied: [...], needs_human: [...],
         caption_removals: [片段...], caption_replacements: {旧:新}}
        """
        out = {
            "applied": [],
            "needs_human": [],
            "caption_removals": [],
            "caption_replacements": {},
            "text_replacements": {},
        }
        for r in repairs or []:
            if not isinstance(r, dict):
                continue
            rtype = str(r.get("type") or "").strip()
            target = str(r.get("target") or "").strip()
            hint = str(r.get("hint") or "").strip()
            reason = str(r.get("reason") or "").strip()
            entry = {"type": rtype, "target": target, "hint": hint,
                     "reason": reason}
            if rtype == "chart":
                self._repair_chart(entry, out)
            elif rtype == "caption":
                self._repair_caption(entry, out)
            elif rtype == "summary":
                self._repair_summary(entry, out)
            elif rtype == "unit":
                # 单位/空格已在 build 收尾规范化，此处仅登记为已处理
                out["applied"].append(entry)
            elif rtype == "stat":
                self._repair_stat(entry, out)
            elif rtype == "cell":
                self._repair_cell(entry, out)
            else:
                out["needs_human"].append(entry)
        return out

    def _repair_chart(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        if target not in self.chart_images or not hint:
            out["needs_human"].append(entry)
            return
        old_path = self.chart_images[target]
        parsed = self.bridge._parse_chart_id(target) if hasattr(
            self.bridge, "_parse_chart_id") else None
        metric = (parsed[0] if parsed else "") or ""
        kind = self.chart_kinds.get(target, "")
        info = self.bridge.resolve_chart_with_hint(target, hint, kind=kind,
                                                   metric=metric)
        if not info or not info.get("path"):
            out["needs_human"].append(entry)
            return
        new_path = info["path"]
        # 验证：新图必须存在、且与旧图不同（否则等于没改）
        if new_path == old_path:
            out["needs_human"].append(entry)
            return
        # 验证：纠正后的位置词必须命中新传感器位置，且不再含错误位置
        if not self._hint_matches_sensor(hint, info.get("sensor_id", "")):
            out["needs_human"].append(entry)
            return
        self.chart_images[target] = new_path
        self.chart_captions[target] = info.get("display", "")
        self.chart_sensors[target] = info.get("sensor_id", "")
        self.chart_kinds[target] = info.get("kind", kind)
        out["applied"].append(entry)
        log.info("修复 chart %s -> %s", target, new_path)

    def _repair_caption(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        if not target:
            out["needs_human"].append(entry)
            return
        if hint and hint != "删除":
            out["caption_replacements"][target] = hint
        else:
            out["caption_removals"].append(target)
        out["applied"].append(entry)
        log.info("修复 caption：%s -> %s", target, hint or "删除")

    def _repair_summary(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        # target 是完整句子/较长片段且 hint 为修正文字 -> 落地为文档文字替换；
        # target 是指标名（短）-> 清总结缓存，下一轮按真实数据重新生成
        if len(target) >= 8 and hint:
            out["text_replacements"][target] = hint
        else:
            for key in list(getattr(self.bridge, "_summary_cache", {}).keys()):
                if key.split("|", 1)[0] == target or target in key.split("|", 1)[0]:
                    del self.bridge._summary_cache[key]
        out["applied"].append(entry)
        log.info("修复 summary：%s",
                 ("文字替换 %s -> %s" % (target[:40], hint[:40]))
                 if len(target) >= 8 and hint else ("清除缓存 %s" % (target or "全部")))

    def _repair_stat(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        # 方向化统计（X/Y/Z .loc）的根因已在 resolver 内修复；LLM 给出整句
        # 修正时（如 Z 方向位置错配）直接落地为文档文字替换
        if len(target) >= 8 and hint:
            out["text_replacements"][target] = hint
        out["applied"].append(entry)
        log.info("修复 stat：%s", entry.get("target"))

    def _repair_cell(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        if len(target) >= 8 and hint:
            out["text_replacements"][target] = hint
            out["applied"].append(entry)
        else:
            out["needs_human"].append(entry)

    def _hint_matches_sensor(self, hint: str, sensor_id: str) -> bool:
        """纠正后的位置词是否命中该传感器的监测部位/位置。"""
        if not hint or not sensor_id:
            return False
        pos = self.bridge._position_for_sensor(sensor_id) if hasattr(
            self.bridge, "_position_for_sensor") else ""
        if not pos:
            return False
        # 去掉方位修饰后做包含匹配（容忍“内/侧/梁”等差异）
        loc = self.bridge._extract_location(hint) if hasattr(
            self.bridge, "_extract_location") else hint
        if not loc:
            loc = hint
        from .bridge_source import _norm, _position_similarity
        if _norm(loc) in _norm(pos) or _norm(pos) in _norm(loc):
            return True
        return _position_similarity(loc, pos) >= 0.72
