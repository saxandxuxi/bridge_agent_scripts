# -*- coding: utf-8 -*-
"""报告审查：用 LLM 对“生成的模板”和“生成的报告”做两轮审查。

第一轮 · 模板审查（analyze_report.py 生成模板后调用）：
  输入 = 成品报告原文 + 模板全文（占位符版）。
  重点：占位符是否准确、上下文有方位(左幅/右幅/上游/下游)时占位符是否带上、
  静态数字是否被误判成占位符、是否篡改了原文内容。

第二轮 · 报告审查（agent.py 生成报告后调用）：
  输入 = 成品报告原文 + 生成报告全文 + 数据链路摘要 + 填表校验告警。
  重点：表格重复列/行、数据索引是否正确、图片是否插错、总结段落统计值是否
  符合逻辑(湿度百分比不超 100、最大值最大、平均值在 [最小值, 最大值] 闭区间)、
  总结段落是否照抄成品报告里的旧值、单位/报告期等。

LLM 不可用或调用失败时自动跳过（降级），不影响报告生成主流程。
"""

import json
import logging
import re
from typing import Dict, List, Optional

from .llm_classifier import LLMClassifier

log = logging.getLogger("report-agent.reviewer")


def _read_docx_text(path: str, limit: int = 50000) -> str:
    """读取 docx 的段落与表格文本，返回纯文本（用于喂给 LLM）。"""
    if not path or not __import__("os").path.isfile(path):
        return ""
    try:
        from docx import Document
        doc = Document(path)
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for tb in doc.tables:
            for row in tb.rows:
                cells = [c.text.strip() for c in row.cells]
                parts.append(" | ".join(cells))
        text = "\n".join(parts).strip()
        return text[:limit]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 docx 文本失败 %s: %s", path, exc)
        return ""


class ReportReviewer:
    """报告审查器（复用 llm_classifier 的 LLM 配置与接口）。"""

    def __init__(self, llm_cfg: Optional[Dict] = None):
        self._llm = LLMClassifier(llm_cfg or {})

    def available(self) -> bool:
        return self._llm.available()

    def _ask_json(self, system: str, user: str) -> Optional[Dict]:
        try:
            resp = self._llm._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
        except Exception as exc:  # noqa: BLE001
            log.warning("审查 LLM 调用失败: %s", exc)
            return None
        m = re.search(r"\{.*\}", str(resp or ""), re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception as exc:  # noqa: BLE001
                log.warning("解析审查 LLM 回复失败: %s", exc)
        return None

    # ------------------------------------------------------------------
    # 第一轮：模板审查
    # ------------------------------------------------------------------
    def review_template(self, source_text: str, template_text: str) -> Dict:
        """审查生成的模板，返回 {"ok", "issues", "raw"}。"""
        result = {"ok": True, "issues": [], "raw": ""}
        if not self.available():
            log.info("LLM 不可用，跳过模板审查")
            return result
        if not template_text.strip():
            log.info("模板文本为空，跳过模板审查")
            return result
        system = (
            "你是桥梁健康监测报告模板审查专家。请审查“占位符版模板”是否准确，"
            "只输出 JSON：{\"issues\": [{\"type\": \"...\", \"detail\": \"...\"}]}，"
            "没有问题输出 {\"issues\": []}。\n"
            "重点审查：\n"
            "1) 占位符是否准确：{{stats.指标.统计}} 的指标/统计是否对应正文含义；"
            "{{cell.指标.位置.统计}} 的“位置”是否和表格行一致；{{chart.xxx}} 是否"
            "带上了监测位置。\n"
            "2) 方位词：若节标题/说明句带 左幅/右幅/上游/下游/左侧/右侧，而该节"
            "图表或单元格占位符的位置没有带对应方位，属于 missing_direction。\n"
            "3) 静态值误判：章节号、列表序号、桩号、测点编号、阈值、规范编号、"
            "设计参数(桥长/跨径/矢跨比)、布设数量、检查记录等静态数字被替换成"
            "占位符，属于 static_as_placeholder。\n"
            "4) 篡改原文：模板把原文中不该改的固定文字、单位、标点、标题改动了，"
            "属于 text_tampered。\n"
            "5) 单位/标点空格：占位符和它后面的单位、标点之间不能有多余空格"
            "（如 {{...}}   m/s² 有两个空格要报 unit_wrong）。\n"
            "6) 多方向位置不要串：GNSS/支座位移等 X/Y/Z 三方向的“对应测点/位置”"
            "占位符必须各自独立，不能把 Z 的位置写成 Y 的位置。\n"
            "type 取值：placeholder_wrong / missing_direction / "
            "static_as_placeholder / text_tampered / unit_wrong / other。"
            "detail 用简短中文说明问题所在（可引用原文字段），不要泛泛而谈。"
        )
        user = (
            "【成品报告原文】\n" + (source_text or "（无）")[:40000]
            + "\n\n【占位符版模板】\n" + template_text[:40000]
        )
        data = self._ask_json(system, user)
        result["raw"] = json.dumps(data, ensure_ascii=False) if data else ""
        if isinstance(data, dict):
            issues = data.get("issues") or []
            result["issues"] = [i for i in issues if isinstance(i, dict)]
            result["ok"] = not result["issues"]
        else:
            result["ok"] = True  # 解析失败视为无结论，不阻塞
        return result


_UNIT_RE = re.compile(r"(m/s²|m/s2|℃|%|με|mm|kN|mm/s²)")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_num(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def self_check_report(path: str) -> List[Dict]:
    """确定性体检（不依赖 LLM），返回问题清单 [{type, detail}]。

    检查：残留占位符、表格重复行、最大<最小、平均值超出 [最小值, 最大值]、
    差值/极差为负、湿度百分数超出 0~100、数值与单位之间多余空格。
    """
    issues: List[Dict] = []
    if not path or not __import__("os").path.isfile(path):
        return issues
    try:
        from docx import Document
        doc = Document(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("self_check 读取报告失败: %s", exc)
        return issues

    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    # 1) 残留占位符
    for t in texts:
        if "{{" in t:
            issues.append({"type": "leftover_placeholder", "detail": t[:80]})
    # 2) 表格：重复行 + 数值逻辑
    for ti, tb in enumerate(doc.tables):
        if not tb.rows:
            continue
        seen_rows = set()
        header = [c.text.strip() for c in tb.rows[0].cells]
        col = {h: i for i, h in enumerate(header)}

        def celln(row, *names):
            for h, i in col.items():
                if any(n in h for n in names):
                    return _to_num(row.cells[i].text)
            return None

        for ri, row in enumerate(tb.rows[1:], 1):
            cells = [c.text.strip() for c in row.cells]
            key = "|".join(cells)
            if key and key in seen_rows:
                issues.append({"type": "table_duplicate",
                               "detail": f"表{ti + 1} 行{ri} 重复"})
            seen_rows.add(key)
            mx = celln(row, "最大", "最高")
            mn = celln(row, "最小", "最低")
            av = celln(row, "平均")
            df = celln(row, "差值", "极差")
            if mx is not None and mn is not None and mx < mn:
                issues.append({"type": "stat_logic",
                               "detail": f"表{ti + 1} 行{ri} 最大{mx} < 最小{mn}"})
            if av is not None and mx is not None and mn is not None \
                    and not (mn - 1e-6 <= av <= mx + 1e-6):
                issues.append({"type": "stat_logic",
                               "detail": f"表{ti + 1} 行{ri} 平均{av} 不在 [{mn},{mx}]"})
            if df is not None and df < -1e-9:
                issues.append({"type": "stat_logic",
                               "detail": f"表{ti + 1} 行{ri} 差值/极差为负 {df}"})
    # 3) 湿度/百分比越界 + 单位空格
    for t in texts:
        for m in re.finditer(r"([-+]?\d+(?:\.\d+)?)\s*(%|％)", t):
            v = _to_num(m.group(1))
            if v is not None and (v < 0 or v > 100):
                issues.append({"type": "stat_logic",
                               "detail": f"百分比越界 {m.group(0)} | {t[:50]}"})
        if re.search(r"\d\s{2,}(m/s²|m/s2|℃|%|με|mm|kN)", t):
            issues.append({"type": "unit_wrong",
                           "detail": f"数值与单位之间有多余空格 | {t[:50]}"})
    return issues

    # ------------------------------------------------------------------
    # 第二轮：报告审查
    # ------------------------------------------------------------------
    def review_report(self, source_text: str, report_text: str,
                      lineage_digest: str = "",
                      table_warnings: str = "") -> Dict:
        """审查生成的报告，返回 {"ok", "issues", "raw"}。"""
        result = {"ok": True, "issues": [], "raw": ""}
        if not self.available():
            log.info("LLM 不可用，跳过报告审查")
            return result
        if not report_text.strip():
            log.info("报告文本为空，跳过报告审查")
            return result
        system = (
            "你是桥梁健康监测报告质量审查专家。对照成品报告原文，审查“生成的报告”"
            "是否存在错误，只输出 JSON：{\"issues\": [{\"type\": \"...\", "
            "\"detail\": \"...\"}]}，没有问题输出 {\"issues\": []}。\n"
            "重点审查：\n"
            "1) 表格：是否有重复的列或重复的行；单元格数值是否张冠李戴（如右幅行填了"
            "左幅数据）。type=table_duplicate 或 index_wrong。\n"
            "2) 图片：图注/章节是否左右幅、上下游、位置错配；同一张图是否被重复插入。"
            "type=image_wrong。\n"
            "3) 统计逻辑：湿度等百分数不应超过 100（如 112% 一定错）；温度/应变等"
            "不应出现明显反物理的值；表格里“最大值”必须不小于“最小值”，“平均值”必须"
            "落在 [最小值, 最大值] 闭区间内；“差值/极差”应为非负。type=stat_logic。\n"
            "4) 单位：数值单位是否写错（如 m/s² 落在句号外、%与℃混用）。type=unit_wrong。\n"
            "5) 单位空格：数值和单位之间不能有多余空格（如“5.5  m/s²”“6.9m/s² ”）。"
            "type=unit_wrong。\n"
            "6) 总结段落：结论里的统计值是否和报告正文/表格不一致；是否照抄成品报告"
            "里的旧数值(应从数据重算，不能直接回填原文)；是否在同一句里把“最高/最低”"
            "极值重复输出两遍且数值口径不一致(如先写最高43.3℃、后面又写最高43.2758℃)。"
            "type=summary_stale。\n"
            "7) 极值来源：若某个极值来自恒 0/故障测点(如结构温度最低 0℃)，正文必须"
            "明确标注是传感器故障，且“对应测点位置”要和“故障位置”前后一致。"
            "type=summary_stale。\n"
            "8) 多方向位置：GNSS 的 X/Y/Z 三方向“对应测点”必须各自正确，"
            "不能出现 Z 方向复用 Y 方向位置。type=index_wrong。\n"
            "9) 报告期：标题/页眉/正文的季度或年份是否和声明报告期一致。"
            "type=period_mismatch。\n"
            "type 取值：table_duplicate / index_wrong / image_wrong / stat_logic / "
            "unit_wrong / summary_stale / period_mismatch / other。"
            "detail 用简短中文说明具体位置和问题，能引用数值就引用。"
        )
        user = (
            "【成品报告原文】\n" + (source_text or "（无）")[:30000]
            + "\n\n【生成的报告】\n" + report_text[:40000]
            + ("\n\n【数据链路摘要（未找到/回退项）】\n" + lineage_digest
               if lineage_digest else "")
            + ("\n\n【规则式填表校验告警】\n" + table_warnings
               if table_warnings else "")
        )
        data = self._ask_json(system, user)
        result["raw"] = json.dumps(data, ensure_ascii=False) if data else ""
        if isinstance(data, dict):
            issues = data.get("issues") or []
            result["issues"] = [i for i in issues if isinstance(i, dict)]
            result["ok"] = not result["issues"]
        else:
            result["ok"] = True
        return result
