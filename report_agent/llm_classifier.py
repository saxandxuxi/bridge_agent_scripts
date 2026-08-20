# -*- coding: utf-8 -*-
"""LLM 辅助分类器：基于 Qwen（通义千问/DashScope）对关键词打分结果做第二轮筛选。

两轮筛选流程（与 recognizer.py 配合）：
  第一轮 — 关键词打分（recognizer.py）：
            基于上下文关键词、图题、文本长度给出 replace / keep / review 初步结论
  第二轮 — LLM 校验（本模块）：
            1. 完整性校验：观察【报告文本】中还有哪些动态值未被第一轮提取
            2. 正确性校验：第一轮提取项中是否有静态值被误判为动态
            3. 对待确认项（review）给出最终 replace / keep 判定
            4. 若全部提取正确 → 模型只回复一个字：是

设计要点：
- 默认接入阿里云百炼（DashScope）OpenAI 兼容接口，模型 qwen-plus
  - API Key 从环境变量获取：优先 QWEN_API_KEY，其次 DASHSCOPE_API_KEY
  - 占位符统一标准：{{stats.<指标英文名>.<统计类型>}}，模型输出会再被本模块规范化校验
  - 报告全文 + 已提取项 + 待确认项一次性发送，单次调用完成校验
  - 网络 / API 失败时自动降级：review 项改用文本长度启发式兜底
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional

log = logging.getLogger("report-agent.llm_classifier")

# --- Qwen / DashScope 默认配置 ---
DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
# API Key 环境变量（按优先级取第一个已设置的）
ENV_KEY_VARS = ("QWEN_API_KEY", "DASHSCOPE_API_KEY")

# 文本长度启发式阈值
SHORT_CONTEXT_THRESHOLD = 5   # <5 字 → 极短上下文
LONG_CONTEXT_THRESHOLD = 50    # >50 字 → 长描述性上下文

# 占位符统一标准：指标英文名 → 中文含义（模型输出会被校验）
METRIC_STANDARD = {
    "temperature": "温度", "humidity": "湿度", "deflection": "挠度",
    "displacement": "位移/变位", "rotation": "转角", "strain": "应变",
    "stress": "应力", "cable_force": "索力", "cable_clamp": "索夹滑动",
    "vehicle_load": "车辆荷载", "wind_load": "风荷载", "wind_speed": "风速",
    "earthquake_load": "地震", "structural_temp": "结构温度",
    "structure_temperature": "结构温度",
    "bearing_displacement": "支座位移",
    "vehicle_count": "车辆数量", "settlement": "沉降",
}
STAT_STANDARD = {
    "max": "最大/最高", "min": "最小/最低", "avg": "平均",
    "median": "中位数", "std": "标准差", "range": "极差/差值",
    "abs_max": "绝对最大",
}

STD_PLACEHOLDER_RE = re.compile(r"\{\{stats\.([a-zA-Z_]+)\.([a-zA-Z_]+)\}\}")

# 非规范指标键 -> 运行时 config.metrics 使用的键（如 LLM 输出 structural_temp）
METRIC_CANON = {
    "structural_temp": "structure_temperature",
}


def get_api_key() -> str:
    """从环境变量获取 API Key（优先 QWEN_API_KEY，其次 DASHSCOPE_API_KEY）。"""
    for var in ENV_KEY_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def normalize_placeholder(raw: str, fallback_index: int) -> str:
    """校验并规范化 LLM 输出的占位符，不符合统一标准则退回 {{data.N}}。

    规则：
      {{stats.<metric>.<stat>}} 且 metric ∈ METRIC_STANDARD、stat ∈ STAT_STANDARD
    """
    if not raw:
        return f"{{{{data.{fallback_index}}}}}"
    m = STD_PLACEHOLDER_RE.search(raw)
    if m and m.group(2) in STAT_STANDARD:
        metric = METRIC_CANON.get(m.group(1), m.group(1))
        if metric in METRIC_STANDARD:
            return f"{{{{stats.{metric}.{m.group(2)}}}}}"
    return f"{{{{data.{fallback_index}}}}}"


def _text_length_heuristic(item: Dict) -> Dict:
    """基于上下文文本长度的启发式降级判断（LLM 不可用时兜底）。

    - 极短上下文（<5 字）：倾向 keep（标签/序号）
    - 长描述性文本（>50 字）：倾向 replace（数据叙述）
    - 中等长度：维持 review
    """
    context = item.get("context") or item.get("snippet") or item.get("caption") or ""
    text_len = len(context.strip())
    result = {
        "verdict": "review",
        "confidence": item.get("confidence", 0.5),
        "reason": f"文本长度降级（上下文 {text_len} 字）",
    }
    if text_len < SHORT_CONTEXT_THRESHOLD:
        result["verdict"] = "keep"
        result["confidence"] = 0.55
        result["reason"] = f"极短上下文（{text_len} 字），疑似表格标签/序号"
    elif text_len > LONG_CONTEXT_THRESHOLD:
        result["verdict"] = "replace"
        result["confidence"] = 0.60
        result["reason"] = f"长描述性上下文（{text_len} 字），疑似数据叙述"
    else:
        result["reason"] = f"中等上下文长度（{text_len} 字），维持待确认"
    return result


class LLMClassifier:
    """基于 Qwen 的 LLM 分类器：两轮筛选中的第二轮。"""

    def __init__(self, llm_cfg: Optional[Dict] = None):
        llm_cfg = llm_cfg or {}
        self.enabled = bool(llm_cfg.get("enabled", False))
        self.api_base = (llm_cfg.get("api_base") or "").strip() or DEFAULT_API_BASE
        # API Key 优先级：config.api_key > 环境变量
        self.api_key = (llm_cfg.get("api_key") or "").strip() or get_api_key()
        self.model = (llm_cfg.get("model") or "").strip() or DEFAULT_MODEL
        self.review_low = float(llm_cfg.get("review_low", 0.38))
        self.review_high = float(llm_cfg.get("review_high", 0.68))
        self.timeout = int(llm_cfg.get("timeout", 300))
        self.batch_size = int(llm_cfg.get("batch_size", 20))

    def available(self) -> bool:
        """LLM 是否可用：启用 + 有 API Key + 有 API 地址。"""
        return self.enabled and bool(self.api_key) and bool(self.api_base)

    def summarize_feature(self, digest: str, max_chars: int = 100) -> str:
        """基于季度/年度统计摘要生成一段结论性描述（≤max_chars 字）。
        重点：数据缺失情况、极值对应位置等特殊位置；不逐个位置罗列。
        LLM 不可用或调用失败时返回空串（由调用方走规则化兜底）。"""
        if not self.available():
            return ""
        system = (
            "你是桥梁结构健康监测数据分析专家。"
            "根据给定的季度/年度统计摘要，用不超过%d字的文字写一句结论性描述。"
            "要求：只突出数据缺失情况、恒 0/疑似故障测点位置等需要关注的点；"
            "不要逐个位置罗列；不要编造统计摘要里没有的数据；"
            "不要重复输出“最高/最低/最大值/最小值”等已经会在正文出现的数值，"
            "那些极值正文自会给出，你只补充缺失、故障位置等额外信息；"
            "如果除了这些极值没有其它异常，就简短说明数据整体是否正常即可；"
            "不要出现传感器编号或任何数字编号，位置一律用监测部位名称；"
            "不要输出“根据统计摘要”之类的转述前缀；只输出结论文本本身。"
        ) % max_chars
        try:
            resp = self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": digest},
            ])
        except Exception as exc:  # noqa: BLE001
            log.warning("总结生成失败: %s", exc)
            return ""
        text = str(resp or "").strip().strip("\"'“”").strip()
        return text[:max_chars]

    # ------------------------------------------------------------------
    # API 调用
    # ------------------------------------------------------------------

    def _chat(self, messages: List[Dict]) -> str:
        """调用 OpenAI 兼容接口（DashScope），返回模型回复文本。"""
        import urllib.request

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,   # 分类任务用低温度，保证确定性
        }
        req = urllib.request.Request(
            f"{self.api_base.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        log.info("调用 Qwen API：model=%s，messages=%d 条", self.model, len(messages))
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------
    # 第二轮：完整性校验 + review 判定
    # ------------------------------------------------------------------

    def verify_and_complete(
        self,
        doc_text: str,
        extracted: List[Dict],
        review_items: List[Dict],
        images: List[Dict],
        review_images: List[Dict],
    ) -> Dict:
        """执行第二轮 LLM 校验。

        参数:
            doc_text: 报告全文摘录（段落 + 表格）
            extracted: 第一轮已提取的动态数字项（value/snippet/placeholder）
            review_items: 待确认数字项（含 _review_index 字段）
            images: 全部图片（caption/verdict）
            review_images: 待确认图片（含 _review_index 字段）

        返回:
            {
              "complete": bool,           # 是否全部正确（LLM 回复"是"）
              "missed": [...],            # 漏掉的动态值
              "wrong": [...],             # 被误判的静态值（应改 keep）
              "review_decisions": {index: {"verdict", "placeholder"}},
              "images_review": {index: "replace"|"keep"},
              "images_wrong": {index: "keep"},   # 误判为 replace 的图
              "raw": "模型原始回复",
            }
        """
        # LLM 不可用 → 降级为文本长度启发式
        if not self.available():
            log.info("LLM 不可用（enabled=%s, api_key=%s），review 项降级为文本长度启发式",
                     self.enabled, bool(self.api_key))
            return self._fallback(review_items, review_images)

        system_prompt = self._build_system_prompt()
        user_content = self._build_user_content(
            doc_text, extracted, review_items, images, review_images
        )

        try:
            resp = self._chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ])
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 调用失败（%s），review 项降级为文本长度启发式", exc)
            return self._fallback(review_items, review_images)

        return self._parse_response(resp, review_items, review_images)

    def verify_images(self, doc_text: str, images: List[Dict],
                      review_images: List[Dict]) -> Dict:
        """单独对图片做一轮 LLM 判定（与数字识别分开，避免相互干扰）。

        返回 {"images_review": {index: "replace"|"keep"},
              "images_wrong": {index}, "raw": 原始回复}。
        """
        if not self.available():
            log.info("LLM 不可用，图片 review 降级为文本长度启发式")
            images_review = {}
            for img in (review_images or []):
                idx = img.get("_review_index", len(images_review))
                r = _text_length_heuristic(img)
                images_review[idx] = r["verdict"]
            return {"images_review": images_review, "images_wrong": {}, "raw": ""}
        system = (
            "你是桥梁健康监测报告图表识别专家。请判断报告中的每张图片属于哪一类：\n"
            "1) 动态监测图（数据曲线图/直方图/分布图等，生成新报告时应替换为程序生成的图）；\n"
            "2) 固定图（示意图/CAD图/布置图/架构图/流程图/logo/照片等，保留）。\n"
            "只对“待确认”的图片给出 replace(动态图) 或 keep(固定图)。\n"
            "同时检查“已判定为动态图”的图片中是否有误判的固定图（图题含示意图/布置图/"
            "架构图/流程图/平面图等），把它们的索引放进 images_wrong。\n"
            "只回复 JSON，格式：{\"images_review\": {\"<index>\": \"replace|keep\"}, "
            "\"images_wrong\": [<index>]}"
        )
        user = (
            "报告文本摘录（供上下文参考）：\n" + (doc_text or "")[:10000]
            + "\n\n图片清单：\n"
            + json.dumps(
                [{k: im.get(k) for k in ("index", "caption", "verdict", "w_in", "h_in")}
                 for im in images],
                ensure_ascii=False,
            )[:10000]
            + "\n\n待确认图片索引："
            + json.dumps([im.get("_review_index") for im in (review_images or [])])
        )
        try:
            resp = self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 图片识别失败（%s）", exc)
            return {"images_review": {}, "images_wrong": {}, "raw": ""}
        result = {"images_review": {}, "images_wrong": {}, "raw": resp}
        m = re.search(r"\{.*\}", resp, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                result["images_review"] = data.get("images_review") or {}
                result["images_wrong"] = {int(x) for x in (data.get("images_wrong") or [])}
            except Exception as exc:  # noqa: BLE001
                log.warning("解析图片 LLM 回复失败: %s", exc)
        return result

    def _fallback(self, review_items: List[Dict], review_images: List[Dict] = None) -> Dict:
        """降级：review 项用文本长度启发式。"""
        decisions = {}
        for item in review_items:
            # 只读 _review_index，不 pop（pop 由调用方 recognize() 统一处理）
            idx = item.get("_review_index", len(decisions))
            r = _text_length_heuristic(item)
            decisions[idx] = {
                "verdict": r["verdict"],
                "confidence": r["confidence"],
                "reason": r["reason"],
                "placeholder": None,
            }
        images_review = {}
        for img in (review_images or []):
            idx = img.get("_review_index", len(images_review))
            r = _text_length_heuristic(img)
            images_review[idx] = r["verdict"]
        return {
            "complete": False,
            "missed": [],
            "wrong": [],
            "review_decisions": decisions,
            "images_review": images_review,
            "images_wrong": {},
            "text_replacements": [],
            "raw": "",
        }

    # ------------------------------------------------------------------
    # 提示词构建
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        metric_desc = "、".join(f"{v}({k})" for k, v in METRIC_STANDARD.items())
        stat_desc = "、".join(f"{v}({k})" for k, v in STAT_STANDARD.items())
        return (
            "你是一个桥梁健康监测报告识别专家，"
            "负责对一份已经过关键词规则初筛的报告做【完整性+正确性+自查自纠】三重校验。\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第一部分：动态值与静态值定义】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "▶ 动态值（应 replace）：会随监测周期变化的统计结果，每期重新计算。\n"
            "  典型例子：本季度最高温度39.93℃、车辆总数1549777辆、应变最大差值697.85με、\n"
            "  索夹滑动最大差值31.61mm、最大10min平均风速11.93m/s、结构温度最大值40.59℃、\n"
            "  最高湿度100.55%、占比、增长率、超标天数等。\n\n"
            "▶ 原文占位标记（应 replace）：部分成品报告用 [A-MAX]、[A-MAX-LOC]、B-MIN、\n"
            "  G-MAX-X 这类字母-统计量标记代替真实数值（如“环境最高温度为[A-MAX]℃，\n"
            "  对应测点位置为[A-MAX-LOC]”）。它们每期变化，视为动态值；\n"
            "  - 值标记（A-MAX）→ {{stats.<指标>.<统计>}}\n"
            "  - 位置标记（A-MAX-LOC）→ {{stats.<指标>.<统计>.loc}}\n"
            "  不要把它们当作静态编号，也不要重复加入 missed（本地已按上下文映射）。\n\n"
            "▶ 静态值（应 keep）：每期不变的固定配置、规范参数、表格标识等。\n"
            "  【A. 工程设计参数】桥长1933.6m、跨径1480+453.6m、桩号K814+091、矢跨比1/9.6\n"
            "  【B. 监测内容/布设表】'共布置11个风荷载测点'、'布设1个温湿度仪'、\n"
            "       测点编号（87-L、87-R、88-L、88-R、测点6、测点16）、传感器型号\n"
            "  【C. 监测阈值/报警值】'一级阈值85t'、'二级阈值95t'、'风速阈值15m/s'、\n"
            "       温度阈值40℃——这些是按规范定的报警值，每期不变\n"
            "  【D. 规范对照表】风力分级对照表（依据GB/T 28591-2012）、地震烈度对照、\n"
            "       荷载等级表——国家/行业标准，每期不变\n"
            "  【E. 编号与日期】规范编号（JT/T 1037-2022、GB/T 28591-2012）、\n"
            "       章节号（2.1、3.2.1）、列表序号（(1)、（15））、版本号\n"
            "  【F. 元信息】监测单位名称、签字人、电话、邮箱、合同号、报告编号\n"
            "  【G. 表格列序号】表格第一行的\"一级\"、\"二级\"、\"三级\"列号\n"
            "       表格第一列的\"桥头\"、\"塔顶\"、\"主梁\"等测点区域名\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第二部分：占位符统一标准】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "所有动态值必须映射为：{{stats.<指标英文名>.<统计类型>}}\n"
            f"指标英文名（metric）只允许以下取值：{metric_desc}\n"
            f"统计类型（stat）只允许以下取值：{stat_desc}\n"
            "无法映射到上述指标的数字，用 {{data.N}}（N 为递增序号）。\n"
            "占位符必须以 {{stats. 或 {{data. 开头且以 }} 结尾。\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第三部分：你的任务（三重校验）】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "1️⃣ 完整性校验：对照【报告文本摘录】，找出【已提取动态值】之外漏掉的动态值。\n"
            "2️⃣ 正确性校验：找出【已提取动态值】中误判为动态的静态值（应改 keep）。\n"
            "3️⃣ 判定【待确认数字项】和【待确认图片】中的每一项是 replace 还是 keep。\n"
            "4️⃣ 季度表述动态化：找出标题/正文中需要替换的季度时间表述。\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第四部分：常见误判红线（务必警惕）】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "🚫 看到'测点+数字'/'数字-L'/'数字-R'/'87号'等 → 测点编号 → keep\n"
            "🚫 看到'共布置N个...测点'/'布设N个...测点' → 布设数量 → keep\n"
            "🚫 看到表格标题含'监测内容表'/'阈值'/'对照表'/'分级表'/'布设表'\n"
            "   → 整个表内数字基本是静态的（阈值/规范/布设），应 keep\n"
            "🚫 看到'《GB/T xxx-2012》'/'《JT/T xxx-2022》'/'国标'/'规范'附近数字\n"
            "   → 规范编号或规范值 → keep\n"
            "🚫 看到章节'X.Y'/'（N）'等列表编号 → keep\n"
            "🚫 看到表头列名（一级/二级/三级/桥头/塔顶/主梁/桥面/锚碇）→ keep\n"
            "🚫 看到'桥梁全长'/'桥宽'/'跨径'/'矢跨比'附近数字 → 设计参数 → keep\n"
            "🚫 看到'1/2边跨'/'主跨跨中'/'1/4'等位置标识数字 → 位置代号 → keep\n\n"
            "🚫 看到【节标题】里的数字（如“3.3.1.2  2/4跨主梁截面挠度监测”、\n"
            "   “上游湘潭侧中跨1/4箱梁结构温度监测”）→ 位置/编号 → keep\n"
            "🚫 看到检查/检测类静态叙述（如“抽取61个螺栓进行检测”、\n"
            "   “扭力值均在648～825N·m”、节点板编号 RE24-RE25）→ 检查记录 → keep\n"
            "🚫 missed（漏掉的动态值）只允许是【数字值】；RE24-RE25、K814+091 这类\n"
            "   字母编号、桩号、节点板号一律不要加入 missed\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第五部分：季度时间表述动态化】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "报告标题/正文中以下表述在生成新报告时需要动态替换：\n"
            "  - 'XXXX年第X季度' / '第X季度' / 'xxxx年第X季度（xx月-xx月）'\n"
            "    → {{date.period_label_cn}}（替换为如 2026年第一季度）\n"
            "  - '2026.1~3' / '2025.10~12' 等简短形式\n"
            "    → {{date.period_label}}（替换为如 2026.1~3）\n"
            "  - '本季度' / '本期' / '本周期'\n"
            "    → {{date.period_label_cn}}\n"
            "请将这些表述放入 text_replacements 数组（original 必须是原文精确片段）。\n\n"
            "═══════════════════════════════════════════════════════════════\n"
            "【第六部分：回复规则与格式】\n"
            "═══════════════════════════════════════════════════════════════\n"
            "▶ 若【待确认数字项】和【待确认图片】都为空，且任务 1、2 均无需修改\n"
            "  → 只回复一个字：是\n"
            "▶ 其他情况 → 回复 JSON（不要 markdown 代码块）：\n"
            "{\n"
            '  "missed": [{"value": "39.93", "snippet": "最高温度为39.93℃",'
            ' "placeholder": "{{stats.temperature.max}}", "reason": "漏掉的动态值"}],\n'
            '  "wrong": [{"value": "11", "snippet": "共布置11个温湿度仪",'
            ' "reason": "测点布设数量是静态配置"}],\n'
            '  "wrong_imgs": [{"index": 0, "reason": "风力分级对照表，GB/T 28591-2012规定"}],\n'
            '  "review": [{"index": 0, "verdict": "replace", "placeholder": "{{stats...}}"}],\n'
            '  "images_review": [{"index": 0, "verdict": "replace|keep"}],\n'
            '  "text_replacements": [{"original": "XXXX年第X季度（xx月-xx月）",'
            ' "replacement": "{{date.period_label}}", "reason": "季度动态化"}],\n'
            '  "summary": "一句话总结（正确识别X项，修正Y项误判，漏掉Z项）"\n'
            "}\n"
            "重要：review / images_review 数组中每一项都必须有 index；"
            "verdict 为 replace 时必须给出符合统一标准的 placeholder（以 {{stats. 或 {{data. 开头且以 }} 结尾）。"
        )

    def _build_user_content(
        self,
        doc_text: str,
        extracted: List[Dict],
        review_items: List[Dict],
        images: List[Dict],
        review_images: List[Dict],
    ) -> str:
        parts = []

        # 1. 报告文本
        parts.append("【报告文本摘录】")
        parts.append(doc_text[:80000] if doc_text else "（无）")

        # 2. 已提取动态值
        parts.append("\n【已提取动态值】（第一轮关键词筛选结果，共 %d 项）" % len(extracted))
        if extracted:
            for i, e in enumerate(extracted[:300]):
                parts.append(
                    f"{i + 1}. value={e.get('value', '')} | snippet={e.get('snippet', '')[:60]}"
                    f" | placeholder={e.get('placeholder', '') or '未映射'}"
                )
        else:
            parts.append("（无）")

        # 3. 待确认数字项
        parts.append("\n【待确认数字项】（共 %d 项）" % len(review_items))
        if review_items:
            for it in review_items:
                idx = it.get("_review_index", 0)
                parts.append(
                    f"index={idx} | value={it.get('value', '')}"
                    f" | snippet={it.get('snippet', '')[:80]}"
                    f" | 当前置信度={it.get('confidence', 0.5)}"
                )
        else:
            parts.append("（无）")

        # 4. 图片信息
        parts.append("\n【图片列表】（共 %d 张，括号内为当前判定）" % len(images))
        for img in images[:200]:
            cap = img.get("caption") or "（无图题）"
            parts.append(
                f"index={img.get('_image_index', img.get('index', 0))} | "
                f"caption={cap[:70]} | 当前判定={img.get('verdict', 'review')}"
            )

        # 5. 待确认图片
        if review_images:
            parts.append("\n【待确认图片】（共 %d 张）" % len(review_images))
            for img in review_images:
                idx = img.get("_review_index", 0)
                cap = img.get("caption") or "（无图题）"
                parts.append(f"index={idx} | caption={cap[:70]}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 回复解析
    # ------------------------------------------------------------------

    def _parse_response(
        self, resp: str, review_items: List[Dict], review_images: List[Dict]
    ) -> Dict:
        resp = (resp or "").strip()

        # 情况 1：模型回复"是" → 全部正确
        if resp == "是" or (resp.startswith("是") and len(resp) <= 3):
            log.info("LLM 校验结果：全部正确（回复'是'）")
            return {
                "complete": True,
                "missed": [],
                "wrong": [],
                "review_decisions": {},
                "images_review": {},
                "images_wrong": {},
                "text_replacements": [],
                "raw": resp,
            }

        # 情况 2：解析 JSON
        data = self._parse_json_loose(resp)
        if data is None:
            log.warning("LLM 回复无法解析为 JSON，按无修改处理：%s", resp[:120])
            return {
                "complete": False,
                "missed": [],
                "wrong": [],
                "review_decisions": {},
                "images_review": {},
                "images_wrong": {},
                "text_replacements": [],
                "raw": resp,
            }

        # 规整 review 判定
        review_decisions = {}
        for item in data.get("review", []):
            try:
                idx = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            if idx < 0:
                continue
            verdict = item.get("verdict", "review")
            if verdict not in ("replace", "keep"):
                continue
            placeholder = None
            if verdict == "replace":
                placeholder = normalize_placeholder(
                    item.get("placeholder", ""), idx + 1
                )
            review_decisions[idx] = {
                "verdict": verdict,
                "placeholder": placeholder,
                "confidence": 0.85,
                "reason": "LLM 判定",
            }

        # 规整漏掉的动态值
        missed = []
        for item in data.get("missed", []):
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            missed.append({
                "value": value,
                "snippet": item.get("snippet", ""),
                "placeholder": normalize_placeholder(
                    item.get("placeholder", ""), len(missed) + 1
                ),
                "reason": item.get("reason", ""),
            })

        # 规整误判项
        wrong = []
        for item in data.get("wrong", []):
            wrong.append({
                "value": str(item.get("value", "")).strip(),
                "snippet": item.get("snippet", ""),
                "reason": item.get("reason", ""),
            })

        # 图片 review 判定
        images_review = {}
        for item in data.get("images_review", []):
            try:
                idx = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            verdict = item.get("verdict", "review")
            if verdict in ("replace", "keep"):
                images_review[idx] = verdict

        images_wrong = {}
        for item in data.get("images_wrong", []):
            try:
                idx = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            images_wrong[idx] = "keep"

        # 文本级替换（季度/时间表述动态化）
        text_replacements = []
        for item in data.get("text_replacements", []):
            original = str(item.get("original", "")).strip()
            replacement = str(item.get("replacement", "")).strip()
            if original and replacement:
                text_replacements.append({
                    "original": original,
                    "replacement": replacement,
                    "reason": item.get("reason", ""),
                })

        log.info(
            "LLM 校验结果：missed=%d, wrong=%d, review判定=%d, 图片判定=%d, 文本替换=%d",
            len(missed), len(wrong), len(review_decisions),
            len(images_review), len(text_replacements),
        )
        return {
            "complete": False,
            "missed": missed,
            "wrong": wrong,
            "review_decisions": review_decisions,
            "images_review": images_review,
            "images_wrong": images_wrong,
            "text_replacements": text_replacements,
            "raw": resp,
        }

    @staticmethod
    def _parse_json_loose(resp: str):
        """从模型回复中宽松地提取 JSON 对象。"""
        # 去掉 markdown 代码块
        text = resp.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试从 { 到最后一个 } 截取
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None
