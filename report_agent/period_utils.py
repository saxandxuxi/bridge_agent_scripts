# -*- coding: utf-8 -*-
"""纯日期工具：季度起止日期 / 最近已结束季度（无第三方依赖）。"""

import calendar
import datetime as dt


def quarter_range(year: int, quarter: int) -> tuple:
    """返回第 year 年第 quarter 季度的起止日期 (start, end)。"""
    q = int(quarter)
    if not 1 <= q <= 4:
        raise ValueError(f"季度必须是 1~4，收到: {quarter}")
    ms = (q - 1) * 3 + 1
    me = q * 3
    start = dt.date(int(year), ms, 1)
    end = dt.date(int(year), me, calendar.monthrange(int(year), me)[1])
    return start, end


def last_completed_quarter(today: dt.date = None) -> tuple:
    """返回今天之前最近一个“已完整结束”的季度 (year, quarter)。

    例如 2026-08-19（第 3 季度中）-> (2026, 2)；2026-01-01 -> (2025, 4)。
    """
    today = today or dt.date.today()
    q = (today.month - 1) // 3 + 1
    if q == 1:
        return today.year - 1, 4
    return today.year, q - 1


def last_completed_year(today: dt.date = None) -> int:
    """返回最近一个已完整结束的年份（当前年份永远不算已结束）。"""
    today = today or dt.date.today()
    return today.year - 1
