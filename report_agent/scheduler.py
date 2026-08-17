# -*- coding: utf-8 -*-
"""定时调度：每周或每月自动生成报告。

使用 APScheduler 的 CronTrigger 实现精确调度，不再轮询。
APScheduler 未安装时自动降级为原轮询模式（每 30s 检查一次）。

配置项（config.json → schedule）：
  mode: weekly | monthly
  weekday: 1-7（1=周一...7=周日），weekly 模式用
  day_of_month: 1-31，monthly 模式用
  hour / minute: 触发时刻

也支持手动通过 run_agent.py --mode weekly 单次执行。
"""

import calendar
import datetime as dt
import logging
import os
import subprocess
import sys
import time

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


def _run_report_generation(cwd: str, mode: str) -> None:
    """调用 run_agent.py 生成报告。"""
    log.info("触发报告生成（模式=%s）", mode)
    cmd = [sys.executable, "run_agent.py", "--mode", mode]
    if mode == "quarterly":
        # 季度首月(1/4/7/10月)触发时，报告上一个完整季度：
        # 4/1 跑 1~3 月、7/1 跑 4~6 月、10/1 跑 7~9 月、1/1 跑去年 10~12 月
        today = dt.date.today()
        q = (today.month - 1) // 3          # 当前季度 0 基
        first = dt.date(today.year, q * 3 + 1, 1)
        end = first - dt.timedelta(days=1)  # 上一季度最后一天
        cmd += ["--date", end.isoformat()]
        log.info("季度报告期截止: %s（覆盖 %s ~ %s）",
                 end.isoformat(),
                 dt.date(end.year, ((end.month - 1) // 3) * 3 + 1, 1).isoformat(),
                 end.isoformat())
    elif mode == "yearly":
        # 年度任务在次年 1 月触发时，报告上一年全年
        end = dt.date(dt.date.today().year - 1, 12, 31)
        cmd += ["--date", end.isoformat()]
        log.info("年度报告期截止: %s", end.isoformat())
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=1800)
        if proc.returncode != 0:
            log.error("报告生成失败，返回码 %s", proc.returncode)
        else:
            log.info("报告生成完成")
    except Exception as exc:  # noqa: BLE001
        log.exception("报告生成异常: %s", exc)


def run_with_apscheduler(cfg: dict) -> None:
    """使用 APScheduler 的 CronTrigger 精确调度。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    schedule = cfg.get("schedule", {})
    mode = schedule.get("mode", "weekly")
    hour = int(schedule.get("hour", 8))
    minute = int(schedule.get("minute", 0))
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
        args=[cwd, mode],
        id="report_generation",
        misfire_grace_time=3600,  # 错过1小时内仍可补执行
        coalesce=True,  # 多次错过只执行一次
    )

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
    while True:
        now = dt.datetime.now()
        target = next_run_time(now, schedule)
        wait_seconds = (target - now).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 30))
            continue

        _run_report_generation(cwd, mode)
        time.sleep(60)


def run_forever(config_path: str = None) -> None:
    """常驻调度服务：到点调用 run_agent.py 生成报告。

    优先使用 APScheduler（精确调度，不轮询）；
    APScheduler 未安装时降级为 30s 轮询模式。
    """
    cfg = load_config(config_path)
    output_dir = cfg.get("output_dir", "outputs")
    _setup_logging(output_dir)

    try:
        import apscheduler  # noqa: F401
        run_with_apscheduler(cfg)
    except ImportError:
        log.warning("未安装 APScheduler，降级为轮询模式（建议 pip install apscheduler）")
        run_with_polling(cfg)
