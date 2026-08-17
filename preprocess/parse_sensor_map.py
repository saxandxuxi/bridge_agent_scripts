#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从《五座桥测点编号表格.docx》提取传感器对照表，输出：
   1) 传感器编号 -> 中文名称(默认 preprocess/传感器对照/传感器编号名称.json)
   2) 按五座桥分别保存的 名称对照(默认 preprocess/传感器对照/传感器名称对照/<桥名>.json)，
     每个文件包含:
       - 传感器名称: 名称 -> [{编号, 特征(监测类别), 位置, 方向, 特征编码, 测点}]
       - 测点映射:   结构应变/振动监测表的 断面位置 -> 测点N -> 编号
       - 表格映射:   梁端支座位移 / 墩顶支座倾角 / 裂缝 / 温湿度 表映射

特征编码仅作参考(来自 Q1 统计值或按类别推断)，实际特征以
daily/<传感器编号>/ 下的特征目录为准，由 build_chart_library 生成
图库/统计库时读取。

用法:
    python parse_sensor_map.py [docx路径] [输出json路径] [统计值目录]
"""

import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DEFAULT_DOCX = r".\\inputs\\五座桥测点编号表格.docx"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "传感器对照", "传感器编号名称.json")
DEFAULT_NAME_MAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "传感器对照", "传感器名称对照")
DEFAULT_STATS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "统计值")

# 桥名段落(文档按 桥名 -> 类别 -> 表格 组织)
BRIDGE_NAMES = {
    "湘江特大桥", "湘江大桥",
    "洣水河", "洣水河特大桥", "洣水河大桥",
    "矮寨", "矮寨大桥",
    "赤石", "赤石大桥",
    "洞庭湖", "洞庭湖大桥",
}

# 桥名标准化(文件名/显示用全称)
BRIDGE_FULL = {
    "湘江特大桥": "湘江特大桥", "湘江大桥": "湘江特大桥",
    "洣水河": "洣水河特大桥", "洣水河特大桥": "洣水河特大桥",
    "洣水河大桥": "洣水河特大桥",
    "矮寨": "矮寨大桥", "矮寨大桥": "矮寨大桥",
    "赤石": "赤石大桥", "赤石大桥": "赤石大桥",
    "洞庭湖": "洞庭湖大桥", "洞庭湖大桥": "洞庭湖大桥",
}

BRIDGE_ORDER = ["湘江特大桥", "洣水河特大桥", "矮寨大桥",
                "赤石大桥", "洞庭湖大桥"]

# 不是"类别"的正文词(表头/行列标记等)
NOT_CATEGORY = {
    "位置", "监测部位", "编号", "方向", "具体位置", "传感器编号",
    "上游", "下游", "左幅", "右幅",
    "平均值", "最大值", "最小值", "差值", "缺失天数",
    "均方根值", "绝对最大值", "序号",
}

# 表头"位置"列的值(上游/下游/左幅/右幅/左/右/顶/底 等方位)，
# 解析时前向填充给整组行，并拼进传感器名称/监测部位(避免同一监测部位
# 多个方位挤成一个位置导致子图过多)
SIDE_WORDS = {"上游", "下游", "左幅", "右幅", "左侧", "右侧",
              "顶部", "底部", "上游侧", "下游侧"}

# 表头"方向"列的值(GNSS/地震/倾角等表格)，从监测部位里排除并单独保存
DIRECTION_WORDS = {
    "X", "Y", "Z",
    "纵桥向(X方向)", "横桥向(Y方向)", "竖向(Z方向)",
    "纵桥向（X方向）", "横桥向（Y方向）", "竖向（Z方向）",
}

# 监测类别 -> 实际数据特征编码(统计值 JSON 里使用的特征名)
CATEGORY_FEATURES = {
    "温湿度": ["WSD(temp)", "WSD(rh)"],
    "结构温度": ["WD(temp)"],
    "应变": ["YB(rsg)"],
    "振动": ["DZJSD(xJsd)"],
    "地震": ["DZJSD(xJsd)"],
    "挠度": ["ND(nd)"],
    "裂缝": ["LF(Δx)"],
    "索力": ["SL(sl)"],
    "倾角": ["EZJD(xJd)", "EZJD(yJd)"],
    "空间变位": ["GNSS(Δx)", "GNSS(Δy)", "GNSS(Δz)"],
    "风荷载": ["FSFX2(spfs)", "FSFX2(spfx)", "FSFX2(szfs)", "FSFX2(szfx)"],
    "风速": ["FSFX2(spfs)", "FSFX2(spfx)", "FSFX2(szfs)", "FSFX2(szfx)"],
}


def cell_text(tc):
    """取一个表格单元格的全部文本。"""
    return "".join(t.text or "" for t in tc.iter(W + "t")).strip()


def parse_docx(path):
    """按文档顺序解析：桥名/类别段落 + 表格行 -> {编号: 信息}。"""
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")

    sensors = {}
    current_bridge = ""
    current_category = ""

    for child in body:
        if child.tag == W + "p":
            text = "".join(t.text or "" for t in child.iter(W + "t")).strip()
            if not text:
                continue
            if text in BRIDGE_NAMES:
                current_bridge = text
                current_category = ""
            elif (len(text) <= 14 and text not in NOT_CATEGORY
                  and not re.fullmatch(r"\d+", text)):
                current_category = text
        elif child.tag == W + "tbl":
            current_side = ""   # 每个表格重新开始，"位置"列按组前向填充
            current_direction = ""
            for tr in child.findall(W + "tr"):
                cells = [cell_text(tc) for tc in tr.findall(W + "tc")]
                side = next((c for c in cells if c in SIDE_WORDS), "")
                if side:
                    current_side = side
                direction = next((c for c in cells if c in DIRECTION_WORDS), "")
                if direction:
                    current_direction = direction
                nums = [c for c in cells if re.fullmatch(r"\d+", c)]
                if not nums:
                    continue  # 表头行/无编号行
                text_cells = [c for c in cells
                              if c and not re.fullmatch(r"\d+", c)
                              and c not in NOT_CATEGORY
                              and c not in SIDE_WORDS
                              and c not in DIRECTION_WORDS]
                cjk_cells = [c for c in text_cells
                             if re.search(r"[\u4e00-\u9fff]", c)]
                location = (max(cjk_cells, key=len) if cjk_cells
                            else (max(text_cells, key=len)
                                  if text_cells else ""))
                extra = " ".join(c for c in text_cells
                                 if c != location and c not in
                                 (location, "上游", "下游", "左幅", "右幅"))
                name = location or (current_bridge + "-" + current_category)
                # "测点1/测点2..."只是同一位置不同传感器的索引，不属于位置名称；
                # 去掉结尾的测点编号后，同一位置的多传感器归为一组，
                # 测点序号由 enrich_entries 按组内顺序重新编号(测点1、测点2...)
                name = re.sub(r"测点\d+$", "", name)
                # 方位(位置列: 上游/下游/左/右/顶/底 等)拼进名称/监测部位，
                # 避免同一监测部位多个方位合并成一个位置(子图过多)；
                # 已含该方位后缀(如"4#墩...左侧")时不再重复追加
                if current_side and current_side not in name:
                    name = name + current_side
                for num in nums:
                    if num not in sensors:
                        sensors[num] = {
                            "桥名": current_bridge,
                            "类别": current_category,
                            "位置": current_side,
                            "方向": current_direction,
                            "监测部位": name,
                            "附加": extra,
                            "名称": name,
                        }
    return sensors


def load_data_features(stats_dir):
    """统计值/<编号>.json -> {编号: [实际特征名]}。"""
    feats = {}
    if not os.path.isdir(stats_dir):
        return feats
    for fn in sorted(os.listdir(stats_dir)):
        if not re.fullmatch(r"\d+\.json", fn):
            continue
        try:
            with open(os.path.join(stats_dir, fn), encoding="utf-8") as f:
                d = json.load(f)
            feats[fn[:-5]] = list(d.get("特征统计", {}).keys())
        except Exception:
            continue
    return feats


def expected_features(info):
    """类别 -> 特征编码的推断(有特殊名称时优先)。"""
    name = info.get("监测部位", "")
    if "梁端" in name:
        return ["WY(Δx)"]
    return CATEGORY_FEATURES.get(info.get("类别", ""), [])


def build_name_map(sensors):
    """
    按桥分组：传感器名称(监测部位) -> [{编号, 特征, 位置, 方向}]，
    保持测点编号表(文档)顺序，用于后续的测点N编号。
    """
    bridges = {}
    for num, info in sensors.items():
        bridge = BRIDGE_FULL.get(info.get("桥名", ""), info.get("桥名", ""))
        name = info.get("监测部位") or info.get("名称", "")
        if not name or not bridge:
            continue
        bridges.setdefault(bridge, {}).setdefault(name, []).append({
            "编号": num,
            "特征": info.get("类别", ""),
            "位置": info.get("位置", ""),
            "方向": info.get("方向", ""),
        })
    return bridges


def enrich_entries(name_map, sensors, data_feats, feat_ref=None):
    """给每个条目补 特征编码(仅作参考，实际以 daily 为准) 和 测点(应变/振动)。
    feat_ref: {编号: 特征编码} 合并补充模式下保留旧传感器已有的参考编码。"""
    for name, entries in name_map.items():
        cat_count = {}   # 同一名称下按类别分别计数，避免跨类别错位
        for e in entries:
            info = sensors.get(e["编号"], {})
            cat = info.get("类别", "")
            feats = data_feats.get(e["编号"])
            if not feats and feat_ref:
                feats = feat_ref.get(e["编号"])
            e["特征编码"] = feats or expected_features(info)
            if cat in ("应变", "振动"):
                cat_count[cat] = cat_count.get(cat, 0) + 1
                e["测点"] = f"测点{cat_count[cat]}"


def bridge_of(info):
    return BRIDGE_FULL.get(info.get("桥名", ""), info.get("桥名", ""))


def build_position_map(sensors, bridge_full):
    """
    结构应变/振动监测表: 断面位置 -> 测点N -> 编号(按编号表顺序)。
    """
    result = {}
    for cat, table_name in (("应变", "结构应变监测表"),
                            ("振动", "结构振动监测表")):
        groups = defaultdict(list)
        for num, info in sensors.items():
            if bridge_of(info) != bridge_full or info.get("类别") != cat:
                continue
            loc = info.get("监测部位") or info.get("名称", "")
            if loc:
                groups[loc].append(num)
        positions = [{"断面位置": loc,
                      "测点": {f"测点{i + 1}": num
                               for i, num in enumerate(ids)}}
                     for loc, ids in groups.items()]
        if positions:
            result[table_name] = positions
    return result


def build_table_map(sensors, bridge_full, data_feats):
    """梁端支座位移 / 墩顶支座倾角 / 裂缝 / 温湿度 表格映射。"""
    tm = {}

    support = {}
    for num, info in sensors.items():
        if bridge_of(info) != bridge_full:
            continue
        m = re.match(r"(\d+#)墩墩顶主梁梁端(左|右)侧", info.get("监测部位", ""))
        if m:
            support.setdefault(m.group(1), {})[m.group(2)] = {
                "编号": num, "特征": "WY(Δx)"}
    if support:
        tm["梁端支座位移表"] = support

    tilt = {}
    for num, info in sensors.items():
        if bridge_of(info) != bridge_full:
            continue
        m = re.match(r"(\d+#)墩墩顶主梁支座(左|右)侧(X|Y)",
                     info.get("监测部位", ""))
        if m:
            key = m.group(2) + m.group(3)
            tilt.setdefault(m.group(1), {})[key] = {
                "编号": num,
                "特征": "EZJD(xJd)" if m.group(3) == "X" else "EZJD(yJd)"}
    # Y 向与 X 向共用同一传感器(该传感器同时有 xJd/yJd 特征)
    for pier in list(tilt):
        for side in ("左", "右"):
            x = tilt[pier].get(side + "X")
            if x and side + "Y" not in tilt[pier]:
                tilt[pier][side + "Y"] = {
                    "编号": x["编号"], "特征": "EZJD(yJd)"}
    if tilt:
        tm["墩顶支座倾角表"] = tilt

    crack = {}
    for num, info in sensors.items():
        if bridge_of(info) != bridge_full:
            continue
        if info.get("类别") == "裂缝":
            crack.setdefault(info.get("监测部位", ""), []).append(num)
    if crack:
        tm["裂缝监测表"] = crack

    temp = {}
    for num, info in sensors.items():
        if bridge_of(info) != bridge_full:
            continue
        if info.get("类别") == "温湿度":
            temp.setdefault(info.get("监测部位", ""), []).append(num)
    if temp:
        tm["温湿度表"] = temp

    struct_temp = {}
    for num, info in sensors.items():
        if bridge_of(info) != bridge_full:
            continue
        if info.get("类别") == "结构温度":
            struct_temp.setdefault(info.get("监测部位", ""), []).append(num)
    if struct_temp:
        tm["结构温度表"] = struct_temp
    return tm


def write_name_map_files(bridges, sensors, data_feats, out_dir,
                         feat_ref=None):
    """把完善后的按桥名称对照写成 5 个 JSON 文件。"""
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for bridge in BRIDGE_ORDER:
        if bridge not in bridges:
            continue
        enrich_entries(bridges[bridge], sensors, data_feats, feat_ref)
        pos_map = build_position_map(sensors, bridge)
        table_map = build_table_map(sensors, bridge, data_feats)
        data = {
            "桥名": bridge,
            "说明": "传感器名称 -> 编号/特征/位置/方向 对照，"
                    "含 特征编码(仅作参考，实际特征以 daily/<编号>/ 目录"
                    "为准) 与 测点(应变/振动)；"
                    "由《五座桥测点编号表格.docx》+ Q1 统计值生成",
            "传感器数量": sum(len(v) for v in bridges[bridge].values()),
            "传感器名称": bridges[bridge],
        }
        if pos_map:
            data["测点映射"] = pos_map
        if table_map:
            data["表格映射"] = table_map
        path = os.path.join(out_dir, bridge + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        written.append(path)
    return written


def main():
    docx_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOCX
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    stats_dir = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_STATS
    merge_mode = "--merge" in sys.argv

    if not os.path.exists(docx_path):
        print(f"[错误] 找不到文档: {docx_path}")
        sys.exit(1)

    sensors = parse_docx(docx_path)
    if not sensors:
        print("[错误] 未能从文档中解析出任何传感器编号")
        sys.exit(1)
    data_feats = load_data_features(stats_dir)
    if data_feats:
        print(f"已加载 Q1 统计值特征(传感器 {len(data_feats)} 个)")
    else:
        print("[提示] 未找到统计值 JSON，特征编码将按监测类别推断")

    parsed_ids = set(sensors)
    feat_ref = {}
    if merge_mode and os.path.isfile(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = (json.load(f) or {}).get("传感器", {}) or {}
            for num in existing:
                feat_ref[num] = (existing[num] or {}).get("特征编码", [])
            sensors = {**existing, **sensors}   # 新解析的覆盖同编号，其余保留
            print(f"[合并] 已有对照 {len(existing)} 个，"
                  f"本次新增/覆盖 {len(parsed_ids)} 个，"
                  f"合并后共 {len(sensors)} 个")
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 读取旧对照失败，按完整模式生成: {exc}")

    data = {
        "说明": "传感器编号 -> 中文监测部位对照表，"
                "由《五座桥测点编号表格.docx》生成；"
                "特征编码仅作参考，实际特征以 daily/<编号>/ 目录为准",
        "生成时间": "",
        "传感器数量": len(sensors),
        "传感器": {
            num: {**info,
                  "特征编码": (data_feats.get(num)
                               or feat_ref.get(num)
                               or expected_features(info))}
            for num, info in sensors.items()
        },
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 按桥分组的 名称 -> 编号/特征 JSON(完善版)
    bridges = build_name_map(sensors)
    name_files = write_name_map_files(bridges, sensors, data_feats,
                                      DEFAULT_NAME_MAP_DIR, feat_ref)

    print(f"共解析出 {len(sensors)} 个传感器")
    print(f"已保存: {out_path}")
    print("按桥名称对照表(含特征编码/测点/表格映射):")
    for p in name_files:
        print(f"  {p}")


if __name__ == "__main__":
    main()
