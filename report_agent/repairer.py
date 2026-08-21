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

# 从修正文字推断 指标/统计量/方向（用于数值验证）
_METRIC_HINTS = [
    ("structure_temperature", ("结构温度",)),
    ("strain", ("应变",)),
    ("displacement", ("位移", "空间变位")),
    ("deflection", ("挠度",)),
    ("vibration", ("振动",)),
    ("wind_speed", ("风速",)),
    ("temperature", ("环境温度", "温度")),
    ("humidity", ("湿度",)),
    ("cable_force", ("索力",)),
    ("rotation", ("倾角",)),
]
_STAT_HINTS = [
    ("max", ("最高", "最大", "最大值")),
    ("min", ("最低", "最小", "最小值")),
    ("range", ("差值", "极差")),
]
_AXIS_HINTS = [
    ("x", ("X向", "X方向")),
    ("y", ("Y向", "Y方向")),
    ("z", ("Z向", "Z方向")),
]


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
            self._apply_text_repair(entry, out)
        else:
            for key in list(getattr(self.bridge, "_summary_cache", {}).keys()):
                if key.split("|", 1)[0] == target or target in key.split("|", 1)[0]:
                    del self.bridge._summary_cache[key]
            out["applied"].append(entry)
            log.info("修复 summary：清除缓存 %s", target or "全部")

    def _repair_stat(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        # 方向化统计（X/Y/Z .loc）的根因已在 resolver 内修复；LLM 给出整句
        # 修正时（如 Z 方向位置错配）直接落地为文档文字替换
        if len(target) >= 8 and hint:
            self._apply_text_repair(entry, out)
        else:
            out["applied"].append(entry)
            log.info("修复 stat：交由 resolver 重解析 %s", entry.get("target"))

    def _repair_cell(self, entry: Dict, out: Dict) -> None:
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        if len(target) >= 8 and hint:
            self._apply_text_repair(entry, out)
        else:
            out["needs_human"].append(entry)

    def _apply_text_repair(self, entry: Dict, out: Dict) -> None:
        """文字替换修复：先做数值验证，与确定性重算一致才落地，否则交人工。"""
        target = entry.get("target") or ""
        hint = entry.get("hint") or ""
        ok, reason = self._verify_text_hint(hint)
        if ok:
            out["text_replacements"][target] = hint
            out["applied"].append(entry)
            log.info("修复文字（已验证）：%s -> %s", target[:40], hint[:40])
        else:
            entry["reason"] = (str(entry.get("reason") or "") +
                               f"；未通过数值验证：{reason}")
            out["needs_human"].append(entry)
            log.info("文字修复未通过验证，交人工：%s（%s）", target[:40], reason)

    def _verify_text_hint(self, hint: str):
        """校验修正文字里的数值是否与确定性重算一致。

        防止 LLM 误改：如把 43.3℃（真实）改成 40.2℃、把 7.94mm 改成
        537.99mm（边坡大地坐标，配置已排除）。返回 (是否通过, 原因)。
        """
        if not hint:
            return False, "hint 为空"
        metric, stat, axis = self._infer_metric_stat(hint)
        if not metric or not stat:
            return False, f"无法从修正文字推断指标/统计量: {hint[:40]}"
        mkey = f"{metric}_{axis}" if axis else metric
        try:
            v, detail = self.bridge.resolve_metric_stat_detail(
                mkey, stat, self.period)
        except Exception as exc:  # noqa: BLE001
            return False, f"重算失败: {exc}"
        if v is None:
            return False, f"{mkey}.{stat} 无解析值"
        nums = self._numbers(hint)
        if not nums:
            return False, "修正文字中没有数值可验证"
        ref = float(v)
        if any(abs(n - ref) <= max(abs(ref) * 0.005, 0.05) for n in nums):
            return True, ""
        return False, f"修正数值 {nums} 与重算值 {ref:g} 不一致"

    @staticmethod
    def _infer_metric_stat(text: str):
        metric = stat = axis = ""
        for m, kws in _METRIC_HINTS:
            if any(k in text for k in kws):
                metric = m
                break
        for s, kws in _STAT_HINTS:
            if any(k in text for k in kws):
                stat = s
                break
        for a, kws in _AXIS_HINTS:
            if any(k in text for k in kws):
                axis = a
                break
        return metric, stat, axis

    @staticmethod
    def _numbers(text: str) -> List[float]:
        out = []
        for m in re.finditer(r"-?\d+(?:\.\d+)?", text or ""):
            try:
                out.append(float(m.group(0)))
            except ValueError:
                continue
        return out

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
