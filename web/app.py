# -*- coding: utf-8 -*-
"""报告智能体 Web 管理台。

两种运行模式：
  bridge（默认）：本服务管理本机的一个/多个桥配置，提供运行、下载、覆盖度等接口。
  hub：部署在中心服务器（如 222.242.152.65），汇总各桥服务器的状态并提供跳转。

环境变量：
  REPORT_WEB_HOST        监听地址（默认 127.0.0.1，公网请用 nginx 反代 + HTTPS）
  REPORT_WEB_PORT        端口（默认 8080）
  REPORT_WEB_TOKEN       访问令牌；为空时不鉴权（仅建议本机调试）
  REPORT_WEB_MODE        bridge | hub（默认 bridge）
  REPORT_WEB_REGISTRY    桥梁注册表路径（默认 bridges/registry.json）
  REPORT_PROJECT_ROOT    项目根目录（默认自动推断）

启动：
  python web/app.py
  REPORT_WEB_TOKEN=xxx python web/app.py
"""

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

from flask import Flask, Response, jsonify, request, send_file

ROOT = os.environ.get("REPORT_PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from report_agent.bridges import get_bridge, list_bridges, resolve_bridge_config  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="/static")

log = logging.getLogger("report-web")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

WEB_TOKEN = os.environ.get("REPORT_WEB_TOKEN", "")
WEB_MODE = os.environ.get("REPORT_WEB_MODE", "bridge")
REGISTRY = os.environ.get("REPORT_WEB_REGISTRY", os.path.join(ROOT, "bridges", "registry.json"))

PREPROCESS_DIR = os.path.join(ROOT, "preprocess")
PREPROCESS_CONFIG = os.path.join(PREPROCESS_DIR, "config.json")
PREPROCESS_STATUS = os.path.join(PREPROCESS_DIR, "status.json")
PREPROCESS_LOG = os.path.join(PREPROCESS_DIR, "pipeline.log")
_preprocess: Dict = {}
_parsing: Dict[str, Dict] = {}   # 模板解析状态（LLM 识别较慢，后台执行）

# 每个桥的“运行中”状态
_running: Dict[str, Dict] = {}
_run_lock = threading.Lock()
_schedulers: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# 周期 / 季度工具
# ---------------------------------------------------------------------------

def _period_from_mode(mode: str, date_str: str = "") -> Dict:
    """按报告模式计算周期，返回 {start, end, label}。
    label 示例: quarterly -> 2026.1~3 / 2026.4~6；monthly -> 2026.07。"""
    import calendar
    end = dt.date.fromisoformat(date_str) if date_str else dt.date.today()
    mode = mode or "quarterly"
    if mode == "yearly":
        start = dt.date(end.year, 1, 1)
        end = dt.date(end.year, 12, 31)
        label = f"{end.year}年"
    elif mode == "quarterly":
        q = (end.month - 1) // 3 + 1
        ms, me = (q - 1) * 3 + 1, q * 3
        start = dt.date(end.year, ms, 1)
        end = dt.date(end.year, me, calendar.monthrange(end.year, me)[1])
        label = f"{end.year}.{ms}~{me}"
    elif mode == "monthly":
        start = dt.date(end.year, end.month, 1)
        end = dt.date(end.year, end.month,
                      calendar.monthrange(end.year, end.month)[1])
        label = f"{end.year}.{end.month:02d}"
    else:  # weekly / manual
        start = end - dt.timedelta(days=6)
        label = f"{end.year}.{end.month:02d}.{end.day:02d}"
    return {"start": start.isoformat(), "end": end.isoformat(), "label": label}


def _label_from_range(start: str, end: str) -> str:
    """由起止日期生成目录标签：同年同季 -> 2026.1~3；同年同月 -> 2026.07。"""
    try:
        sm, em = int(start[5:7]), int(end[5:7])
        sy, ey = start[:4], end[:4]
    except (IndexError, ValueError):
        return ""
    if sy == ey and sm == em:
        return f"{sy}.{sm:02d}"
    if sy == ey and (sm, em) in ((1, 3), (4, 6), (7, 9), (10, 12)):
        return f"{sy}.{sm}~{em}"
    return f"{sy}{sm:02d}-{ey}{em:02d}"


def _period_dir_base(cfg: Optional[Dict] = None) -> str:
    """季度目录的上级目录：从配置图库目录解析出 preprocess/ 这一级。
    兼容 图库_<期>/<桥名> 与 图库/<桥名> 两种布局。"""
    bd = (cfg or {}).get("bridge_data") or {}
    cd = str(bd.get("charts_dir", "") or "").replace("\\", "/")
    for marker in ("图库_", "图库"):
        idx = cd.find(marker)
        if idx > 0:
            base = cd[:idx].rstrip("/")
            return base if os.path.isabs(base) else os.path.normpath(
                os.path.join(ROOT, base))
    return PREPROCESS_DIR


def _quarter_dirs(cfg: Optional[Dict], label: str) -> tuple:
    """季度化输出目录：<base>/图库_<label>/<桥名>、<base>/统计值_<label>/<桥名>；
    桥名写法不一致时(湘江特大桥 <-> 湘江特)自动匹配实际存在的子目录。"""
    base = _period_dir_base(cfg)
    bridge = ((cfg or {}).get("bridge_data") or {}).get("bridge_name", "")
    def _with_bridge(p: str) -> str:
        return os.path.join(p, bridge) if bridge else p
    from report_agent.config import resolve_bridge_subdir
    if not label:
        charts = _with_bridge(os.path.join(base, "图库"))
        stats = _with_bridge(os.path.join(base, "统计值"))
    else:
        charts = _with_bridge(os.path.join(base, f"图库_{label}"))
        stats = _with_bridge(os.path.join(base, f"统计值_{label}"))
    return (resolve_bridge_subdir(charts, bridge),
            resolve_bridge_subdir(stats, bridge))


def _dir_nonempty(path: str) -> bool:
    return bool(os.path.isdir(path) and any(True for _ in os.scandir(path)))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _config_for(bridge_id: str) -> Optional[Dict]:
    cfg_path = resolve_bridge_config(bridge_id, REGISTRY)
    if not cfg_path:
        return None
    try:
        from report_agent.config import load_config
        return load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "_config_path": cfg_path}


def _config_path_for(bridge_id: str) -> Optional[str]:
    return resolve_bridge_config(bridge_id, REGISTRY)


def _save_config(cfg: Dict, cfg_path: str) -> None:
    """写回配置（先备份 .bak）。"""
    try:
        shutil.copyfile(cfg_path, cfg_path + ".bak")
    except OSError:
        pass
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _raw_config(cfg_path: str) -> Dict:
    """直接读取配置文件原始 JSON（不做路径解析），用于局部更新时
    保留相对路径，避免把配置全部改写成绝对路径。"""
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _repair_filename(name: str) -> str:
    """修复 Windows 浏览器上传中文文件名时的 GBK 乱码
    （GBK 字节被按 Latin-1 解码，如 湘江特大桥 -> Ïæ½­ÌØ´óÇÅ）。"""
    if not name:
        return name
    try:
        repaired = name.encode("latin-1").decode("gbk")
        if any("\u4e00" <= ch <= "\u9fff" for ch in repaired):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def _mask_secrets(cfg: Dict) -> Dict:
    out = dict(cfg)
    llm = out.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        llm = dict(llm)
        llm["api_key"] = "******" if llm["api_key"] else ""
        out["llm"] = llm
    return out


def _bridge_snapshot(bridge: Dict) -> Dict:
    bid = bridge.get("id", "")
    cfg = _config_for(bid)
    snap = {
        "id": bid,
        "name": bridge.get("name", bid),
        "host": bridge.get("host", ""),
        "port": bridge.get("port", 8080),
        "token_env": bridge.get("token_env", ""),
        "description": bridge.get("description", ""),
        "config": bridge.get("config", ""),
        "config_ok": isinstance(cfg, dict) and "error" not in cfg,
        "config_error": cfg.get("error") if isinstance(cfg, dict) else None,
    }
    if isinstance(cfg, dict) and "error" not in cfg:
        snap["template"] = cfg.get("template", "")
        snap["output_dir"] = cfg.get("output_dir", "")
        snap["bridge_data"] = bool((cfg.get("bridge_data") or {}).get("enabled", False))
        snap["schedule"] = cfg.get("schedule", {})
        out_dir = cfg.get("output_dir", "")
        if out_dir and os.path.isdir(out_dir):
            docs = [f for f in os.listdir(out_dir)
                    if f.lower().endswith(".docx") and not f.startswith("~$")]
            docs.sort(key=lambda f: os.path.getmtime(os.path.join(out_dir, f)), reverse=True)
            snap["report_count"] = len(docs)
            snap["latest_report"] = docs[0] if docs else None
            snap["latest_report_mtime"] = (
                dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(out_dir, docs[0]))).isoformat(timespec="seconds")
                if docs else None
            )
        last_run = os.path.join(out_dir or "", "last_run.json")
        if os.path.isfile(last_run):
            try:
                with open(last_run, "r", encoding="utf-8") as f:
                    lr = json.load(f)
                snap["last_run"] = {
                    "output": lr.get("output"),
                    "period": lr.get("period"),
                    "days": lr.get("days"),
                    "pending_charts": len(lr.get("pending_charts", [])),
                    "missing_cells": len(lr.get("missing_cells", [])),
                }
            except Exception:  # noqa: BLE001
                pass
    snap["running"] = bool(_running.get(bid, {}).get("running"))
    return snap


def _require_token() -> Optional[Response]:
    if not WEB_TOKEN:
        return None
    token = request.headers.get("X-Auth-Token", "") or request.args.get("token", "")
    if token != WEB_TOKEN:
        return jsonify({"error": "未授权：token 无效"}), 401
    return None


def _subprocess_env() -> Dict:
    env = dict(os.environ)
    paths = [ROOT]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status")
def api_status():
    auth = _require_token()
    if auth:
        return auth
    return jsonify({
        "ok": True,
        "mode": WEB_MODE,
        "project_root": ROOT,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "registry": REGISTRY,
        "auth_required": bool(WEB_TOKEN),
        "version": "1.0",
    })


@app.route("/api/bridges")
def api_bridges():
    auth = _require_token()
    if auth:
        return auth
    return jsonify({"bridges": [_bridge_snapshot(b) for b in list_bridges(REGISTRY)]})


@app.route("/api/bridges/<bridge_id>")
def api_bridge(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    b = get_bridge(bridge_id, REGISTRY)
    if not b:
        return jsonify({"error": f"未找到桥梁 {bridge_id}"}), 404
    return jsonify(_bridge_snapshot(b))


@app.route("/api/bridges/<bridge_id>/config")
def api_bridge_config(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if cfg is None:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    return jsonify(_mask_secrets(cfg))


@app.route("/api/bridges/<bridge_id>/config", methods=["POST"])
def api_bridge_config_update(bridge_id):
    """更新桥配置（数据路径 / 模板 / 调度 / 报告命名等），先备份再写回。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    try:
        from report_agent.config import load_config
        cfg = load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"配置不可用: {exc}"}), 400

    data = request.get_json(silent=True) or {}
    bd = cfg.setdefault("bridge_data", {})
    if isinstance(data.get("paths"), dict):
        for k in ("stats_dir", "charts_dir", "sensor_map", "name_dict", "overview"):
            if k in data["paths"] and data["paths"][k] is not None:
                bd[k] = str(data["paths"][k]).strip()
    for k in ("sensor_exclude", "auto_fill_missing_charts", "fuzzy_threshold", "period_aggregate"):
        if k in data:
            bd[k] = data[k]
    if isinstance(data.get("metrics"), dict):
        bd["metrics"] = data["metrics"]
    if data.get("template"):
        cfg["template"] = str(data["template"]).strip()
    if data.get("output_dir"):
        cfg["output_dir"] = str(data["output_dir"]).strip()
    if data.get("analysis_file") is not None:
        cfg["analysis_file"] = str(data["analysis_file"]).strip() or None
    if isinstance(data.get("schedule"), dict):
        sch = data["schedule"]
        if sch.get("mode") in ("weekly", "monthly", "quarterly", "yearly"):
            cfg.setdefault("schedule", {})["mode"] = sch["mode"]
        for k in ("weekday", "day_of_month", "hour", "minute"):
            if k in sch and sch[k] is not None:
                try:
                    cfg.setdefault("schedule", {})[k] = int(sch[k])
                except (TypeError, ValueError):
                    pass
    if isinstance(data.get("report"), dict) and data["report"].get("name_prefix") is not None:
        cfg.setdefault("report", {})["name_prefix"] = str(data["report"]["name_prefix"])

    _save_config(cfg, cfg_path)
    return jsonify({"ok": True, "config_path": cfg_path})


@app.route("/api/bridges/<bridge_id>/template", methods=["POST"])
def api_bridge_template_upload(bridge_id):
    """上传报告模板 .docx，保存到 templates/ 并更新配置。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    name = _repair_filename(os.path.basename(f.filename))
    if not name.lower().endswith(".docx"):
        return jsonify({"error": "仅支持 .docx 模板"}), 400
    templates_dir = os.path.join(ROOT, "templates")
    os.makedirs(templates_dir, exist_ok=True)
    dest = os.path.join(templates_dir, name)
    f.save(dest)
    try:
        cfg = _raw_config(cfg_path)
        cfg["template"] = os.path.join("templates", name)
        _save_config(cfg, cfg_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"模板已上传但配置更新失败: {exc}"}), 500
    return jsonify({"ok": True, "template": os.path.join("templates", name), "size": os.path.getsize(dest)})


def _bridge_template_files(bridge_name: str) -> list:
    """列出 templates/ 下某桥的模板文件（含版本号），按版本号/时间倒序。"""
    tpl_dir = os.path.join(ROOT, "templates")
    if not os.path.isdir(tpl_dir):
        return []
    prefix = (bridge_name or "") + "_template"
    files = []
    for fn in sorted(os.listdir(tpl_dir)):
        if not fn.lower().endswith(".docx"):
            continue
        if prefix and not fn.startswith(prefix):
            continue
        p = os.path.join(tpl_dir, fn)
        files.append({
            "name": fn,
            "path": os.path.relpath(p, ROOT).replace("\\", "/"),
            "size": os.path.getsize(p),
            "mtime": dt.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds"),
        })
    files.sort(key=lambda x: (x["mtime"], x["name"]), reverse=True)
    return files


@app.route("/api/bridges/<bridge_id>/templates")
def api_bridge_templates(bridge_id):
    """列出该桥可选的模板（含当前配置模板标记）。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    bname = (cfg.get("bridge_data") or {}).get("bridge_name") or bridge_id
    files = _bridge_template_files(bname)
    if not files:
        # 桥名前缀没匹配到时，退回列出全部模板
        files = _bridge_template_files("")
    current = os.path.basename(cfg.get("template", ""))
    for f in files:
        f["current"] = f["name"] == current
    return jsonify({"templates": files, "current": current,
                    "bridge_name": bname})


@app.route("/api/bridges/<bridge_id>/templates/<path:filename>")
def api_bridge_template_download(bridge_id, filename):
    """下载某模板 .docx。"""
    auth = _require_token()
    if auth:
        return auth
    safe = os.path.basename(filename)
    path = os.path.join(ROOT, "templates", safe)
    if not os.path.isfile(path) or not safe.lower().endswith(".docx"):
        return jsonify({"error": "模板文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/api/bridges/<bridge_id>/analysis/<path:filename>")
def api_bridge_analysis_download(bridge_id, filename):
    """下载解析分析 JSON。"""
    auth = _require_token()
    if auth:
        return auth
    safe = os.path.basename(filename)
    path = os.path.join(ROOT, "outputs", "analysis", safe)
    if not os.path.isfile(path) or not safe.lower().endswith(".json"):
        return jsonify({"error": "分析文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/api/bridges/<bridge_id>/source-report", methods=["POST"])
def api_bridge_source_report_upload(bridge_id):
    """上传成品报告 .docx。

    表单字段：
      file               成品报告 .docx
      bridge_target      same=当前桥 / new=新桥
      new_bridge_id      新桥 ID（bridge_target=new 时必填，如 xinhe）
      new_bridge_name    新桥名称（如 新河特大桥）
    已有桥：保存到 inputs/ 并更新配置 source_report；
    新桥：自动生成 config_<id>.json 并登记到 registry.json。
    """
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    name = _repair_filename(os.path.basename(f.filename))
    if not name.lower().endswith(".docx"):
        return jsonify({"error": "仅支持 .docx 成品报告"}), 400

    inputs_dir = os.path.join(ROOT, "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    # 重名时加时间戳，避免覆盖旧成品报告
    dest = os.path.join(inputs_dir, name)
    if os.path.isfile(dest):
        stem, ext = os.path.splitext(name)
        dest = os.path.join(
            inputs_dir, f"{stem}_{dt.datetime.now():%Y%m%d_%H%M%S}{ext}")
    f.save(dest)
    rel_report = os.path.relpath(dest, ROOT).replace("\\", "/")

    bridge_target = str(request.form.get("bridge_target") or "same").strip()
    if bridge_target == "new":
        new_name = str(request.form.get("new_bridge_name") or "").strip()
        new_id = str(request.form.get("new_bridge_id") or "").strip()
        if not new_name:
            return jsonify({"error": "新桥请填写桥名（new_bridge_name）"}), 400
        try:
            from setup_bridge import _bridge_id, build_config, register_bridge
            bid = new_id or _bridge_id(new_name)
            cfg = build_config(new_name, os.path.abspath(dest))
            cfg_dir = os.path.join(ROOT, "config")
            os.makedirs(cfg_dir, exist_ok=True)
            new_cfg_path = os.path.join(cfg_dir, f"config_{bid}.json")
            with open(new_cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            register_bridge(bid, new_name, new_cfg_path)
            log.info("新桥登记完成: id=%s name=%s config=%s",
                     bid, new_name, new_cfg_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"新桥配置生成失败: {exc}"}), 500
        return jsonify({"ok": True, "bridge_id": bid, "bridge_name": new_name,
                        "source_report": rel_report,
                        "config": os.path.join("config",
                                               "config_" + bid + ".json")})

    # 已有桥：更新 source_report，保留当前模板
    try:
        cfg = _raw_config(cfg_path)
        cfg["source_report"] = rel_report
        _save_config(cfg, cfg_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"报告已保存但配置更新失败: {exc}"}), 500
    return jsonify({"ok": True, "bridge_id": bridge_id,
                    "source_report": rel_report,
                    "template": cfg.get("template", "")})


def _next_template_version(bridge_name: str) -> str:
    """取 templates/<桥名>_template 的下一版本号，返回文件名。"""
    tpl_dir = os.path.join(ROOT, "templates")
    prefix = (bridge_name or "桥") + "_template"
    max_ver = 0
    has_plain = False
    if os.path.isdir(tpl_dir):
        for fn in os.listdir(tpl_dir):
            if not fn.lower().endswith(".docx"):
                continue
            stem = fn[:-5]
            if stem == prefix:
                has_plain = True
            else:
                m = re.match(re.escape(prefix) + r"_v(\d+)$", stem)
                if m:
                    max_ver = max(max_ver, int(m.group(1)))
    if not has_plain and max_ver == 0:
        return f"{prefix}.docx"
    return f"{prefix}_v{max_ver + 1}.docx"


def _parse_template_worker(bridge_id: str, cfg_path: str,
                           source_report: str) -> None:
    """后台执行 analyze_report.py，把成品报告重新解析成模板。"""
    st = _parsing.setdefault(bridge_id, {"running": False})
    try:
        cfg = _raw_config(cfg_path)
        b = get_bridge(bridge_id, REGISTRY) or {}
        bname = ((cfg.get("bridge_data") or {}).get("bridge_name")
                 or b.get("name") or bridge_id)
        tpl_name = _next_template_version(bname)
        tpl_path = os.path.join(ROOT, "templates", tpl_name)
        analysis_path = os.path.join(
            ROOT, "outputs", "analysis",
            "analysis_" + os.path.splitext(os.path.basename(source_report))[0]
            + ".json")
        cmd = [sys.executable, os.path.join(ROOT, "analyze_report.py"),
               "--input", os.path.abspath(source_report),
               "--config", cfg_path,
               "--annotate", tpl_path,
               "--log", os.path.join(ROOT, "outputs", "logs",
                                     f"analyze_report_{bridge_id}.log")]
        st["cmd"] = " ".join(cmd)
        proc = subprocess.Popen(cmd, cwd=ROOT, env=_subprocess_env(),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        st["pid"] = proc.pid
        out, _ = proc.communicate(timeout=7200)
        st["returncode"] = proc.returncode
        st["log_tail"] = out.decode("utf-8", errors="replace")[-3000:]
        if proc.returncode == 0 and os.path.isfile(tpl_path):
            cfg = _raw_config(cfg_path)
            cfg["template"] = os.path.relpath(tpl_path, ROOT).replace("\\", "/")
            if os.path.isfile(analysis_path):
                cfg["analysis_file"] = os.path.relpath(
                    analysis_path, ROOT).replace("\\", "/")
            _save_config(cfg, cfg_path)
            st["template"] = cfg["template"]
        else:
            st["error"] = "模板解析失败，详见日志尾部"
        st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        st["error"] = str(exc)
        st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        log.exception("桥梁 %s 模板解析异常", bridge_id)
    finally:
        st["running"] = False


@app.route("/api/bridges/<bridge_id>/template/parse", methods=["POST"])
def api_bridge_template_parse(bridge_id):
    """重新解析成品报告 -> 生成新模板（后台执行，LLM 识别较慢）。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    from report_agent.config import load_config
    cfg = load_config(cfg_path)
    source_report = cfg.get("source_report", "")
    if not source_report or not os.path.isfile(source_report):
        return jsonify({"error": "尚未上传成品报告（source_report 为空或文件不存在）"}), 400
    st = _parsing.get(bridge_id)
    if st and st.get("running"):
        return jsonify({"error": "模板解析已在运行", "started_at": st.get("started_at")}), 409
    _parsing[bridge_id] = {
        "running": True,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    t = threading.Thread(target=_parse_template_worker,
                         args=(bridge_id, cfg_path, source_report),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True,
                    "source_report": source_report,
                    "started_at": _parsing[bridge_id]["started_at"]})


@app.route("/api/bridges/<bridge_id>/parse/status")
def api_bridge_parse_status(bridge_id):
    """模板解析状态。"""
    auth = _require_token()
    if auth:
        return auth
    return jsonify(_parsing.get(bridge_id, {"running": False}))


@app.route("/api/bridges/<bridge_id>/parse/result")
def api_bridge_parse_result(bridge_id):
    """模板解析结果摘要：新模板 + 分析统计（数字/图片/图表占位/数据占位）。"""
    auth = _require_token()
    if auth:
        return auth
    st = _parsing.get(bridge_id, {})
    cfg = _config_for(bridge_id)
    out = {"status": st, "template": None, "analysis": None,
           "analysis_download": ""}
    if not cfg or "error" in cfg:
        return jsonify(out)
    tpl = st.get("template") or cfg.get("template", "")
    if tpl and os.path.isfile(tpl):
        out["template"] = {
            "name": os.path.basename(tpl),
            "path": os.path.relpath(tpl, ROOT).replace("\\", "/"),
            "size": os.path.getsize(tpl),
            "mtime": dt.datetime.fromtimestamp(os.path.getmtime(tpl)).isoformat(timespec="seconds"),
            "download": "/api/bridges/%s/templates/%s" % (
                bridge_id, os.path.basename(tpl)),
        }
    src = cfg.get("source_report", "")
    if src:
        base = os.path.splitext(os.path.basename(src))[0]
        apath = os.path.join(ROOT, "outputs", "analysis",
                             f"analysis_{base}.json")
        if not os.path.isfile(apath):
            # 兼容带时间戳/版本的 analysis 文件，取最新一份
            adir = os.path.join(ROOT, "outputs", "analysis")
            hits = [f for f in os.listdir(adir)
                    if f.startswith(f"analysis_{base}") and f.endswith(".json")]
            if hits:
                apath = os.path.join(adir, sorted(hits,
                                                  key=lambda f: os.path.getmtime(
                                                      os.path.join(adir, f)))[-1])
        if os.path.isfile(apath):
            try:
                with open(apath, "r", encoding="utf-8") as fh:
                    a = json.load(fh)
                out["analysis"] = {
                    "path": os.path.relpath(apath, ROOT).replace("\\", "/"),
                    "summary": a.get("summary", {}),
                    "numbers": len(a.get("numbers", [])),
                    "images": len(a.get("images", [])),
                    "chart_texts": len(a.get("chart_texts", [])),
                    "data_values": len(a.get("data_values", {})),
                    "texts": len(a.get("texts", [])),
                }
                out["analysis_download"] = (
                    "/api/bridges/%s/analysis/%s" % (
                        bridge_id, os.path.basename(apath)))
            except Exception as exc:  # noqa: BLE001
                out["analysis_error"] = str(exc)
    return jsonify(out)


@app.route("/api/bridges/<bridge_id>/sensor-map-docx", methods=["POST"])
def api_bridge_sensor_map_docx_upload(bridge_id):
    """上传传感器测点编号表格 .docx 并重新生成传感器对照表。

    表单字段：
      file   测点编号表格 .docx（可含一座或多座桥）
      mode   full=完整覆盖（默认）/ merge=合并补充到现有对照表
    """
    auth = _require_token()
    if auth:
        return auth
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    name = _repair_filename(os.path.basename(f.filename))
    if not name.lower().endswith(".docx"):
        return jsonify({"error": "仅支持 .docx 测点编号表格"}), 400
    mode = str(request.form.get("mode") or "full").strip()
    if mode not in ("full", "merge"):
        mode = "full"

    inputs_dir = os.path.join(ROOT, "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    stem, ext = os.path.splitext(name)
    dest = os.path.join(inputs_dir, f"{stem}_{dt.datetime.now():%Y%m%d_%H%M%S}{ext}")
    f.save(dest)

    stats_dir = os.path.join(ROOT, "preprocess", "统计值_2026.1~3")
    if not os.path.isdir(stats_dir):
        stats_dir = os.path.join(ROOT, "preprocess", "统计值")
    out_map = os.path.join(ROOT, "preprocess", "传感器对照",
                           "传感器编号名称.json")
    cmd = [sys.executable,
           os.path.join(ROOT, "preprocess", "scripts", "parse_sensor_map.py"),
           dest, out_map, stats_dir]
    if mode == "merge":
        cmd.append("--merge")
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=_subprocess_env(),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"对照表生成异常: {exc}"}), 500
    if proc.returncode != 0:
        return jsonify({"error": "对照表生成失败",
                        "log": (proc.stdout or "") + (proc.stderr or "")}), 500

    # 更新预处理配置里的测点编号表格路径
    try:
        with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as fh:
            pcfg = json.load(fh)
        pcfg["sensor_map_docx"] = os.path.relpath(dest, ROOT).replace("\\", "/")
        with open(PREPROCESS_CONFIG, "w", encoding="utf-8") as fh:
            json.dump(pcfg, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({
        "ok": True,
        "mode": mode,
        "saved": os.path.relpath(dest, ROOT).replace("\\", "/"),
        "log": (proc.stdout or "")[-2000:],
        "sensor_map": os.path.relpath(out_map, ROOT).replace("\\", "/"),
    })


@app.route("/api/bridges/<bridge_id>/data")
def api_bridge_data_check(bridge_id):
    """校验数据存放路径是否可用，并返回统计值/图库概况。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    bd = cfg.get("bridge_data") or {}
    def _info(path):
        if not path or not os.path.isdir(path):
            return {"path": path, "ok": False}
        return {"path": path, "ok": True}
    stats = _info(bd.get("stats_dir", ""))
    charts = _info(bd.get("charts_dir", ""))
    if stats.get("ok"):
        stats["json_files"] = len([x for x in os.listdir(stats["path"]) if x.endswith(".json")])
    if charts.get("ok"):
        charts["sensor_dirs"] = len([x for x in os.listdir(charts["path"])
                                     if os.path.isdir(os.path.join(charts["path"], x))])
    return jsonify({
        "stats_dir": stats,
        "charts_dir": charts,
        "sensor_map": _info(bd.get("sensor_map", "")),
        "name_dict": _info(bd.get("name_dict", "")),
        "overview": _info(bd.get("overview", "")),
        "bridge_name": bd.get("bridge_name", ""),
    })


@app.route("/api/bridges/<bridge_id>/coverage")
def api_bridge_coverage(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    bcfg = cfg.get("bridge_data") or {}
    if not bcfg.get("enabled", False):
        return jsonify({"enabled": False, "message": "该桥未启用真实数据（bridge_data.enabled=false）"})
    from report_agent.bridge_source import BridgeData
    bridge = BridgeData(bcfg, base_dir=os.path.dirname(cfg.get("_config_path", ROOT)))
    bridge.load()
    return jsonify(bridge.coverage())


@app.route("/api/bridges/<bridge_id>/analysis")
def api_bridge_analysis(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    analysis = cfg.get("_chart_texts")
    out = {
        "chart_texts_count": len(analysis) if analysis else 0,
        "data_values_count": len(cfg.get("_data_values", {})),
    }
    # 模板占位符统计
    try:
        from report_agent.template_analyzer import analyze_template
        result = analyze_template(cfg.get("template", ""))
        placeholders = result.get("placeholders", [])
        from collections import Counter
        out["placeholder_total"] = len(placeholders)
        def _p_type(p: Dict) -> str:
            t = p.get("type", "unknown")
            if t != "unknown":
                return t
            key = str(p.get("key", ""))
            if key.startswith("cell."):
                return "cell"
            if key.startswith("data."):
                return "data"
            return t
        out["placeholder_by_type"] = dict(Counter(_p_type(p) for p in placeholders))
    except Exception as exc:  # noqa: BLE001
        out["template_error"] = str(exc)
    return jsonify(out)


@app.route("/api/bridges/<bridge_id>/reports")
def api_bridge_reports(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    out_dir = cfg.get("output_dir", "")
    if not out_dir or not os.path.isdir(out_dir):
        return jsonify({"reports": []})
    reports = []
    for f in os.listdir(out_dir):
        if f.lower().endswith(".docx") and not f.startswith("~$"):
            p = os.path.join(out_dir, f)
            reports.append({
                "name": f,
                "size": os.path.getsize(p),
                "mtime": dt.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds"),
            })
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return jsonify({"reports": reports, "output_dir": out_dir})


@app.route("/api/bridges/<bridge_id>/reports/<path:filename>")
def api_bridge_report_download(bridge_id, filename):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    out_dir = cfg.get("output_dir", "")
    safe = os.path.basename(filename)
    path = os.path.join(out_dir, safe)
    if not os.path.isfile(path) or not safe.lower().endswith(".docx"):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/api/bridges/<bridge_id>/charts/<path:filename>")
def api_bridge_chart(bridge_id, filename):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    charts_dir = (cfg.get("charts") or {}).get("output_dir", "")
    safe = os.path.basename(filename)
    for base in (charts_dir, os.path.join(ROOT, "outputs", "charts")):
        path = os.path.join(base, safe)
        if os.path.isfile(path):
            return send_file(path)
    return jsonify({"error": "图片不存在"}), 404


def _run_pipeline(period: Dict, charts_dir: str, stats_dir: str,
                  st: Dict) -> int:
    """调用 pipeline.py 完成 秒级->日级->图库/统计值->对照表。
    返回子进程退出码。"""
    pcfg = {}
    if os.path.isfile(PREPROCESS_CONFIG):
        try:
            with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as f:
                pcfg = json.load(f)
        except Exception:  # noqa: BLE001
            pcfg = {}
    raw = pcfg.get("raw_data_dir", "")
    daily = pcfg.get("daily_dir", os.path.join(PREPROCESS_DIR, "日级数据"))
    map_docx = pcfg.get("sensor_map_docx", "")
    cmd = [sys.executable, os.path.join(PREPROCESS_DIR, "pipeline.py"),
           "--raw", raw, "--daily", daily,
           "--charts", charts_dir, "--stats", stats_dir,
           "--start", period["start"], "--end", period["end"]]
    if map_docx:
        cmd += ["--sensor-map-docx", map_docx]
    st["pipeline_cmd"] = " ".join(cmd)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=_subprocess_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    st["pipeline_pid"] = proc.pid
    try:
        out, _ = proc.communicate(timeout=7200)
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        st["pipeline_error"] = str(exc)
        return 1
    st["pipeline_log_tail"] = out.decode("utf-8", errors="replace")[-4000:]
    return proc.returncode


def _update_bridge_data_dirs(bridge_id: str, stats_dir: str,
                             charts_dir: str) -> None:
    """把桥配置的 bridge_data 路径切到季度目录。"""
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return
    try:
        from report_agent.config import load_config
        cfg = load_config(cfg_path)
    except Exception:  # noqa: BLE001
        return
    bd = cfg.setdefault("bridge_data", {})
    bd["stats_dir"] = stats_dir
    bd["charts_dir"] = charts_dir
    # 传感器对照表是固定产物，统一放 preprocess/传感器对照/，不随季度变化
    map_dir = os.path.join(PREPROCESS_DIR, "传感器对照")
    bd["sensor_map"] = os.path.join(map_dir, "传感器编号名称.json")
    bd["overview"] = os.path.join(stats_dir, "总览.json")
    bridge = bd.get("bridge_name", "") or ""
    from report_agent.config import name_dict_candidates
    nd_dir = os.path.join(map_dir, "传感器名称对照")
    bd["name_dict"] = ""
    for fn in name_dict_candidates(bridge):
        cand = os.path.join(nd_dir, fn)
        if os.path.isfile(cand):
            bd["name_dict"] = cand
            break
    if not bd["name_dict"]:
        bd["name_dict"] = os.path.join(nd_dir,
                                       f"{bridge}大桥.json")
    _save_config(cfg, cfg_path)


@app.route("/api/bridges/<bridge_id>/period")
def api_bridge_period(bridge_id):
    """按模式/日期计算周期，返回季度目录及数据是否就绪。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    mode = request.args.get("mode", "quarterly")
    date = request.args.get("date", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if start and end:
        period = {"start": start, "end": end, "label": _label_from_range(start, end)}
    else:
        period = _period_from_mode(mode, date)
    charts_dir, stats_dir = _quarter_dirs(cfg, period["label"])
    return jsonify({
        **period,
        "charts_dir": charts_dir,
        "stats_dir": stats_dir,
        "charts_exists": _dir_nonempty(charts_dir),
        "stats_exists": _dir_nonempty(stats_dir),
        "data_ready": _dir_nonempty(charts_dir) and _dir_nonempty(stats_dir),
    })


@app.route("/api/bridges/<bridge_id>/run", methods=["POST"])
def api_bridge_run(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    b = get_bridge(bridge_id, REGISTRY)
    cfg_path = resolve_bridge_config(bridge_id, REGISTRY)
    if not b or not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404

    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or "quarterly"
    if mode not in ("weekly", "monthly", "quarterly", "yearly", "manual"):
        return jsonify({"error": f"无效模式: {mode}"}), 400
    date = str(data.get("date") or "").strip()
    engine = str(data.get("engine") or "").strip() or None
    start = str(data.get("start") or "").strip()
    end = str(data.get("end") or "").strip()
    auto_preprocess = bool(data.get("auto_preprocess"))
    template = str(data.get("template") or "").strip() or None

    if start and end:
        period = {"start": start, "end": end,
                  "label": _label_from_range(start, end)}
    else:
        period = _period_from_mode(mode, date)
        start, end = period["start"], period["end"]

    with _run_lock:
        st = _running.get(bridge_id)
        if st and st.get("running"):
            return jsonify({"error": "该桥已有任务正在运行", "started_at": st.get("started_at")}), 409
        st = {"running": True, "started_at": dt.datetime.now().isoformat(timespec="seconds")}
        _running[bridge_id] = st

    def _worker():
        try:
            st["period"] = period
            cfg = _config_for(bridge_id)
            charts_dir, stats_dir = _quarter_dirs(cfg, period["label"])
            data_ready = _dir_nonempty(charts_dir) and _dir_nonempty(stats_dir)
            st["charts_dir"] = charts_dir
            st["stats_dir"] = stats_dir
            st["data_ready"] = data_ready
            if auto_preprocess:
                if not data_ready:
                    st["preprocess"] = "running"
                    rc = _run_pipeline(period, charts_dir, stats_dir, st)
                    st["preprocess"] = "done" if rc == 0 else "failed"
                    if rc != 0:
                        st["error"] = ("数据预处理失败，详见 pipeline 日志。"
                                       if not st.get("pipeline_error")
                                       else st["pipeline_error"])
                        return
                else:
                    st["preprocess"] = "skipped"
                _update_bridge_data_dirs(bridge_id, stats_dir, charts_dir)
            else:
                st["preprocess"] = "manual"

            cmd = [sys.executable, os.path.join(ROOT, "run_agent.py"),
                   "--config", cfg_path, "--mode", mode]
            if date:
                cmd += ["--date", date]
            if engine:
                cmd += ["--engine", engine]
            if template:
                tpl_path = (template if os.path.isabs(template)
                            else os.path.join(ROOT, template))
                cmd += ["--template", tpl_path]
                st["template"] = template
            st["cmd"] = " ".join(cmd)
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=_subprocess_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            st["pid"] = proc.pid
            out, _ = proc.communicate(timeout=3600)
            st["returncode"] = proc.returncode
            st["log_tail"] = out.decode("utf-8", errors="replace")[-4000:]
            st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            log.info("桥梁 %s 报告生成完成，返回码 %s", bridge_id, proc.returncode)
        except Exception as exc:  # noqa: BLE001
            st["error"] = str(exc)
            st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            log.exception("桥梁 %s 报告生成异常", bridge_id)
        finally:
            st["running"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "bridge_id": bridge_id,
                    "mode": mode, "started_at": st["started_at"]})


@app.route("/api/bridges/<bridge_id>/scheduler")
def api_bridge_scheduler(bridge_id):
    """调度器状态 + 当前配置。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    st = _schedulers.get(bridge_id, {})
    return jsonify({
        "running": bool(st.get("proc") and st.get("proc").poll() is None),
        "pid": st.get("proc").pid if st.get("proc") and st.get("proc").poll() is None else None,
        "started_at": st.get("started_at"),
        "schedule": cfg.get("schedule", {}),
        "mode_text": {
            "weekly": "每周", "monthly": "每月", "quarterly": "每季度", "yearly": "每年",
        }.get((cfg.get("schedule") or {}).get("mode", ""), "未配置"),
    })


@app.route("/api/bridges/<bridge_id>/scheduler/start", methods=["POST"])
def api_bridge_scheduler_start(bridge_id):
    """启动常驻调度器（serve_scheduler.py --bridge <id>）。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    st = _schedulers.get(bridge_id)
    if st and st.get("proc") and st["proc"].poll() is None:
        return jsonify({"error": "调度器已在运行", "pid": st["proc"].pid}), 409
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "serve_scheduler.py"), "--bridge", bridge_id],
        cwd=ROOT, env=_subprocess_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _schedulers[bridge_id] = {"proc": proc, "started_at": dt.datetime.now().isoformat(timespec="seconds")}
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/bridges/<bridge_id>/scheduler/stop", methods=["POST"])
def api_bridge_scheduler_stop(bridge_id):
    """停止常驻调度器。"""
    auth = _require_token()
    if auth:
        return auth
    st = _schedulers.get(bridge_id)
    if not st or not st.get("proc") or st["proc"].poll() is not None:
        return jsonify({"error": "调度器未在运行"}), 404
    try:
        st["proc"].terminate()
        st["proc"].wait(timeout=10)
    except Exception:  # noqa: BLE001
        st["proc"].kill()
    return jsonify({"ok": True, "stopped_pid": st["proc"].pid})


@app.route("/api/bridges/<bridge_id>/run/status")
def api_bridge_run_status(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    st = _running.get(bridge_id, {})
    cfg = _config_for(bridge_id)
    last_run = None
    if cfg and "error" not in cfg:
        p = os.path.join(cfg.get("output_dir", ""), "last_run.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    last_run = json.load(f)
            except Exception:  # noqa: BLE001
                pass
    return jsonify({"running": st, "last_run": last_run})


@app.route("/api/bridges/<bridge_id>/log")
def api_bridge_log(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    name = request.args.get("name", "agent")
    lines = min(int(request.args.get("lines", 300)), 5000)
    candidates = {
        "agent": os.path.join(ROOT, "outputs", "logs", "agent.log"),
        "scheduler": os.path.join(ROOT, "outputs", "logs", "scheduler.log"),
        "web": os.path.join(ROOT, "outputs", "logs", "web.log"),
        "run": os.path.join(ROOT, "outputs", "logs", "web_run.log"),
    }
    path = candidates.get(name)
    if not path or not os.path.isfile(path):
        return jsonify({"log": "", "path": path or ""})
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-lines:]
    return jsonify({"log": "".join(tail), "path": path, "lines": len(tail)})


@app.route("/api/preprocess/config")
def api_preprocess_config():
    """读取数据处理管道配置（秒级/日级/图库/统计值路径）。"""
    auth = _require_token()
    if auth:
        return auth
    if not os.path.isfile(PREPROCESS_CONFIG):
        return jsonify({"error": "未找到 preprocess/config.json"}), 404
    with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return jsonify(cfg)


@app.route("/api/preprocess/config", methods=["POST"])
def api_preprocess_config_save():
    """保存数据处理管道配置。

    秒级数据目录 / 日级数据目录 / 桥名 为必填；
    图库/统计值目录等可留空（按桥名自动匹配并更新 config/config_<桥>.json
    的 bridge_data 路径；数据还没生成时留空，pipeline 跑完后自动写回）。
    """
    auth = _require_token()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    raw = str(data.get("raw_data_dir") or "").strip()
    daily = str(data.get("daily_dir") or "").strip()
    bridge_name = str(data.get("bridge_name") or "").strip()
    if not raw or not daily or not bridge_name:
        return jsonify({
            "error": "秒级数据目录、日级数据目录、桥名为必填项"} ), 400
    cfg = {}
    if os.path.isfile(PREPROCESS_CONFIG):
        with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    for k in ("raw_data_dir", "daily_dir", "charts_dir", "stats_dir",
              "sensor_map_docx", "bridge_name"):
        if k in data and data[k] is not None:
            cfg[k] = str(data[k]).strip()
    os.makedirs(PREPROCESS_DIR, exist_ok=True)
    with open(PREPROCESS_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 按桥名自动更新 config/config_<桥>.json 的 bridge_data 路径
    from report_agent.config import bridge_dir_match
    cfg_dir = os.path.join(ROOT, "config")
    bridge_cfg_path, canon_name = None, bridge_name
    assets = {}
    if os.path.isdir(cfg_dir):
        for fn in sorted(os.listdir(cfg_dir)):
            if not (fn.startswith("config_") and fn.endswith(".json")):
                continue
            p = os.path.join(cfg_dir, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    bname = ((json.load(f).get("bridge_data") or {})
                             .get("bridge_name", "") or "")
            except Exception:
                continue
            if bname and bridge_dir_match(bridge_name, bname):
                bridge_cfg_path, canon_name = p, bname
                break
    if bridge_cfg_path:
        try:
            from setup_bridge import find_bridge_assets
            assets = find_bridge_assets(canon_name)
            with open(bridge_cfg_path, encoding="utf-8") as f:
                bcfg = json.load(f)
            bd = bcfg.setdefault("bridge_data", {})
            bd["enabled"] = True
            bd["bridge_name"] = canon_name
            if assets.get("stats_dir"):
                bd["stats_dir"] = assets["stats_dir"]
            if assets.get("charts_dir"):
                bd["charts_dir"] = assets["charts_dir"]
            if assets.get("sensor_map"):
                bd["sensor_map"] = assets["sensor_map"]
            if assets.get("name_dict"):
                bd["name_dict"] = assets["name_dict"]
            if assets.get("stats_dir"):
                bd["overview"] = os.path.join(
                    assets["stats_dir"], "总览.json")
            _save_config(bcfg, bridge_cfg_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": True, "config": cfg,
                            "bridge_config_error": str(exc)})

    return jsonify({
        "ok": True,
        "config": cfg,
        "bridge_config": (os.path.relpath(bridge_cfg_path, ROOT)
                          if bridge_cfg_path else None),
        "bridge_name": canon_name,
        "auto_paths": assets,
    })


@app.route("/api/preprocess/run", methods=["POST"])
def api_preprocess_run():
    """启动数据处理管道（后台执行）。"""
    auth = _require_token()
    if auth:
        return auth
    st = _preprocess.get("proc")
    if st and st.poll() is None:
        return jsonify({"error": "数据处理管道已在运行", "pid": st.pid}), 409
    data = request.get_json(silent=True) or {}
    cmd = [sys.executable, os.path.join(PREPROCESS_DIR, "pipeline.py")]
    flags = {
        "raw": data.get("raw_data_dir"), "daily": data.get("daily_dir"),
        "charts": data.get("charts_dir"), "stats": data.get("stats_dir"),
        "sensor_map_docx": data.get("sensor_map_docx"),
    }
    for k, v in flags.items():
        if v:
            cmd += [f"--{k}", str(v)]
    # 时间范围（只处理该时间段数据）
    start = str(data.get("start") or "").strip()
    end = str(data.get("end") or "").strip()
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    if data.get("skip_preprocess"):
        cmd.append("--skip-preprocess")
    if data.get("skip_charts"):
        cmd.append("--skip-charts")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=_subprocess_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _preprocess["proc"] = proc
    _preprocess["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/preprocess/status")
def api_preprocess_status():
    """数据处理管道状态 + 日志尾部。"""
    auth = _require_token()
    if auth:
        return auth
    st = _preprocess.get("proc")
    running = bool(st and st.poll() is None)
    status = {}
    if os.path.isfile(PREPROCESS_STATUS):
        with open(PREPROCESS_STATUS, "r", encoding="utf-8") as f:
            status = json.load(f)
    tail = ""
    if os.path.isfile(PREPROCESS_LOG):
        with open(PREPROCESS_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-120:])
    return jsonify({
        "running": running,
        "pid": st.pid if st and running else None,
        "status": status,
        "log_tail": tail,
    })


@app.route("/api/hub/bridges")
def api_hub_bridges():
    auth = _require_token()
    if auth:
        return auth
    if WEB_MODE != "hub":
        return jsonify({"error": "当前不是 hub 模式（设置 REPORT_WEB_MODE=hub）"}), 400
    import requests
    out = []
    for b in list_bridges(REGISTRY):
        host = b.get("host", "")
        port = b.get("port", 8080)
        token = os.environ.get(b.get("token_env", ""), "") or WEB_TOKEN
        if not host:
            out.append({
                "id": b.get("id"),
                "name": b.get("name"),
                "url": "",
                "reachable": False,
                "error": "注册表中未配置 host",
            })
            continue
        url = f"http://{host}:{port}/api/status"
        item = {"id": b.get("id"), "name": b.get("name"), "url": url, "reachable": False}
        try:
            headers = {"X-Auth-Token": token} if token else {}
            r = requests.get(url, headers=headers, timeout=5)
            item["reachable"] = r.status_code == 200
            if r.ok:
                item["remote"] = r.json()
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)
        out.append(item)
    return jsonify({"bridges": out})


def main():
    host = os.environ.get("REPORT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("REPORT_WEB_PORT", "8080"))
    log.info("报告智能体 Web 管理台启动: http://%s:%s  mode=%s  auth=%s",
             host, port, WEB_MODE, "on" if WEB_TOKEN else "off")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
