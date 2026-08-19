# Windows 独立服务器部署说明

## 架构：没有中心服务器

本项目**没有中心服务器、没有汇总节点**：每座桥各用一台自己的 Windows 服务器，
服务器上同时运行 Web 管理台、调度器和报告智能体，互相之间不通信、不汇总。

```
赤石大桥服务器        洞庭湖大桥服务器       洣水河特大桥服务器     湘江特大桥服务器     矮寨大桥服务器
┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ 原始数据(保密) │   │ 原始数据(保密) │   │ 原始数据(保密) │   │ 原始数据(保密) │   │ 原始数据(保密) │
│ 预处理产物     │   │ 预处理产物     │   │ 预处理产物     │   │ 预处理产物     │   │ 预处理产物     │
│ Web:8456      │   │ Web:8456      │   │ Web:8456      │   │ Web:8456      │   │ Web:8456      │
│ 调度器+智能体  │   │ 调度器+智能体  │   │ 调度器+智能体  │   │ 调度器+智能体  │   │ 调度器+智能体  │
└───────────────┘   └───────────────┘   └───────────────┘   └───────────────┘   └───────────────┘
```

因为监测数据需要保密，**原始数据只能存放在该桥自己的服务器上**，不跨服务器传输，
也不上传到任何公共平台。`bridges/registry.json` 只用来记录“每台服务器上部署了哪座桥”，
方便用 `--bridge` 快速切换，不会把你的数据发给别的服务器。

所有服务器都是 **Windows** 系统，本说明只讲 Windows；Docker、Linux systemd、
中心汇总页均不使用。

---

## 一、每台桥服务器上有什么

| 组件 | 说明 |
|---|---|
| 原始数据 | 各传感器按日/小时存放的原始文件（只在本机，不出服务器） |
| 桥数据预处理产物 | `统计值_<期>/<桥名>/*.json`、`图库_<期>/<桥名>/*.png`、`传感器对照/*.json`、`总览.json` |
| 报告智能体 | `run_agent.py` / `serve_scheduler.py` / `report_agent/` |
| Web 管理台 | `web/app.py`（默认端口 **8456**；数据路径配置、模板上传、调度器控制、季度/年度/月/周） |
| 调度器 | 按 `config/config_<桥>.json → schedule` 自动出报告 |

> 为什么用 8456 而不是 8080：8080 在很多服务器上被其他软件占用，
> 换成一个不常用的端口可以避免冲突。防火墙放行 8456 即可。

---

## 二、网络：电脑和服务器不在同一个内网，能访问吗？

**能。** Web 走的是普通 HTTP（IP + 端口），只要满足下面三件事，任何一台能上
互联网的电脑都能在浏览器打开 `http://服务器公网IP:8456`：

1. **服务器有公网 IP**（或路由器做了端口映射/运营商给了公网地址）；
2. **Windows 防火墙放行 8456 端口**；
3. **云服务器安全组放行 8456 端口**（如果是阿里云/腾讯云等，控制台里也要放行）。

打不开时按顺序排查：

| 排查点 | 做法 |
|---|---|
| 服务是否在运行 | 服务器上执行 `python web\app.py`，看到“Web 管理台启动”即成功 |
| 防火墙 | `netsh advfirewall firewall add rule name="ReportWeb" dir=in action=allow protocol=TCP localport=8456` |
| 云安全组 | 阿里云/腾讯云控制台 → 安全组 → 入方向放行 TCP 8456 |
| 路由器端口映射 | 服务器接在路由器后面时，把公网 8456 映射到内网服务器 IP 的 8456 |
| 运营商封禁 | 部分宽带封 80/443 等常用端口，8456 这类高位端口一般可用 |

安全提醒：跨公网访问时**务必设置访问令牌**（`REPORT_WEB_TOKEN`），
否则任何知道 IP 的人都能打开你的管理台。更稳妥的做法是加一层 nginx 反代 +
HTTPS（见 [docs/nginx入门.md](docs/nginx入门.md)），那不是必须的，先用 IP+端口
也能正常用。

---

## 三、首次部署（Windows 小白版）

完整一步步的操作见 [docs/部署说明_Windows小白版.md](docs/部署说明_Windows小白版.md)，
这里给要点：

### 1. 复制代码到服务器

把整个项目文件夹（含 `web/`、`report_agent/`、`preprocess/`、`config/`、
`templates/`、`bridges/registry.json`、`requirements.txt` 等）复制到服务器，
例如 `D:\bridge_agent_scripts`。

### 2. 准备 Python 环境

服务器装好 conda（已装则跳过），然后：

```bat
conda create -n bridge python=3.11 -y
conda activate bridge
cd /d D:\bridge_agent_scripts
pip install -r requirements.txt
```

### 3. 配置本桥

把 `config/` 下对应桥的 json 改好（模板路径、统计值/图库路径、`bridge_name`），
并确认 `bridges/registry.json` 里本桥的 `host`（本服务器 IP）、`port: 8456`、
`config` 路径正确。

### 4. 启动 Web 管理台

```bat
set REPORT_WEB_TOKEN=你的令牌
python web\app.py
```

浏览器打开 `http://127.0.0.1:8456` 或 `http://服务器IP:8456`。

### 5. 启动季度调度器

```bat
python serve_scheduler.py --bridge mishuihe
```

调度器会常驻运行，季度过完自动生成报告（详见下一节）。

### 6. 开机自启（可选，推荐）

运行 `install_auto_start.bat`（以管理员身份），把 Web 和调度器注册成开机自启的
计划任务，服务器重启后不需要人工再启动。

---

## 四、季度调度器：过完才生成 + 起始日期补跑

调度配置在 `config/config_<桥>.json → schedule`：

```json
{
  "schedule": {
    "mode": "quarterly",
    "start_date": "2026-01-01",
    "day_of_month": 1,
    "hour": 8,
    "minute": 0
  }
}
```

规则：

1. **季度过完才生成**：调度器在每季度首月的 `day_of_month` 日（默认 1 日）触发，
   生成**刚结束的上一季度**。例如 4/1 生成 1~3 月、7/1 生成 4~6 月、
   10/1 生成 7~9 月、1/1 生成去年 10~12 月。
2. **start_date 起始日期**（仅季度模式生效）：调度器启动时，先自动补跑
   “从 `start_date` 到今天所有已完整结束的季度”，然后进入周期等待。

举两个例子：

- 今天是 2026-08-19（第 3 季度中），`start_date` 留空或填今天：
  当前季度还没过完，调度器什么都不跑，等 2026-10-01 生成第 3 季度报告。
- `start_date` 填 `2026-01-01`，8 月启动：
  调度器先自动补跑第 1、2 季度报告（1~3 月、4~6 月），再等 10/1 生成第 3 季度。

> 手动生成季度报告也一样只填季度号，不填日期：
> `python run_agent.py --bridge mishuihe --mode quarterly --year 2026 --quarter 1`
> 如果填的季度还没过完（比如现在 8 月却填 `--quarter 3`），程序会明确报错。

---

## 五、安全要点

1. **访问令牌**：每台服务器设置 `REPORT_WEB_TOKEN`，页面首次打开输入一次。
2. **网络**：Web 默认只监听 `127.0.0.1`；跨公网访问时，要么直接放行 8456 +
   令牌鉴权，要么用 nginx 反代 + HTTPS（见 [docs/nginx入门.md](docs/nginx入门.md)）。
3. **数据权限**：原始数据目录只允许运行服务的账号读写；原始数据绝不复制到
   非本桥服务器。
4. **备份**：定期备份 `bridges/registry.json`、`config/*.json`、模板和 `outputs/`；
   统计值与图库可从原始数据重新生成，不必备份。
5. **日志**：报告运行日志 `outputs/agent.log`、调度日志 `outputs/scheduler.log`，
   Web 页面可直接查看。

---

## 六、日常使用与升级

### 网页操作

- **总览**：本桥卡片，含配置状态、最近报告、运行状态。
- **生成报告**：选模式（**季度** / 年度 / 月 / 周 / 手动）→ 季度模式下选
  **季度号 1~4 + 年份**，不需要填日期 → 开始生成。
- **下载报告**：列出本桥 `outputs/` 下的 .docx，一键下载。
- **调度器**：设置周期与 `起始日期`，一键启动/停止常驻调度器。
- **日志**：agent / scheduler / web 日志尾部。

### 升级

```bat
cd /d D:\bridge_agent_scripts
git pull                     // 或重新复制覆盖代码（不要覆盖 config/ 和 outputs/）
pip install -r requirements.txt
// 重新启动 web 和调度器即可
```

### 常见问题

- **待补图表多**：优先补 `bridge_data.chart_map`（占位符→传感器编号）；
  再检查 `metrics.<指标>.feature` 是否填对。
- **某个测点始终取不到值**：在 `sensor_aliases` 里把模板写法和传感器编号精确绑定。
- **数据明显异常（如 9000 万）**：把传感器加入 `sensor_exclude`，并到预处理侧排查
  该传感器原始数据。
- **报告期无数据**：`统计值/*.json` 必须覆盖报告期；缺数据时对应单元格显示 “—”。
- **目录页码没刷新**：生成时已写入更新域标记，用 Word 打开时选择“更新目录”即可。

---

## 七、部署后验证清单

```bat
// 1. 服务可访问（本机）
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8456/api/status').read().decode())"

// 2. 手动生成一次季度报告（第 1 季度，2026 年）
python run_agent.py --bridge mishuihe --mode quarterly --year 2026 --quarter 1

// 3. 检查输出
dir outputs\reports\*.docx
```

部署完成后，`bridges/registry.json` 里本桥的 `host/port/token_env` 填对即可；
每台服务器只关心自己那座桥，不需要也无法汇总其他桥。
