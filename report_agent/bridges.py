# -*- coding: utf-8 -*-
"""多桥注册表：六座桥 / 多台服务器的统一管理入口。

注册表文件：<项目根>/bridges/registry.json

结构：
{
  "bridges": [
    {
      "id": "chishi",
      "name": "赤石大桥",
      "config": "config_chishi.json",            // 相对项目根或绝对路径
      "host": "222.242.152.65",                  // 部署该桥的服务器
      "port": 8080,                              // 该服务器上 Web 服务端口
      "token_env": "BRIDGE_CHISHI_TOKEN",        // 访问令牌的环境变量名
      "description": "赤石大桥健康监测"
    }
  ]
}

CLI 用法：
  python run_agent.py --bridge chishi --mode quarterly
"""

import json
import logging
import os
from typing import Dict, List, Optional

log = logging.getLogger("report-agent.bridges")


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_registry_path() -> str:
    return os.path.join(project_root(), "bridges", "registry.json")


def load_registry(registry_path: Optional[str] = None) -> Dict:
    """读取桥梁注册表；文件不存在时返回空注册表。"""
    path = registry_path or default_registry_path()
    if not os.path.isfile(path):
        return {"path": path, "bridges": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bridges = data.get("bridges", []) or []
        return {"path": path, "bridges": bridges}
    except Exception as exc:  # noqa: BLE001
        log.warning("读取桥梁注册表失败 %s: %s", path, exc)
        return {"path": path, "bridges": []}


def list_bridges(registry_path: Optional[str] = None) -> List[Dict]:
    return load_registry(registry_path)["bridges"]


def get_bridge(bridge_id: str, registry_path: Optional[str] = None) -> Optional[Dict]:
    # 精确匹配（id 或 name）优先
    for b in list_bridges(registry_path):
        if b.get("id") == bridge_id or b.get("name") == bridge_id:
            return b
    # 模糊匹配：去掉“大桥/特大桥”后缀后按包含/前缀匹配
    # （如 --bridge 洣水河特 -> 洣水河特大桥；mishui -> mishuihe）
    q = _bridge_key(bridge_id)
    if q:
        for b in list_bridges(registry_path):
            if _bridge_key(b.get("name")) == q:
                return b
        for b in list_bridges(registry_path):
            if _bridge_fuzzy(q, b.get("name")):
                return b
        for b in list_bridges(registry_path):
            if _bridge_fuzzy(q, b.get("id")):
                return b
    return None


def _bridge_key(s) -> str:
    """桥名/ID 归一化：去空白、去“大桥/特大桥”后缀、转小写。"""
    s = str(s or "").strip().lower()
    for suffix in ("特大桥", "大桥"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _bridge_fuzzy(query: str, candidate) -> bool:
    """桥名/ID 模糊匹配：子串/前后缀包含；query 或 candidate 太短时不匹配。"""
    c = _bridge_key(candidate)
    if not query or not c or len(query) < 2 or len(c) < 2:
        return False
    if query in c or c in query:
        return True
    # ID 前缀匹配（mishui -> mishuihe）
    if len(query) >= 3 and (c.startswith(query) or query.startswith(c)):
        return True
    return False


def resolve_bridge_config(bridge_id: str, registry_path: Optional[str] = None) -> Optional[str]:
    """把桥 ID 解析为配置文件路径；找不到返回 None。"""
    bridge = get_bridge(bridge_id, registry_path)
    if bridge:
        cfg = bridge.get("config", "")
        if not cfg:
            return None
        if os.path.isabs(cfg):
            return cfg if os.path.isfile(cfg) else None
        root = os.path.dirname(os.path.abspath(registry_path or default_registry_path()))
        cand = os.path.join(root, cfg)
        if os.path.isfile(cand):
            return cand
        cand2 = os.path.join(project_root(), cfg)
        return cand2 if os.path.isfile(cand2) else None
    # 回退：bridges/<id>/config.json 或 bridges/<id>.json
    for cand in (
        os.path.join(project_root(), "bridges", bridge_id, "config.json"),
        os.path.join(project_root(), "bridges", bridge_id + ".json"),
        os.path.join(project_root(), "config", "config_" + bridge_id + ".json"),
        os.path.join(project_root(), "config_" + bridge_id + ".json"),
    ):
        if os.path.isfile(cand):
            return cand
    return None
