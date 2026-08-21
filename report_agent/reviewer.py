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

    # ------------------------------------------------------------------
    # 第二轮：报告审查
    # ------------------------------------------------------------------
    def review_report(self, source_text: str, report_text: str,
                      lineage_digest: str = "",
                      table_warnings: str = "",
                      chart_index: str = "",
                      prior_issues: str = "") -> Dict:
        """审查生成的报告，返回 {"ok", "issues", "repairs", "raw"}。

        repairs 是机器可执行的修复清单（供 repairer 逐条“验证后落地”），
        issues 是给人看的问题清单，二者可同时存在。
        """
        result = {"ok": True, "issues": [], "repairs": [], "raw": ""}
        if not self.available():
            log.info("LLM 不可用，跳过报告审查")
            return result
        if not report_text.strip():
            log.info("报告文本为空，跳过报告审查")
            return result
        system = (
            "你是桥梁健康监测报告质量审查专家。对照成品报告原文与真实监测数据"
            "（数据链路摘要），审查“生成的报告”是否存在错误。\n"
            "只输出 JSON：{\"issues\": [{\"type\": \"...\", \"detail\": \"...\", "
            "\"needs_human\": true/false}], \"repairs\": [{\"type\": \"...\", "
            "\"target\": \"...\", \"hint\": \"...\", \"reason\": \"...\"}]}；"
            "没有问题输出 {\"issues\": [], \"repairs\": []}。\n"
            "重点审查（issues.type 取值：table_duplicate / index_wrong / "
            "image_wrong / stat_logic / unit_wrong / summary_stale / "
            "period_mismatch / other）：\n"
            "1) 表格：是否有重复的列或重复的行；单元格数值是否张冠李戴（如右幅行填了"
            "左幅数据）。\n"
            "2) 图片：图本身是否左右幅/上下游/位置错配，或同一张图被重复插入。"
            "若图片是对的、只是图片下方或正文里的“配图说明文字”写错（如标题是3#墩、"
            "配图说明却写2#墩），不要报 image_wrong，而是输出 caption 修复："
            "type=caption、target=错的图注原文片段（用于定位删除）、hint=正确文字或"
            "“删除”。\n"
            "3) 方位语义：桥跨地名方位（炎陵侧/汝城侧/随州侧/湘潭侧/吉首侧等）可与"
            "截面方位（上游/下游/左幅/右幅/顶板/底板）叠加共存，语义一致即可，不要"
            "因为“上游+炎陵侧”同时出现就报冲突；只有当真正的截面方位（左幅↔右幅、"
            "上游↔下游、顶板↔底板）与表格/数据矛盾时才报 index_wrong/image_wrong。\n"
            "4) 统计逻辑：湿度等百分数不应超过 100（如 112% 一定错）；温度/应变等"
            "不应出现明显反物理的值；表格里“最大值”必须不小于“最小值”，“平均值”必须"
            "落在 [最小值, 最大值] 闭区间内；“差值/极差”应为非负。\n"
            "5) 单位：数值单位是否写错（如 m/s² 落在句号外、%与℃混用）；数值和单位"
            "之间不能有多余空格（如“5.5  m/s²”“6.9m/s² ”）。type=unit_wrong。\n"
            "6) 总结段落：结论里的统计值是否和报告正文/表格不一致；是否照抄成品报告"
            "里的旧数值(应从数据重算，不能直接回填原文)；是否在同一句里把“最高/最低”"
            "极值重复输出两遍且数值口径不一致。注意：四舍五入保留1位小数属正常精度"
            "处理（如 43.2758℃ 写成 43.3℃ 是合理的），不要报；只有两位小数下四舍五入"
            "也不一致（偏差≥0.5）、或数值在正文/表格/数据链路中完全找不到来源时才报。"
            "type=summary_stale。\n"
            "7) 极值来源：若某个极值来自恒 0/故障测点(如结构温度最低 0℃)，正文必须"
            "明确标注是传感器故障，且“对应测点位置”要和“故障位置”前后一致。"
            "type=summary_stale。\n"
            "8) 多方向位置：GNSS 的 X/Y/Z 三方向“对应测点”必须各自正确，"
            "不能出现 Z 方向复用 Y 方向位置。type=index_wrong。\n"
            "9) 报告期：标题/页眉/正文的季度或年份是否和声明报告期一致。"
            "type=period_mismatch。\n"
            "10) 结论与真实数据一致性（双向）：原文/结论说某位置“正常”，但数据链路"
            "摘要显示该位置有恒0故障、缺失天数>0或缺失小时数达到阈值，必须报 "
            "summary_stale 并给 summary 修复；反之原文说某位置“异常/故障”，但数据"
            "显示正常，也必须按真实数据纠正。\n"
            "11) 数据阅读纪律（避免误报/误改）：\n"
            "  a. 布点/限值表（表头如 一级/二级/三级 限值、布点表、设计值）不是监测"
            "统计表，禁止用其中的数字计算差值/极值或作为替代值；统计表表头含"
            "平均/最大/最小/差值。\n"
            "  b. 边坡/大地坐标类 GNSS（位置名含“边坡”，如 炎陵侧中跨1/4截面对应"
            "边坡）是大地坐标绝对值，配置已将其从位移指标中排除，禁止把它当全局"
            "位移极值或替代值；位移极值只取非边坡统计表。\n"
            "  c. 数据链路摘要里“未找到”的 cell 通常是恒值故障传感器（整季恒值/恒0），"
            "不代表该测点整组数据缺失；不得据此断言某个极值“无数据支撑”。\n"
            "  d. 权威口径：结论中的统计值以季度/年度统计文件（季度统计.json）的"
            "全桥统计与 stats.* 占位符解析结果为准，这是数据侧唯一权威；报告表格"
            "个别行与全桥统计不一致、或限值表数值不同，都不代表结论值虚构；只有"
            "季度统计文件缺失该特征时才可判“无数据支撑”。\n"
            "  e. 若你怀疑 stats 解析值与表格矛盾，把 needs_human 置为 true 提示"
            "人工核对即可，不要自行给出替代数值/位置；拿不准正确值时"
            "把 issues.needs_human 置为 true，不要编造替代数值或替代位置。\n"
            "12) 报告结构权威：图片/测点位置是否合理，一律以报告自身的小标题、"
            "表格标题、表头、表格内填写的监测部位为准；正文里的布点描述（如“仅在"
            "××布设振动测点”）不得用于否定图表/表格里的位置，除非报告内部自相矛盾"
            "（同一图表前后标注不一致）。\n"
            "13) 审查纪律（宁缺毋滥）：图注与配图说明的语义措辞差异（如“振动”vs"
            "“地震荷载”这类用词不同）可忽略，不报；但配图说明与图/表方位的客观矛盾"
            "（说明写左幅、图/表是右幅，或上/下游颠倒）要报 image_wrong/caption。"
            "如无明确、可证实的问题就输出空 issues；不确定的不要报，避免过度审查。\n"
            "issues.needs_human：仅当“缺少该类型图/统计值/数据/特征，或图库图本身"
            "不合要求、无法通过重新索引/重算纠正”时为 true，其余为 false。\n"
            "repairs.type 取值：chart（重索引图片）、caption（删除/改正错误图注）、"
            "stat（重算统计值）、cell（重算单元格）、summary（重生成总结润色）、"
            "unit（单位/空格规范化）。\n"
            "repairs.target：chart 用图表索引表里的 chart_id；caption 用错的图注原文"
            "片段；stat/cell 用占位符或“对应测点/位置”所在指标；summary 用指标名。\n"
            "repairs.hint：正确的监测位置/方向/特征，或 caption 的正确文字（删除则"
            "写“删除”）。能确定才输出 repair，不确定不要瞎编。\n"
            "detail/reason 用简短中文说明具体位置和问题，能引用数值就引用。"
        )
        # 长报告拆段逐段审查（降低单次上下文过长导致的幻觉），每段独立调用，
        # 问题合并去重；成品原文/血缘/图表索引等全局信息每段都带上。
        base_parts = []
        if lineage_digest:
            base_parts.append("【数据链路摘要（未找到/回退项）】\n" + lineage_digest)
        if table_warnings:
            base_parts.append("【规则式填表校验告警】\n" + table_warnings)
        if chart_index:
            base_parts.append("【图表索引表（chart_id→图注→位置，供 chart/caption 修复定位）】\n"
                              + chart_index)
        if prior_issues:
            base_parts.append("【上一轮已发现问题（本轮判断是否已解决、解决是否正确）】\n"
                              + prior_issues)
        source_block = "【成品报告原文】\n" + (source_text or "（无）")[:30000]
        segments = _split_segments(report_text)
        all_issues, all_repairs, raws = [], [], []
        for _i, seg in enumerate(segments):
            parts = [source_block,
                     f"【生成的报告（第{_i + 1}/{len(segments)}段）】\n{seg}"]
            parts += base_parts
            data = self._ask_json(system, "\n\n".join(parts))
            if isinstance(data, dict):
                all_issues += [x for x in (data.get("issues") or [])
                               if isinstance(x, dict)]
                all_repairs += [x for x in (data.get("repairs") or [])
                                if isinstance(x, dict)]
                raws.append(json.dumps(data, ensure_ascii=False))
        # 合并去重
        seen_i, seen_r = set(), set()
        for x in all_issues:
            key = (str(x.get("type")), str(x.get("detail"))[:60])
            if key not in seen_i:
                seen_i.add(key)
                result["issues"].append(x)
        for x in all_repairs:
            key = (str(x.get("type")), str(x.get("target"))[:50],
                   str(x.get("hint"))[:40])
            if key not in seen_r:
                seen_r.add(key)
                result["repairs"].append(x)
        result["raw"] = "\n".join(raws)[:12000] if raws else ""
        result["ok"] = not result["issues"]
        return result


def _split_segments(text: str, max_len: int = 6000) -> List[str]:
    """把报告文本按段落拆成 ≤max_len 的分段（尽量保持段落完整）。"""
    paras = [p for p in str(text or "").split("\n") if p.strip()]
    if not paras:
        return [str(text or "")]
    segs, cur, cur_len = [], [], 0
    for p in paras:
        if cur and cur_len + len(p) > max_len:
            segs.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p) + 1
    if cur:
        segs.append("\n".join(cur))
    return segs


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
