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
    for b in list_bridges(registry_path):
        if b.get("id") == bridge_id or b.get("name") == bridge_id:
            return b
    return None


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
