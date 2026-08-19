# Windows 小白版部署说明

照着复制粘贴就能跑。全部操作都在**同一台 Windows 服务器**上完成，
不需要 Docker、不需要 Linux、不需要中心服务器。

> 假设你要部署“洣水河特大桥”，以下把桥名统一写成 `mishuihe`；
> 换成其他桥就把这个 ID 换成 `chishi` / `xiangjiang` / `aizhai` / `dongtinghu` 等
> （以 `bridges/registry.json` 里的 `id` 为准）。

---

## 第 0 步：准备 3 样东西

1. 项目代码文件夹（本仓库）；
2. 本桥的原始数据（放服务器某个目录，例如 `E:\信科采集软件解析数据`）；
3. conda（用来创建 Python 环境，装一次以后都不用管）。

---

## 第 1 步：把代码复制到服务器

把整个项目文件夹复制到服务器，例如：

```
D:\bridge_agent_scripts\
```

里面有这些关键东西（别删）：

```
bridge_agent_scripts/
├── run_agent.py              # 生成报告的命令
├── serve_scheduler.py        # 常驻调度器（自动出报告）
├── web/                      # Web 管理台
├── report_agent/             # 核心代码
├── preprocess/               # 数据处理管道
├── config/                   # 各桥配置文件
├── templates/                # 报告模板
├── bridges/registry.json     # 桥梁注册表
├── start_web.bat             # 启动 Web（双击）
├── start_scheduler.bat       # 启动调度器（双击/命令行）
├── install_auto_start.bat    # 注册开机自启（管理员运行一次）
└── requirements.txt
```

---

## 第 2 步：创建 Python 环境（一次）

打开命令行（Win+R 输入 `cmd` 回车），执行：

```bat
conda create -n bridge python=3.11 -y
conda activate bridge
cd /d D:\bridge_agent_scripts
pip install -r requirements.txt
```

看到 `Successfully installed ...` 就说明环境装好了。

---

## 第 3 步：改本桥配置

### 3.1 检查注册表

打开 `bridges\registry.json`，确认本桥条目存在且 `config` 指向
`config/config_<桥>.json`，`port` 是 `8456`：

```json
{
  "id": "mishuihe",
  "name": "洣水河特大桥",
  "config": "config/config_mishuihe.json",
  "host": "218.75.217.159",
  "port": 8456,
  "token_env": "BRIDGE_MISHUIHE_TOKEN"
}
```

`host` 填这台服务器的 IP（无所谓内外网，只作记录用）。

### 3.2 检查桥配置

打开 `config\config_mishuihe.json`，确认：

- `template` 指向 `templates/` 下已有的模板（`.docx`）；
- `bridge_data.bridge_name` 是桥名；
- `bridge_data.stats_dir` / `charts_dir` 指向本机预处理产物目录；
- `schedule` 里有季度调度和起始日期：

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

`start_date` 的意思是“从这一天开始算调度”：调度器启动时会把
**从这天到今天的已结束季度**全部补跑一遍，之后每季度过完自动生成下一份。
（例如现在 8 月、`start_date=2026-01-01`，启动后会自动生成第 1、2 季度报告，
再等 10/1 生成第 3 季度。）

---

## 第 4 步：先手动生成一次，验证能跑通

```bat
python run_agent.py --bridge mishuihe --mode quarterly --year 2026 --quarter 1
```

如果第 1 季度还没过完会直接报错（这是正常的保护）。跑通后在 `outputs\reports\`
能看到类似 `洣水河特大桥2026.1~3.docx` 的文件。

---

## 第 5 步：启动 Web 管理台（浏览器页面）

双击 `start_web.bat`，或者命令行执行：

```bat
start_web.bat
```

浏览器打开 `http://127.0.0.1:8456`（服务器本机），
或 `http://服务器IP:8456`（其他电脑，见“网络访问”一节）。

首次打开如果提示输入令牌，令牌在 `start_web.bat` 里的 `REPORT_WEB_TOKEN`。

---

## 第 6 步：启动季度调度器（自动出报告）

命令行执行：

```bat
start_scheduler.bat mishuihe
```

或者直接：

```bat
python serve_scheduler.py --bridge mishuihe
```

调度器会一直运行（窗口别关）。日志在 `outputs\logs\scheduler.log`。

---

## 第 7 步：注册开机自启（服务器重启也不用管）

**以管理员身份**双击 `install_auto_start.bat mishuihe`。

它会创建两个 Windows 计划任务：

- `BridgeReportWeb`：开机自动启动 Web 管理台；
- `BridgeReportScheduler`：开机自动启动调度器。

以后服务器重启，服务会自动起来，不需要人工操作。

---

## 网络访问：在其他电脑上打开

- 同一内网：浏览器输 `http://服务器内网IP:8456`；
- 不同内网（跨公网）：浏览器输 `http://服务器公网IP:8456`，
  前提是防火墙放行 8456（见下）。

放行防火墙（管理员命令行执行一次）：

```bat
netsh advfirewall firewall add rule name="ReportWeb" dir=in action=allow protocol=TCP localport=8456
```

如果服务器在阿里云/腾讯云等，还要到云控制台 → 安全组 → 入方向放行 TCP 8456。

> 想用域名 + HTTPS（可选、更安全）再看 [nginx入门.md](nginx入门.md)；
> 不装 nginx 用 IP + 端口也能正常工作。

---

## 常见问题

**Q：调度器窗口一关就停了？**
正常，关窗口 = 停服务。请按第 7 步注册开机自启，或保持窗口一直开着。

**Q：填了季度 3，报“尚未结束”？**
季度 3 要等 10/1 才算过完。要么等季度过完再生成，要么改成已结束的季度号。

**Q：端口被占用？**
`8456` 被占用的话，把 `start_web.bat` 里的端口改成别的（比如 8457），
同时同步改防火墙放行和浏览器访问地址。

**Q：Web 能打开但调度器没跑？**
看 `outputs\logs\scheduler.log` 最后几行；常见原因是配置文件里
`schedule.mode` 不是 `quarterly`，或 `--bridge` 的 ID 写错。
