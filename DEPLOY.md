# 多服务器部署与 Web 管理台

本方案把“数据分析报告智能体”部署成两级架构：

```
                        中心服务器 222.242.152.65
                        ┌─────────────────────────────┐
                        │  Web 管理台（hub 模式）      │
                        │  六桥状态汇总 / 一键跳转      │
                        │  赤石大桥实例（数据在本地）    │
                        └──────────────┬──────────────┘
              HTTPS + 令牌             │  HTTPS + 令牌
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
   赤石大桥服务器   洞庭湖大桥服务器  洣水河特大桥服务器  湘江特大桥服务器  矮寨大桥服务器
   （222.242.152.65）  （各桥独立服务器） ... 
   原始数据 + 预处理产物 + 报告智能体 + Web(bridge)   每台服务器同构
```

设计原则：**数据留在各桥服务器上**，中心服务器只做状态汇总和跳转，不传输原始数据。

---

## 一、每台桥服务器上的组成

| 组件 | 说明 |
|---|---|
| 原始数据 | 各传感器的按日/小时原始文件 |
| 桥数据预处理产物 | `统计值_<期>/<桥名>/*.json`、`图库_<期>/<桥名>/*.png`、`传感器对照/*.json`、`总览.json` |
| 报告智能体 | `run_agent.py` / `serve_scheduler.py` / `report_agent/` |
| Web 管理台 | `web/app.py`（bridge 模式，默认 8080；含数据路径配置、模板上传、调度器控制、季度/年度周期） |
| 调度器 | 按 `config_<桥>.json → schedule` 自动出报告 |

每台桥服务器的目录建议：

```
/opt/report-agent/                # 本仓库代码
/data/bridge/                     # 桥数据预处理产物
    ├── 统计值/
    ├── 图库/
    ├── summary.csv
    └── inventory.csv
/data/raw/                        # 原始监测数据（预处理输入）
```

---

## 二、首次部署步骤（Linux / systemd，推荐）

### 1. 安装代码与依赖

```bash
cd /opt
git clone <你的仓库地址> report-agent   # 或 scp/rsync 同步
cd report-agent
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

> 若服务器不联网，可在有网的机器上 `pip download -r requirements.txt -d wheels/` 后拷贝离线安装。

### 2. 放置数据

```bash
mkdir -p /data/bridge /data/raw
# 把预处理产出的 统计值/图库 等拷入 /data/bridge
# 把原始数据放入 /data/raw（预处理脚本在每台桥服务器本地跑一次）
```

### 3. 创建本桥配置

复制 `config/config_chishi.json` 为 `config/config_<桥>.json`，修改：

```json
{
  "template": "templates/<桥>_template.docx",
  "source_report": "/data/raw/<桥>.docx",
  "bridge_data": {
    "enabled": true,
    "bridge_name": "赤石",
    "stats_dir": "/data/bridge/统计值",
    "charts_dir": "/data/bridge/图库",
    "sensor_map": "/data/bridge/传感器对照/传感器编号名称.json",
    "overview": "/data/bridge/统计值/总览.json",
    "name_dict": "/data/bridge/传感器对照/传感器名称对照/<桥名>.json",
    "metrics": { ... },
    "sensor_exclude": ["101", "106"],
    "sensor_aliases": {},
    "chart_map": {}
  },
  "schedule": { "mode": "quarterly", "day_of_month": 1, "hour": 8, "minute": 0 }
}
```

关键配置项：

- `bridge_data.metrics.<指标>.feature`：模板里的指标对应预处理的特征名（如 `WSD(temp)`）。特征名不确定时留空，系统会退化为“该传感器第一个特征”。
- `bridge_data.name_dict`：人工维护的名称→编号对照表（`传感器对照/传感器名称对照/<桥名>.json`，固定产物），精确命中优先于模糊匹配；未配置时系统自动按 `bridge_name` 查找。
- `bridge_data.sensor_exclude`：数据异常的传感器编号或名称，统计时排除。
- `bridge_data.chart_map`：图表占位符 → 传感器编号的人工映射，用于补齐图库自动匹配不到的那 20%。
- `bridge_data.sensor_aliases`：模板测点写法 → 传感器编号的精确映射（如 `"测点1": "100"`）。

### 4. 注册到桥梁注册表

编辑 `bridges/registry.json`，把本桥的 `host`、`port`、`config` 填对。

### 5. 启动 Web 管理台（bridge 模式）

```bash
export REPORT_WEB_TOKEN="$(openssl rand -hex 24)"
export REPORT_WEB_HOST=127.0.0.1
export REPORT_WEB_PORT=8080
export REPORT_WEB_MODE=bridge
export REPORT_PROJECT_ROOT=/opt/report-agent

cp deploy/systemd/report-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now report-web
```

网页访问：`http://<服务器IP>:8080`（防火墙放行 8080，或用 nginx 反代 80/443）。
页面功能与远程访问排查见 [deploy/web_usage.md](deploy/web_usage.md)。

### 6. 启动定时调度

```bash
cp deploy/systemd/report-agent.service /etc/systemd/system/
# 编辑 ExecStart 增加 --bridge <桥id>，例如：
# ExecStart=/opt/report-agent/.venv/bin/python serve_scheduler.py --bridge chishi
systemctl daemon-reload
systemctl enable --now report-agent
```

### 7. nginx + HTTPS（公网必须）

```bash
cp deploy/nginx/bridge.conf.example /etc/nginx/conf.d/bridge.conf
# 替换 server_name、证书路径
nginx -t && systemctl reload nginx
```

> 没有证书可先用自签证书，或内网用 HTTP + 令牌；严禁把 Web 直接绑定 0.0.0.0 裸奔。

---

## 三、中心服务器 222.242.152.65 部署（hub 模式）

中心服务器同时运行：

1. **赤石大桥实例**（因为数据就在这台服务器上）：按上面第二节步骤部署，`REPORT_WEB_MODE=bridge`，端口 8080。
2. **中心汇总页**：另起一个实例，`REPORT_WEB_MODE=hub`，端口 8081：

```bash
export REPORT_WEB_MODE=hub
export REPORT_WEB_TOKEN="中心令牌"
export REPORT_WEB_PORT=8081
# 为每座桥设置访问令牌环境变量（与各桥服务器上的令牌一致）
export BRIDGE_CHISHI_TOKEN="赤石服务器令牌"
export BRIDGE_DONGTINGHU_TOKEN="洞庭湖令牌"
...
python web/app.py
```

中心页会通过 `/api/hub/bridges` 依次请求各桥的 `/api/status`，在线状态一目了然；点“打开”进入对应桥的工作台。

---

## 四、Windows 服务器部署

```powershell
# 1. 安装 Python 3.10+ 并加入 PATH
# 2. 安装依赖
python -m pip install -r requirements.txt
# 3. 配置 config_<桥>.json、bridges/registry.json
# 4. 启动 Web（前台测试）
$env:REPORT_WEB_TOKEN="你的令牌"
$env:REPORT_WEB_MODE="bridge"
python web/app.py
# 5. 正式后台运行：任务计划程序 或 NSSM 注册为服务
# 6. 定时任务
.\deploy\install_windows_task.ps1 -Python (Get-Command python).Source -WorkDir D:\report-agent
```

---

## 五、安全要点

1. **访问令牌**：所有桥实例和中心实例都设置 `REPORT_WEB_TOKEN`，前端页面输入一次后保存在浏览器 localStorage。
2. **网络**：Web 默认只监听 `127.0.0.1`，公网一律走 nginx + HTTPS 反代；防火墙只放行 80/443。
3. **数据权限**：`/data` 目录仅报告服务账号可读写；调度器与 Web 用独立账号运行。
4. **备份**：定期备份 `bridges/registry.json`、各 `config_*.json`、模板和 `outputs/`；统计值与图库可从原始数据重新生成，不必备份。
5. **日志**：报告运行日志在 `outputs/agent.log`、调度日志 `outputs/scheduler.log`，Web 页面可直接查看。

> 图表统一使用 Python（matplotlib）出图，已移除 MATLAB 依赖；报告周期支持 季度 / 年度 / 月 / 周。

---

## 六、日常使用与升级

### 网页操作

- **总览**：六桥卡片，含配置状态、最近报告、运行状态。
- **生成报告**：选桥 → 模式（季度/月/周）→ 日期 → 引擎 → 开始；页面每 4 秒轮询进度。
- **下载报告**：列出该桥 `outputs/` 下的 .docx，一键下载。
- **数据覆盖**：每个指标有几台传感器、有数据的数量、最早/最晚日期——快速发现缺数据。
- **待补清单**：上次运行未匹配到图库的图表占位符清单，照着在 `chart_map` 里补映射即可。

### 升级

```bash
cd /opt/report-agent
git pull                     # 或重新 rsync
.venv/bin/pip install -r requirements.txt
systemctl restart report-web report-agent
```

### 常见问题

- **待补图表多**：优先补 `chart_map`（占位符→传感器编号）；再检查 `metrics.<指标>.feature` 是否填对。
- **某个测点始终取不到值**：在 `sensor_aliases` 里把模板写法和传感器编号精确绑定。
- **数据明显异常（如 9000 万）**：把传感器加入 `sensor_exclude`，同时到预处理侧排查该传感器原始数据。
- **报告期无数据**：`统计值/*.json` 的“每日统计”必须覆盖报告期；缺数据时对应单元格会显示 “—”，并在覆盖度页暴露。
- **目录页码没刷新**：生成时已写入 `<w:updateFields/>`，用 Word 打开时选择“更新目录”即可；如需服务端自动重排页码，可在服务器装 LibreOffice 后做一次宏转换（见升级注意事项）。

---

## 七、验证清单（每台桥服务器部署后）

```bash
# 1. 数据源可用
curl -H "X-Auth-Token: $REPORT_WEB_TOKEN" http://127.0.0.1:8080/api/status
curl -H "X-Auth-Token: $REPORT_WEB_TOKEN" http://127.0.0.1:8080/api/bridges
curl -H "X-Auth-Token: $REPORT_WEB_TOKEN" http://127.0.0.1:8080/api/bridges/chishi/coverage

# 2. 手动生成一次
python run_agent.py --bridge chishi --mode quarterly --date <报告期最后一天>

# 3. 检查输出
ls -l outputs/*.docx
python - <<'PY'
from docx import Document
doc = Document("outputs/<桥>2026.X~X.docx")
import re
leftover = [m.group(0) for p in doc.paragraphs for m in re.finditer(r"\{\{[^}]+\}\}", p.text)]
print("残留占位符:", leftover)
PY
```

部署完成后，把 `bridges/registry.json` 里六座桥的 `host/port/token_env` 填齐，中心页即可看到全貌。
