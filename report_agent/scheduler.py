# -*- coding: utf-8 -*-
"""定时调度：每周/每月/每季度/每年自动生成报告。

使用 APScheduler 的 CronTrigger 实现精确调度，不再轮询。
APScheduler 未安装时自动降级为原轮询模式（每 30s 检查一次）。

配置项（config.json → schedule）：
  mode: weekly | monthly | quarterly | yearly
  weekday: 1-7（1=周一...7=周日），weekly 模式用
  day_of_month: 1-31，monthly/quarterly/yearly 模式用
  hour / minute: 触发时刻
  start_date: YYYY-MM-DD，调度起始日期：
    - quarterly：启动时先补跑“从 start_date 到今天的已结束季度”，
      之后每到季度过完（次季首月 day_of_month 日）再生成上一季度；
      同时每年 1 月还会自动生成上一年度报告；
    - yearly：每年 1 月 day_of_month 日触发，生成上一年全年；
      启动时若上一年度报告还不存在，也会补跑一份。
    例：start_date=2026-01-01，8 月启动 → 先补跑 Q1、Q2，再等 Q3 过完。

也支持手动通过 run_agent.py --mode weekly 单次执行。
"""

import calendar
import datetime as dt
import logging
import os
import subprocess
import sys
import time

from .period_utils import (last_completed_quarter, last_completed_year,
                           quarter_range)
from .config import load_config

log = logging.getLogger("report-agent.scheduler")


def next_run_time(now: dt.datetime, schedule: dict) -> dt.datetime:
    """计算下一个执行时刻（用于日志展示，APScheduler 内部使用自己的触发器）。

    schedule:
      mode=weekly  -> 每周 weekday(1=周一..7=周日) 的 hour:minute
      mode=monthly -> 每月 day_of_month 的 hour:minute（超长月份取当月最后一天）
    """
    hour = int(schedule.get("hour", 8))
    minute = int(schedule.get("minute", 0))
    mode = schedule.get("mode", "weekly")

    if mode == "monthly":
        day = int(schedule.get("day_of_month", 1))
        year, month = now.year, now.month
        last = calendar.monthrange(year, month)[1]
        target_day = min(day, last)
        candidate = now.replace(day=target_day, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            last = calendar.monthrange(year, month)[1]
            candidate = now.replace(
                year=year, month=month, day=min(day, last),
                hour=hour, minute=minute, second=0, microsecond=0,
            )
        return candidate

    if mode == "quarterly":
        # 每季度首月（1/4/7/10）的 day_of_month 日触发，生成刚结束的上一季度
        day = int(schedule.get("day_of_month", 1))
        for month in (1, 4, 7, 10):
            last = calendar.monthrange(now.year, month)[1]
            candidate = now.replace(
                year=now.year, month=month, day=min(day, last),
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if candidate > now:
                return candidate
        candidate = now.replace(
            year=now.year + 1, month=1, day=min(day, 31),
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        return candidate

    if mode == "yearly":
        day = int(schedule.get("day_of_month", 1))
        year = now.year
        candidate = now.replace(year=year, month=1, day=min(day, 31), hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate.replace(year=year + 1)
        return candidate

    weekday = int(schedule.get("weekday", 1))  # 1=周一 ... 7=周日
    py_weekday = (weekday - 1) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (py_weekday - candidate.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    candidate += dt.timedelta(days=days_ahead)
    if candidate <= now:
        candidate += dt.timedelta(days=7)
    return candidate


def _setup_logging(output_dir: str) -> None:
    """配置日志：控制台 + 文件，与 agent.py 风格一致。"""
    os.makedirs(output_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "outputs", "logs", "scheduler.log"),
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def _run_report_generation(cwd: str, mode: str,
                           year: int = None, quarter: int = None,
                           config_path: str = None) -> None:
    """调用 run_agent.py 生成报告。"""
    log.info("触发报告生成（模式=%s）", mode)
    cmd = [sys.executable, "run_agent.py", "--mode", mode]
    if config_path:
        # 必须带 --config，否则 run_agent 会落到默认配置(config.json)，
        # 导致调度器一直用错桥的数据源（如报 data/temperature_daily.csv 不存在）
        cmd += ["--config", config_path]
    if mode == "quarterly":
        if quarter is None:
            # 季度过完（次季首月触发）时，报告刚结束的上一季度：
            # 4/1 跑 1~3 月、7/1 跑 4~6 月、10/1 跑 7~9 月、1/1 跑去年 10~12 月
            year, quarter = last_completed_quarter()
        start, end = quarter_range(year, quarter)
        cmd += ["--year", str(year), "--quarter", str(quarter)]
        log.info("季度报告期: %s（覆盖 %s ~ %s）",
                 f"{year}.{start.month}~{end.month}",
                 start.isoformat(), end.isoformat())
    elif mode == "yearly":
        # 年度任务在次年 1 月触发时，报告上一年全年；补跑时显式传 year
        if year is None:
            year = dt.date.today().year - 1
        cmd += ["--year", str(year)]
        log.info("年度报告期: %d年（%d-01-01 ~ %d-12-31）",
                 year, year, year)
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=1800)
        if proc.returncode != 0:
            log.error("报告生成失败，返回码 %s", proc.returncode)
        else:
            log.info("报告生成完成")
    except Exception as exc:  # noqa: BLE001
        log.exception("报告生成异常: %s", exc)


def _pending_quarters(start_date: dt.date, today: dt.date = None) -> list:
    """返回从 start_date 到今天之间“已完整结束且完全落在 start_date 之后”的季度。

    例如 start_date=2026-01-01、today=2026-08-19 -> [(2026,1),(2026,2)]；
    start_date 为今天（2026-08-19，Q3 未结束）-> []。
    """
    today = today or dt.date.today()
    if today < start_date:
        return []
    ly, lq = last_completed_quarter(today)
    out = []
    y, q = start_date.year, (start_date.month - 1) // 3 + 1
    while (y, q) <= (ly, lq):
        s, _e = quarter_range(y, q)
        if s >= start_date:
            out.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return out


def _yearly_report_exists(output_dir: str, year_label: str) -> bool:
    """输出目录里是否已有某年度的年度报告（文件名含 “2025年” 等）。"""
    if not output_dir or not os.path.isdir(output_dir):
        return False
    try:
        for fn in os.listdir(output_dir):
            if fn.lower().endswith(".docx") and year_label in fn:
                return True
    except OSError:
        pass
    return False


def _catch_up_yearly(cwd: str, cfg: dict) -> None:
    """启动补跑：上一完整年度还没有年度报告时生成一份（季度/年度模式通用）。

    例如 start_date=2025-06-30、2026 年 8 月启动，会自动补生成
    2025 年年度报告（已存在则跳过，避免每次重启重跑）。
    """
    schedule = cfg.get("schedule", {})
    prev_year = last_completed_year()
    # 起始日期保护：start_date 所在年份晚于上一年度才不补跑
    # （start_date=2025-06-30 时，2025 年度仍然补跑）
    start_str = str(schedule.get("start_date") or "").strip()
    if start_str:
        try:
            if dt.date.fromisoformat(start_str).year > prev_year:
                log.info("起始日期 %s 晚于 %d 年，不补跑 %d 年年度报告",
                         start_str, prev_year, prev_year)
                return
        except ValueError:
            log.warning("schedule.start_date 无效: %r，忽略补跑", start_str)
            return
    label = f"{prev_year}年"
    if _yearly_report_exists(cfg.get("output_dir", "outputs"), label):
        log.info("已存在 %d 年年度报告，跳过补跑", prev_year)
        return
    log.info("== 补跑 %d 年年度报告 ==", prev_year)
    _run_report_generation(cwd, "yearly", year=prev_year,
                           config_path=cfg.get("_config_path") or "")


def _catch_up_pending(cwd: str, cfg: dict) -> None:
    """调度器启动时补跑：季度模式补已结束季度；同时补上一年年度报告。"""
    schedule = cfg.get("schedule", {})
    # 年度报告补跑对季度/年度模式都执行
    _catch_up_yearly(cwd, cfg)
    if schedule.get("mode") != "quarterly":
        return
    start_str = str(schedule.get("start_date") or "").strip()
    if not start_str:
        log.info("未配置 schedule.start_date，调度从今天开始（当前季度过完才生成）")
        return
    try:
        start_date = dt.date.fromisoformat(start_str)
    except ValueError:
        log.warning("schedule.start_date 无效: %r，忽略补跑", start_str)
        return
    pending = _pending_quarters(start_date)
    if not pending:
        log.info("起始日期 %s 之后没有需要补跑的已结束季度", start_date.isoformat())
        return
    log.info("起始日期 %s：补跑 %d 个已结束季度：%s",
             start_date.isoformat(), len(pending),
             "、".join(f"{y}Q{q}" for y, q in pending))
    for y, q in pending:
        log.info("== 补跑 %d 年第 %d 季度 ==", y, q)
        _run_report_generation(cwd, "quarterly", year=y, quarter=q,
                               config_path=cfg.get("_config_path") or "")


def run_with_apscheduler(cfg: dict) -> None:
    """使用 APScheduler 的 CronTrigger 精确调度。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    schedule = cfg.get("schedule", {})
    mode = schedule.get("mode", "weekly")
    hour = int(schedule.get("hour", 8))
    minute = int(schedule.get("minute", 0))
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = cfg.get("_config_path", "")

    scheduler = BlockingScheduler()

    if mode == "monthly":
        day = int(schedule.get("day_of_month", 1))
        trigger = CronTrigger(day=day, hour=hour, minute=minute)
        log.info(
            "APScheduler 启动：每月 %d 日 %02d:%02d 触发（%s 模式）",
            day, hour, minute, mode,
        )
    elif mode == "yearly":
        day = int(schedule.get("day_of_month", 1))
        trigger = CronTrigger(month="1", day=day, hour=hour, minute=minute)
        log.info(
            "APScheduler 启动：每年 1 月 %d 日 %02d:%02d 触发（%s 模式）",
            day, hour, minute, mode,
        )
    elif mode == "quarterly":
        # 每季度首月 day_of_month 日触发（覆盖当季）
        day = int(schedule.get("day_of_month", 1))
        trigger = CronTrigger(month="1,4,7,10", day=day, hour=hour, minute=minute)
        log.info(
            "APScheduler 启动：每季度首月 %d 日 %02d:%02d 触发（%s 模式）",
            day, hour, minute, mode,
        )
    else:
        weekday_map = {1: "mon", 2: "tue", 3: "wed", 4: "thu",
                       5: "fri", 6: "sat", 7: "sun"}
        wd = int(schedule.get("weekday", 1))
        cron_dow = weekday_map.get(wd, "mon")
        trigger = CronTrigger(day_of_week=cron_dow, hour=hour, minute=minute)
        log.info(
            "APScheduler 启动：每周%s %02d:%02d 触发（%s 模式）",
            cron_dow, hour, minute, mode,
        )

    scheduler.add_job(
        _run_report_generation,
        trigger=trigger,
        args=[cwd, mode, None, None, config_path],
        id="report_generation",
        misfire_grace_time=3600,  # 错过1小时内仍可补执行
        coalesce=True,  # 多次错过只执行一次
    )
    if mode == "quarterly":
        # 季度模式下，每年 1 月额外生成上一年度报告
        day = int(schedule.get("day_of_month", 1))
        scheduler.add_job(
            _run_report_generation,
            CronTrigger(month="1", day=day, hour=hour, minute=minute),
            args=[cwd, "yearly", None, None, config_path],
            id="yearly_report",
            misfire_grace_time=3600,
            coalesce=True,
        )
        log.info("已附加年度报告任务：每年 1 月 %d 日 %02d:%02d 生成上一年报告",
                 day, hour, minute)

    # 先补跑 start_date 之后已结束的季度 / 上一年度报告，再进入周期调度
    _catch_up_pending(cwd, cfg)

    # 下次执行时间
    next_time = next_run_time(dt.datetime.now(), schedule)
    log.info("下次执行时间: %s", next_time)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("调度服务已停止")


def run_with_polling(cfg: dict) -> None:
    """降级模式：使用原轮询方式（APScheduler 未安装时）。"""
    schedule = cfg.get("schedule", {})
    mode = schedule.get("mode", "weekly")
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    log.info("轮询模式启动：模式=%s，下次执行=%s", mode, next_run_time(dt.datetime.now(), schedule))
    _catch_up_pending(cwd, cfg)
    while True:
        now = dt.datetime.now()
        target = next_run_time(now, schedule)
        wait_seconds = (target - now).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 30))
            continue

        _run_report_generation(cwd, mode,
                               config_path=cfg.get("_config_path") or "")
        time.sleep(60)


def run_forever(config_path: str = None) -> None:
    """常驻调度服务：到点调用 run_agent.py 生成报告。

    优先使用 APScheduler（精确调度，不轮询）；
    APScheduler 未安装时降级为 30s 轮询模式。
    """
    cfg = load_config(config_path)
    cfg.setdefault("_config_path", config_path or "")
    output_dir = cfg.get("output_dir", "outputs")
    _setup_logging(output_dir)

    try:
        import apscheduler  # noqa: F401
        run_with_apscheduler(cfg)
    except ImportError:
        log.warning("未安装 APScheduler，降级为轮询模式（建议 pip install apscheduler）")
        run_with_polling(cfg)
