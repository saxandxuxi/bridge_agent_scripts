# 数据分析报告智能体

一个“喂模板、吐报告”的自动化智能体：给它一份 Word 报告模板，它会自动识别模板里需要替换的数据（正文数字、统计表格、图表），从数据文件中计算统计量（最大值、最小值、平均值等），调用 Python（matplotlib）生成折线图/直方图/柱状图/箱线图，最后把数据、图表填回模板，输出一份完整的 Word 报告。可以部署到 Windows 服务器上，按季度/年度/月/周定时自动出报告。

## 新增能力（2026-08）

- **真实监测数据接入**：`report_agent/bridge_source.py` 直接读取“桥数据预处理”产出的 `统计值/*.json` 与 `图库/*.png`，无需 CSV 虚拟数据；测点名称自动匹配传感器编号，支持别名、模糊匹配、异常传感器排除。
- **Web 管理台**：`web/app.py`（Flask）提供本桥总览、一键生成、报告下载、数据覆盖度、待补图表清单、日志查看。**没有中心服务器**：每座桥独立部署在自己服务器的 Windows 上，原始数据不出本机。
- **多桥注册表**：`bridges/registry.json` 管理各桥（`run_agent.py --bridge <id>` / `serve_scheduler.py --bridge <id>` 一键切换）。
- **部署文档**：`DEPLOY.md` 给出 Windows 独立服务器部署、防火墙/远程访问、调度器说明与令牌鉴权。

```
┌────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐   ┌──────────────┐
│ 数据文件    │ → │ 数据加载/过滤 │ → │ 统计计算      │ → │ Python 出图   │ → │ 填充 Word 模板│
│ (CSV)      │   │ (按季/年/月/周)│   │ (最大/最小/均值)│   │ (折线/直方图) │   │ (文本/表格/图)│
└────────────┘   └──────────────┘   └───────────────┘   └──────────────┘   └──────────────┘
                                                              ↑
                                       定时调度（季度/年度/月/周，Windows 计划任务自启）
```

## 目录结构

```
data_analysis/
├── run_agent.py                  # 智能体命令行入口（生成报告 / 识别模板）
├── analyze_report.py             # 成品报告解析识别（DOCX/PDF）
├── serve_scheduler.py            # 常驻定时调度服务（每周/每月自动出报告）
├── bridges/registry.json         # 六座桥注册表（服务器/配置/令牌环境变量）
├── web/                          # Flask Web 管理台（后端 + 静态前端）
├── DEPLOY.md                     # Windows 独立服务器部署文档
├── config/                       # 各桥智能体配置（config.json / config_<桥>.json）
├── requirements.txt              # Python 依赖
├── report_agent/                 # 核心代码
│   ├── agent.py                  # 主流程编排
│   ├── bridge_source.py          # 真实监测数据适配器（统计值 JSON + 图库）
│   ├── bridges.py                # 多桥注册表解析
│   ├── data_loader.py            # CSV 数据读取与时间过滤
│   ├── stats.py                  # 统计计算
│   ├── template_analyzer.py      # 模板识别（找出需要更换的数据）
│   ├── recognizer.py             # 成品报告解析：图片/数字 动态 vs 固定 分类
│   ├── chart_generator.py        # Python 出图（matplotlib）
│   ├── report_builder.py         # Word 填充（文本/表格/图表）
│   └── scheduler.py              # 定时调度逻辑
├── scripts/
│   ├── build_template.py         # 生成示例报告模板
│   └── make_sample_data.py       # 生成示例数据
│   ├── make_sample_bridge_report.py  # 生成“桥梁监测报告”DOCX 样例（识别测试用）
│   └── make_sample_pdf.py        # 生成“桥梁监测报告”PDF 样例（识别测试用）
├── templates/                    # 报告模板（.docx）
├── data/                         # 数据文件（CSV）
└── outputs/                      # 生成的报告与图表
```

## 快速开始（本机）

1. 准备环境（Python 3.10+）：

```bash
python -m pip install -r requirements.txt
```

2. 生成示例数据和模板：

```bash
python scripts/make_sample_data.py
python scripts/build_template.py
```

3. 让智能体“识别”模板里需要更换的数据：

```bash
python run_agent.py --inspect-template
```

它会列出模板中所有的统计占位符、图表位置、表格填充项，以及正文中“疑似需要动态替换的数字”，并把结果保存到 `outputs/template_analysis.json`。

4. 生成一份周报：

```bash
python run_agent.py --mode weekly
```

生成的文件在 `outputs/` 目录下，例如 `温度分析报告_20260729_20260804.docx`，同目录还有 4 张图表 PNG。

## 使用真实监测数据（桥模式）

把“桥数据预处理”的产物放好后，在 `config/config_chishi.json` 的 `bridge_data` 里配置路径并启用：

```bash
python run_agent.py --bridge chishi --mode quarterly --year 2026 --quarter 1
```

季度模式只填**季度号（1~4）+ 年份**，不需要填具体日期；季度还没过完会直接报错
（例如现在第 3 季度中却填 `--quarter 3`）。

桥模式下：

- `{{cell.<指标>.<测点>.<统计量>}}`：按“测点名称 → 传感器编号”匹配，聚合报告期内的每日统计；
- `{{chart.<ID>}}`：优先从 `图库` 取真实图片，取不到生成“待补充”占位图并记入待补清单；
- 匹配规则（单元格）：**表格上下文（测点映射/表格映射）** > 配置别名 > **人工名称对照表** > 编号 > 名称全等 > 模糊包含；仍找不到回退为该指标全部传感器聚合值。
- 匹配规则（图表）：`chart_map` 显式映射 > **图注位置/指标别名**（如“4#墩墩顶主梁支座倾角”→“240/241 号 EZJD(xJd)”）> 相邻图注上下文继承 > 指标序号顺序分配。
- 名称对照表 `传感器对照/传感器名称对照/<桥名>.json`（固定产物）里的 `测点映射`（应变/振动表 断面位置→测点N→编号）与 `表格映射`（位移/倾角/裂缝/温湿度/结构温度表）会在填表时自动生效，单元格命中来源记录在 `last_run.json → bridge.match_stats`。
- **图号图名自动生成**：每张图下方自动插入“图3.1.1-1 第6跨跨中断面主梁箱内环境温度时程曲线图”样式的居中图注（按章节号编号）。
- **多位置图注展开**：“第6、7跨跨中断面环境温度监测时程曲线图、频率分布直方图”这类组合句会自动展开成 第6跨/第7跨 × 时程图/直方图 = 4 张图；若表格有 3 个监测部位而模板只标了 2 个，`auto_fill_missing_charts`（默认开）会从图库自动补齐缺失图，缺图清单写入 `last_run.json → chart_gaps`。
- **多传感器按行填充**：一个监测部位有多个传感器（如“第五跨L/4处主梁”有 178/295/297/298/301/302 六个测点）时，表格的每一行取对应传感器的值，不再整列重复同一个值；补插图严格插在该节最后一张图注之后，图号按章节连续编号。
- **“编号(特征)_图型”行识别**：报告里“184(xJsd)_时程曲线”这类行直接给出 传感器编号+特征+图型，识别器会把它们转成 `{{chart.chart_sensor_184_trend}}` 精确占位符，运行时直接取 `图库/184/DZJSD(xJsd)/时间序列图.png`，不再靠猜。
- **位置名数字保护**：节标题/表格标题里的位置数字（“第7跨L3/4断面”“第五跨L/4处主梁”“4#墩”）不再被当成动态数据替换成 `{{data.N}}`；已有模板中的同类占位符也已还原为字面量。
- **数值清洗**：`zero_cleanup`/`spike_cleanup` 剔除全零日、0 污染日和数量级异常的尖峰日（如个别传感器 75958600 这类毛刺），指标回退聚合限定在同类别传感器内，避免跨类别污染。
- **图片 RGB 兼容**：插入报告前会把 RGBA PNG 统一转成 RGB（WPS/Word 对 RGBA 渲染兼容性差可能显示空白），转换失败的日志会写到 agent.log。
- 生成后自动修正：页眉 `xxxx年第X季度数据分析报告`、落款 `xxxx年xx月xx日`、结论段 `xx月-xx月` 会替换为实际报告期；目录页码通过 `<w:updateFields/>` 标记，**首次用 Word 打开时会提示“是否更新目录”，点“是”即可刷新页码**。

常见的“待完善”配置在 `bridge_data` 的三个字段：

| 字段 | 用途 |
|---|---|
| `metrics.<指标>.feature` | 指标对应的预处理特征名（如 `WSD(temp)`） |
| `name_dict` | 人工维护的名称→编号对照表（`传感器对照/传感器名称对照/<桥名>.json`），精确命中优先 |
| `sensor_exclude` | 数据异常的传感器（编号或名称），统计时排除 |
| `chart_map` / `sensor_aliases` | 图表占位符→传感器编号、测点写法→编号 的人工映射 |

`last_run.json` 的 `bridge.match_stats` 会记录本次运行的命中来源（name_dict / fuzzy / metric_fallback），用于评估对照表覆盖率。

## Web 管理台

```bash
python -m pip install -r requirements.txt
export REPORT_WEB_TOKEN=你的令牌            # Windows: $env:REPORT_WEB_TOKEN="..."
python web/app.py                           # 默认 127.0.0.1:8456
```

打开 http://127.0.0.1:8456 即可看到本桥总览、生成报告（**季度/年度/月/周/手动**，
季度模式填季度号 1~4 + 年份、年度模式填年份，不用填日期）、下载报告、数据覆盖度、
待补清单、**配置管理（数据路径、模板上传）、调度器启停**与日志。
远程访问与防火墙/安全组设置见 [DEPLOY.md](DEPLOY.md)。

图表统一使用 Python 出图（MATLAB 路径已移除）；调度周期支持季度（季度首月）、年度（1 月）、月度、周度。
Windows 独立服务器部署见 [DEPLOY.md](DEPLOY.md) 和
[docs/部署说明_Windows小白版.md](docs/部署说明_Windows小白版.md)。

## 数据处理管道（桥数据预处理已集成）

`preprocess/` 目录已集成“桥数据预处理”项目：

- `preprocess/scripts/`：秒级原始数据 → 日级/小时级数据 → 图库/统计值 → 传感器对照表 的全部脚本；
- `preprocess/图库`、`preprocess/统计值`：已复制的现有产物（.gitignore 忽略，可重新生成）；
- `preprocess/pipeline.py`：把相互独立的脚本串成一条命令（路径全部可配）；
- `preprocess/config.json`：秒级原始数据、日级数据、图库、统计值、测点编号表格 的路径配置。

用法：

```bash
python preprocess/pipeline.py                          # 全流程
python preprocess/pipeline.py --skip-preprocess        # 只重建图库/统计值/对照表
python preprocess/pipeline.py --raw D:/数据 --daily D:/日级 --charts D:/图库 --stats D:/统计值
```

Web 管理台的“数据处理”页可配置四个路径并一键运行，实时显示步骤状态和日志。
`config/config_chishi.json` 的 `bridge_data` 已指向 `preprocess/统计值` 与 `preprocess/图库`。

## 模板里的占位符（如何告诉智能体换哪里）

模板中的动态内容用 `{{...}}` 标记，其他文字原样保留。支持的占位符：

| 类型 | 写法 | 说明 |
|---|---|---|
| 统计值 | `{{stats.temperature.max}}` | 替换为计算结果，如 `33.4` |
| 日期 | `{{date.period_start}}` | 报告期开始/结束/生成时间 |
| 图表 | `{{chart.trend}}`（独占一行） | 替换为一张 Python（matplotlib）生成的图片 |
| 可重复行 | 表格某行写 `{{rows.daily_records}}` | 这一行会按数据逐行复制 |
| 行内字段 | `{{col.temperature:0.1f}}` | 可重复行里每列的数据 |

常用统计键（以 `temperature` 列为例）：

```
stats.temperature.max / min / avg / median / std / p25 / p75 / range
stats.temperature.max_date / min_date        # 极值出现日期
stats.temperature.days_above_30              # 超过 30℃ 的天数
stats.days                                   # 数据总天数
```

支持 Python 格式说明符，如 `{{stats.temperature.std:.2f}}`、`{{col.deviation:+0.1f}}`。

拿到你自己的报告模板后，把要动态变化的数字替换成对应的占位符即可；其余文字、排版都不用动。可重复行适合“逐日明细”“各站点汇总”这类需要按数据行数扩展的表格。

## 成品报告自动识别（DOCX / PDF）：哪些图、哪些数字该替换

如果你手里只有一份**已经写好的成品报告**，没有占位符，智能体可以先把文件解析一遍，自动判断：

- **图片**：图题含“趋势/曲线/直方图/分布/对比/统计”等关键词 → 建议替换（动态数据图）；图题含“CAD/示意/图纸/照片/平面图”等 → 保留（固定图）；无图题的小图 → 大概率是 logo，保留。
- **数字**：上下文含“最高/最低/平均/标准差/挠度/超过/共/累计”等 → 建议替换（动态统计值）；上下文含“桥长/跨径/桩号/设计/合同/日期/编号”等 → 保留（固定参数，如“桥长123米”）。
- 拿不准的项标记为 `review`，由人工确认。

用法：

```bash
# 识别 DOCX 报告，输出分析 JSON，并生成“标注草稿”
python analyze_report.py --input 原报告.docx \
    --out outputs/analysis.json \
    --annotate outputs/模板草稿.docx

# 识别 PDF 报告（PDF 不能直接改，只输出 JSON 清单）
python analyze_report.py --input 原报告.pdf --out outputs/analysis_pdf.json
```

标注草稿里：

- 动态数字 → `{{stats.温度.max}}` 这类建议占位符；识别不出指标时用 `{{data.N}}` 占位；
- 动态图所在段落 → `{{chart.trend}}`（按图题关键词推测 ID，可手动改名）；
- 固定数字、CAD 图、示意图 → 原样保留。

复核流程：打开 `analysis.json`（含置信度和理由）或标注草稿，把 `{{data.N}}` 改成真实统计键、把 `{{chart.*}}` 改成 `config.json` 里 `charts.definitions` 的图表 ID，然后就能作为模板交给 `run_agent.py` 使用。识别是启发式的，正式使用前建议人工过一遍，必要时在 `report_agent/recognizer.py` 的 `DYNAMIC_WORDS` / `STATIC_WORDS` / `IMAGE_*_WORDS` 里补充你们行业的固定参数词。

样例验证：

```bash
python scripts/make_sample_bridge_report.py   # 生成 DOCX 样例（桥长123米 + 温度统计 + 趋势图 + CAD图）
python scripts/make_sample_pdf.py             # 生成 PDF 样例
python analyze_report.py --input outputs/sample/桥梁监测报告样例.docx --annotate outputs/sample/桥梁监测报告模板草稿.docx
```

## 图表：Python（matplotlib）出图

出图逻辑在 `report_agent/chart_generator.py`，全部使用 Python + matplotlib，
无需 MATLAB。模板里的 `{{chart.*}}` 占位符在桥模式下优先取“桥数据预处理”
图库里的真实图片，取不到时生成“待补充”占位图并记入待补清单。
图表类型支持折线图、直方图、柱状图、箱线图等，中文字体用 Windows 自带的
微软雅黑。

## 定时输出：季度/年度/月/周

### 方式一：常驻调度服务（推荐服务器）

修改 `config/config_<桥>.json` 的 `schedule` 字段：

```json
{
  "schedule": {
    "mode": "quarterly",     // weekly / monthly / quarterly / yearly
    "start_date": "2026-01-01",  // 季度模式起始日期（启动时先补跑已结束季度）
    "weekday": 1,            // weekly 模式：每周几（1=周一 ... 7=周日）
    "day_of_month": 1,       // monthly/quarterly/yearly：几号触发
    "hour": 8,
    "minute": 0
  }
}
```

然后运行：

```bash
python serve_scheduler.py --bridge chishi
```

服务会常驻，到点自动执行 `run_agent.py`，日志写入 `outputs/scheduler.log`。
季度模式**只在季度过完后的次季首月触发**，生成刚结束的上一季度；
年度模式**每年 1 月触发**，生成上一年全年（例如 2027-01-01 生成 2026 年年度报告）；
季度模式下也会自动附加年度任务（每年 1 月生成上一年度报告）；
`start_date` 填了之后，调度器启动时会先把“从该日期到今天的已结束季度”
补跑一遍，上一年度报告不存在时也会补跑一份；**某期图库/统计值缺失时，
调度器会先自动跑数据处理管道再生成报告**（相当于 web“生成报告”的自动版）。

### 方式二：交给系统计划任务

- Windows：运行 `install_auto_start.bat <bridge_id>`（以管理员身份），
  注册开机自启；或用“任务计划程序”添加，任务命令填
  `python run_agent.py --bridge <bridge_id> --mode quarterly --year 2026 --quarter 1`。

## 服务器部署

服务器均为 Windows，独立部署（每座桥一台服务器，无中心节点）。
照着 [docs/部署说明_Windows小白版.md](docs/部署说明_Windows小白版.md) 复制代码、
建环境、改配置、双击启动即可；完整说明见 [DEPLOY.md](DEPLOY.md)。

## 数据格式

`data/temperature_daily.csv` 至少包含日期列和数值列：

```csv
date,temperature,humidity
2026-08-01,33.2,68.5
2026-08-02,31.8,71.0
```

列名在 `config.json → data` 中配置（`date_column` / `value_columns` / `thresholds`）。报告区间：周报取最近 7 天，月报取最近 30 天（以报告日结束，含报告日）。

## 常见问题

- **图表中文乱码**：Windows 自带微软雅黑，matplotlib 已自动选择；个别精简版系统需补装中文字体。
- **数据区间为空**：检查 `data/` 文件路径和日期范围（`--date` 指定的结束日之后没有数据）。
- **生成后还有 `{{...}}` 残留**：说明模板里写了不存在的统计键，程序会报错并列出具体占位符；修正模板或 `config.json` 即可。
- **想要新的统计量**：在 `report_agent/stats.py` 的 `compute_stats` 里增加字段，模板中直接用 `{{stats.<列>.<新字段>}}`。

## 一键流程

```bash
python scripts/make_sample_data.py && python scripts/build_template.py
python run_agent.py --inspect-template
python run_agent.py --mode weekly
```
