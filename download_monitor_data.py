#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从桥梁健康监测平台自动下载指定时段的监测数据（Excel）。

目标页面:
  登录 -> 基础设施监测 -> 湖南高速公路集团桥梁结构健康监测平台(旧)
  -> 选桥(找不到时先点「在线监测」进入桥列表)
  -> 在线监测 -> 作用监测 -> 车辆荷载 -> 车道统计
  -> 点图表右上角「数据视图」图标 -> 点「导出Excel」下载

用法（Windows cmd / PowerShell）:
    python download_monitor_data.py --bridges 赤石大桥,矮寨大桥,洞庭湖大桥

说明:
  - 账号密码读取 monitor_credentials.json（{"username": "...", "password": "..."}）
    或环境变量 MONITOR_USER / MONITOR_PASS（cmd 用 set，PowerShell 用 $env:）；
  - 验证码需要手动输入并点击「登录」，脚本检测到登录成功后会继续；
  - 按 7 天一段自动切换时间范围、点「数据视图」、「导出Excel」下载；
  - 支持一次跑多座桥，每桥独立输出目录与进度记录，中断后可续跑。

说明:
  - 会打开一个可见的 Edge 窗口，自动填好用户名/密码；
  - 验证码需要你手动输入并点击「登录」，脚本检测到登录成功后会继续；
  - 之后按 7 天一段自动切时间范围、点「数据视图」、下载 Excel；
  - 任一步骤找不到元素时，会在 debug/ 目录保存截图和页面 HTML，并给出提示。

常用参数:
  --start/--end     时间范围（默认 2026-01-01 ~ 2026-03-31）
  --chunk           每段天数（默认 7）
  --bridge          单座桥名称（默认 赤石大桥）
  --bridges         多座桥，逗号分隔（如 赤石大桥,矮寨大桥,洞庭湖大桥）
  --out             下载保存目录（仅单桥时生效）
  --once            只下载第一段后退出（用于试跑）
  --headless        隐藏浏览器窗口（仅调试用，正常运行不要加）
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "http://10.30.1.156:8088/home/fusion/monitoring/comprehensive"
POST_LOGIN_MARKERS = ["桥梁列表", "结构监测", "工作台", "下载任务", "基础设施",
                      "在线监测", "赤石大桥", "视频监测"]
PLATFORM_MARKERS = ["赤石大桥", "在线监测", "结构检测", "桥梁列表", "车道统计"]
DEBUG_DIR = Path("debug")
CRED_FILE = Path("monitor_credentials.json")


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_credentials() -> tuple[str, str]:
    """从环境变量或本地凭据文件读取账号密码（优先环境变量）。"""
    user = os.environ.get("MONITOR_USER", "")
    pwd = os.environ.get("MONITOR_PASS", "")
    if user and pwd:
        return user, pwd
    if CRED_FILE.exists():
        try:
            data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            user = data.get("username") or data.get("user") or ""
            pwd = data.get("password") or data.get("pass") or ""
            if user and pwd:
                return user, pwd
        except Exception:
            pass
    print("请先配置账号密码，二选一：")
    print(f'  1) 创建本地文件 {CRED_FILE}（不会被提交到 git），内容:')
    print('     {"username": "你的用户名", "password": "你的密码"}')
    print("  2) 或设置环境变量:")
    print('     $env:MONITOR_USER = "用户名"')
    print('     $env:MONITOR_PASS = "密码"')
    sys.exit(1)


def dump_debug(page, tag: str):
    """失败时保存截图和 DOM，方便定位问题。"""
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = DEBUG_DIR / f"{ts}_{tag}.png"
    dom = DEBUG_DIR / f"{ts}_{tag}.html"
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception:
        pass
    try:
        dom.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        for i, fr in enumerate(page.frames):
            try:
                (DEBUG_DIR / f"{ts}_{tag}_frame{i}.html").write_text(
                    fr.content(), encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass
    log(f"已保存调试文件: {shot} / {dom}")


def iter_frames(page):
    """遍历页面主 frame 和所有子 iframe。"""
    seen = set()
    for fr in page.frames:
        if fr not in seen:
            seen.add(fr)
            yield fr


def find_text_element(page, texts):
    """在页面所有 frame 中找包含指定文本的最后一个可见元素，返回 locator 或 None。"""
    for text in texts:
        for fr in iter_frames(page):
            try:
                loc = fr.get_by_text(text).last
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                pass
    return None


def wait_for_text(page, text: str, timeout_s: int = 60) -> bool:
    """轮询等待某段文字（可见元素）出现在页面任意 frame 中。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if find_text_element(page, [text]) is not None:
            return True
        time.sleep(2)
    return False


def close_modals(page):
    """关闭可能遮挡操作的弹窗（版本提示/修改密码/通知等）。"""
    for btn_text in ("暂不更新", "以后再说", "知道了", "关闭", "取消"):
        try:
            btn = find_text_element(page, [btn_text])
            if btn is not None:
                btn.click(timeout=3000)
                log(f"已关闭弹窗: {btn_text}")
                time.sleep(1)
        except Exception:
            pass


def click_by_text(page, text: str, timeout_ms: int = 15000):
    """点击包含指定文本的可见元素（自动遍历 iframe），找不到时抛异常。"""
    for fr in iter_frames(page):
        try:
            loc = fr.get_by_text(text).last
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=timeout_ms)
            log(f"已点击: {text}")
            return loc
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    dump_debug(page, f"click_{text}")
    raise RuntimeError(f"找不到可点击元素: {text}")


def wait_infra_frame(page, timeout_s: int = 30):
    """等待「基础设施监测」的 iframe 元素出现，返回可操作的 frame_locator。"""
    selector = "iframe[src*='7001']"
    page.wait_for_selector(selector, timeout=timeout_s * 1000)
    return page.frame_locator(selector)


def click_in_frame(page, fl, primary_text: str, timeout_ms: int = 20000):
    """在指定 iframe 内点击旧平台入口（按多个文本候选匹配）。"""
    candidates = [primary_text, "桥梁结构健康监测平台(旧)", "健康监测平台(旧)",
                  "湖南高速公路集团桥梁结构健康监测平台", "湖南高速公路集团桥梁结构"]
    for t in candidates:
        try:
            loc = fl.get_by_text(t).last
            loc.wait_for(state="visible", timeout=10000)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=10000)
            log(f"iframe 内已点击: {t}")
            return
        except Exception:
            continue
    dump_debug(page, "platform_link")
    raise RuntimeError("在基础设施监测页面找不到旧平台入口，已保存调试文件。")


def page_has_platform(page) -> bool:
    """判断页面（含 iframe）是否已出现旧平台的特征内容。"""
    try:
        for m in PLATFORM_MARKERS:
            if page.get_by_text(m, exact=False).count():
                return True
        for fr in page.frames:
            for m in PLATFORM_MARKERS:
                if fr.get_by_text(m, exact=False).count():
                    return True
    except Exception:
        pass
    return False


def find_platform_page(context, old_page, original_url, timeout_s: int = 30):
    """点击旧平台入口后，等待新标签页（或当前页跳转/iframe）出现平台页面。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for p in context.pages:
            if p.is_closed():
                continue
            try:
                if p is old_page:
                    if p.url != original_url and ("fusion" in p.url or "monitor" in p.url):
                        return p
                    if page_has_platform(p):
                        return p
                    continue
                if page_has_platform(p) or "fusion" in p.url or "monitor" in p.url:
                    return p
            except Exception:
                pass
        time.sleep(1)
    dump_debug(old_page, "platform_page")
    raise RuntimeError("点击旧平台链接后未检测到目标页面，已保存调试文件。")


def wait_login_success(page, timeout_s: int = 600):
    """等待用户手动输入验证码并登录成功。"""
    log("请在打开的浏览器窗口中输入验证码并点击「登录」...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for marker in POST_LOGIN_MARKERS:
            try:
                el = page.get_by_text(marker, exact=False).filter(visible=True).first
                if el.count() and el.is_visible():
                    log(f"检测到登录成功（页面出现: {marker}）")
                    return
            except Exception:
                pass
        time.sleep(2)
    dump_debug(page, "login_timeout")
    raise RuntimeError("等待登录超时（10 分钟）。请检查验证码/账号密码后重试。")


def ensure_logged_in(page, timeout_s: int = 30):
    """切换到旧平台后，等页面就绪；若出现登录框则等待用户再次登录。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            u = page.locator("input[name='username']")
            if u.count() and u.first.is_visible():
                log("检测到旧平台需要登录，请在浏览器中再次输入验证码并登录...")
                wait_login_success(page, timeout_s=timeout_s)
                return
            for marker in POST_LOGIN_MARKERS:
                if page.get_by_text(marker, exact=False).count():
                    return
        except Exception:
            pass
        time.sleep(2)


def handle_user_select_dialog(page, timeout_s: int = 30):
    """处理旧平台的「用户选择」弹窗：选第一个用户并点确定。"""
    if not wait_for_text(page, "用户选择", timeout_s=timeout_s):
        return
    log("检测到「用户选择」弹窗，自动选择用户...")
    sel = None
    deadline = time.time() + 15
    while time.time() < deadline:
        for fr in iter_frames(page):
            try:
                s = fr.locator(".el-select").first
                if s.count() and s.is_visible():
                    sel = s
                    break
            except Exception:
                pass
        if sel is not None:
            break
        time.sleep(1)
    if sel is None:
        dump_debug(page, "user_select")
        raise RuntimeError("「用户选择」弹窗未加载下拉框，已保存调试文件。")
    sel.click(timeout=5000)
    time.sleep(1)
    picked = False
    for fr in iter_frames(page):
        try:
            item = fr.locator(".el-select-dropdown__item").first
            if item.count() and item.is_visible():
                item.click(timeout=5000)
                log(f"已选择用户: {item.inner_text().strip()}")
                picked = True
                break
        except Exception:
            pass
    if not picked:
        dump_debug(page, "user_select_option")
        raise RuntimeError("「用户选择」下拉没有选项，已保存调试文件。")
    time.sleep(0.5)
    ok = find_text_element(page, ["确定"])
    if ok is None:
        dump_debug(page, "user_select_confirm")
        raise RuntimeError("找不到「确定」按钮，已保存调试文件。")
    ok.click(timeout=5000)
    time.sleep(2)
    log("「用户选择」已确认")


def find_date_inputs(page):
    """跨 iframe 定位当前可见的日期范围输入框，返回 (开始, 结束) 或 (None, None)。"""
    for fr in iter_frames(page):
        try:
            inputs = fr.locator("input.el-range-input")
            n = inputs.count()
            for i in range(0, max(0, n - 1), 2):
                if inputs.nth(i).is_visible() and inputs.nth(i + 1).is_visible():
                    return inputs.nth(i), inputs.nth(i + 1)
        except Exception:
            pass
    return None, None


def set_date_range(page, start: str, end: str):
    """设置查询日期范围，并验证输入框中的值已更新。"""
    start_in, end_in = find_date_inputs(page)
    if start_in is None:
        dump_debug(page, "date_input")
        raise RuntimeError("找不到日期输入框，已保存调试文件。")
    for el, val in ((start_in, start), (end_in, end)):
        el.click()
        el.fill(val)
        el.press("Enter")
        time.sleep(0.5)
    time.sleep(1.0)
    # 关闭可能弹出的日历面板，避免遮挡后续点击
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(0.5)
    cur_start = start_in.input_value().strip()
    cur_end = end_in.input_value().strip()
    if cur_start != start or cur_end != end:
        log(f"日期输入后未生效: 当前 [{cur_start} ~ {cur_end}]，期望 [{start} ~ {end}]")
        dump_debug(page, "date_mismatch")
    else:
        log(f"时间范围已设置: {start} ~ {end}")


def find_export_button(page, texts=None):
    """找导出/下载 Excel 按钮（默认优先数据视图弹窗里的「导出Excel」）。"""
    if texts is None:
        texts = ["导出Excel", "导出EXCEL", "导出", "Excel", "EXCEL", "下载"]
    return find_text_element(page, texts)


def close_pickup_popups(page):
    """关闭可能遮挡图表的浮层（日期面板/下拉/弹窗），并确认已关闭。"""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(0.3)
    # 点击“车道统计”标签让日期输入失焦（已在当前页时无副作用）
    tab = find_text_element(page, ["车道统计"])
    if tab is not None:
        try:
            tab.click(timeout=3000)
            time.sleep(0.4)
        except Exception:
            pass
    # 若仍有可见浮层，点页面空白处（顶部导航区域）
    for fr in iter_frames(page):
        try:
            for sel in (".el-picker-panel", ".el-select-dropdown", ".el-popper"):
                loc = fr.locator(sel)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        page.mouse.click(400, 100)
                        time.sleep(0.4)
                        return
        except Exception:
            pass


def open_data_view(page):
    """打开 ECharts 图表的「数据视图」弹窗。

    优先通过 Vue 组件里的图表实例触发，失败则点击图表右上角工具箱图标。
    """
    chart_ids = ["charts3", "charts4", "charts5"]
    js = """
    (id) => {
      let el = document.getElementById(id);
      while (el) {
        const vm = el.__vue__;
        if (vm) {
          let cur = vm;
          while (cur) {
            if (cur.myChart && cur.myChart.dispatchAction) {
              cur.myChart.dispatchAction({type: 'showDataView'});
              return true;
            }
            cur = cur.$parent;
          }
        }
        el = el.parentElement;
      }
      return false;
    }
    """
    # 先尝试通过图表实例触发
    for fr in iter_frames(page):
        for cid in chart_ids:
            try:
                loc = fr.locator(f"#{cid}")
                if loc.count() and loc.first.is_visible():
                    try:
                        if fr.evaluate(js, cid):
                            log(f"已通过图表实例打开数据视图: {cid}")
                            time.sleep(1)
                            return
                    except Exception:
                        pass
            except Exception:
                pass
    # 坐标回退：点图表右上角工具箱（数据视图图标在最右侧附近），最多两轮
    for attempt in (1, 2):
        close_pickup_popups(page)
        for fr in iter_frames(page):
            for cid in chart_ids:
                try:
                    canvas = fr.locator(f"#{cid} canvas").first
                    box = canvas.bounding_box() if canvas.count() else None
                    if not box:
                        box = fr.locator(f"#{cid}").first.bounding_box()
                    if not box or box["width"] < 50:
                        continue
                    log(f"图表 {cid} 画布位置: "
                        f"x={box['x']:.0f} y={box['y']:.0f} w={box['width']:.0f} h={box['height']:.0f}")
                    # 工具箱图标：实测从画布右缘 -11/-38/-65/-92/-120（数据视图在最右）
                    for off_x in (-10, -25, -40, -55, -70, -85, -100, -115, -130, -145):
                        x = box["x"] + box["width"] + off_x
                        for off_y in (12, 22):
                            y = box["y"] + off_y
                            page.mouse.click(x, y)
                            time.sleep(1.2)
                            if find_export_button(page, ["导出Excel", "导出EXCEL"]) is not None:
                                log(f"已点击工具箱坐标打开数据视图: {cid} @ ({x:.0f},{y:.0f})")
                                return
                except Exception:
                    pass
        if attempt == 1:
            log("第一轮未打开「数据视图」，关闭浮层后重试...")
    dump_debug(page, "data_view_open")
    raise RuntimeError("无法打开「数据视图」弹窗，已保存调试文件。")


def close_data_view(page):
    """关闭 ECharts 数据视图弹窗。"""
    for fr in iter_frames(page):
        try:
            btn = fr.locator("#btnClose").first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3000)
                time.sleep(0.5)
                return
        except Exception:
            pass
    try:
        btn = find_text_element(page, ["关闭"])
        if btn is not None:
            btn.click(timeout=3000)
            time.sleep(0.5)
    except Exception:
        pass


def save_data_url_fallback(page, dest: Path) -> bool:
    """导出按钮以 data: URL 方式导航时，把内容解码保存为文件。"""
    import base64
    for fr in iter_frames(page):
        try:
            u = fr.url
            if u.startswith("data:application/vnd.ms-excel"):
                payload = u.split(",", 1)[1]
                dest.write_bytes(base64.b64decode(payload))
                log(f"已从 data URL 保存: {dest.name}")
                return True
        except Exception:
            pass
    return False


def download_chunk(page, start: str, end: str, out_dir: Path) -> bool:
    """为一个 7 天区间下载 Excel；返回是否成功。"""
    set_date_range(page, start, end)
    time.sleep(3)  # 等图表按新日期刷新
    close_pickup_popups(page)

    # 若数据视图弹窗未打开则打开
    if find_export_button(page, ["导出Excel", "导出EXCEL"]) is None:
        open_data_view(page)
        time.sleep(1)

    export = find_export_button(page, ["导出Excel", "导出EXCEL"])
    if export is None:
        dump_debug(page, f"export_btn_{start}_{end}")
        raise RuntimeError(f"找不到「导出Excel」按钮 [{start}~{end}]，已保存调试文件。")

    dest = out_dir / f"车道统计_{start}_{end}.xls"
    try:
        with page.expect_download(timeout=60000) as dl_info:
            export.click(timeout=10000)
        dl = dl_info.value
        dl.save_as(str(dest))
        log(f"下载完成: {dest.name}")
    except PlaywrightTimeoutError:
        if not save_data_url_fallback(page, dest):
            dump_debug(page, f"download_{start}_{end}")
            raise RuntimeError(f"导出超时 [{start}~{end}]，已保存调试文件。")
    finally:
        close_data_view(page)
    return True


def navigate_to_lane_stats(page, bridge, timeout_s=60):
    """导航到指定桥的「车道统计」页（假设已进入旧平台页面）。"""
    try:
        click_by_text(page, bridge, timeout_ms=8000)
    except RuntimeError:
        log(f"页面未见「{bridge}」，先点击「在线监测」进入桥列表...")
        click_by_text(page, "在线监测", timeout_ms=15000)
        time.sleep(3)
        close_modals(page)
        click_by_text(page, bridge, timeout_ms=15000)
    time.sleep(2)
    nav_steps = ("在线监测", "作用监测", "车辆荷载", "车道统计")
    for i, step in enumerate(nav_steps):
        click_by_text(page, step, timeout_ms=15000)
        nxt = nav_steps[i + 1] if i + 1 < len(nav_steps) else None
        if nxt is not None:
            if not wait_for_text(page, nxt, timeout_s=timeout_s):
                log(f"等待「{nxt}」超时，重试点击「{step}」...")
                click_by_text(page, step, timeout_ms=15000)
                if not wait_for_text(page, nxt, timeout_s=timeout_s):
                    dump_debug(page, f"wait_{nxt}")
                    raise RuntimeError(f"等待「{nxt}」出现超时，已保存调试文件。")
        time.sleep(1.5)
    log(f"已到达「{bridge}」车道统计页")


def build_chunks(start: str, end: str, chunk_days: int):
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    chunks = []
    cur = start_d
    while cur <= end_d:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end_d)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + timedelta(days=1)
    return chunks


def load_progress(progress_file):
    if progress_file.exists():
        try:
            return set(tuple(x) for x in json.loads(progress_file.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_progress(progress_file, done):
    progress_file.write_text(
        json.dumps([list(x) for x in done], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser(description="桥梁健康监测平台车道统计 Excel 下载")
    ap.add_argument("--start", default="2026-01-01", help="开始日期")
    ap.add_argument("--end", default="2026-03-31", help="结束日期")
    ap.add_argument("--chunk", type=int, default=7, help="每段天数(默认7)")
    ap.add_argument("--bridge", default="赤石大桥", help="桥梁名称（单桥）")
    ap.add_argument("--bridges", default="",
                    help="多座桥梁，逗号分隔，如 赤石大桥,矮寨大桥,洞庭湖大桥")
    ap.add_argument("--out", default="", help="输出目录（默认 inputs/{桥名}_车道统计_2026Q1）")
    ap.add_argument("--once", action="store_true", help="只下载第一段后退出（测试用）")
    ap.add_argument("--headless", action="store_true", help="无头模式(仅调试)")
    args = ap.parse_args()

    user, pwd = load_credentials()
    if args.bridges.strip():
        bridges = [b.strip() for b in args.bridges.split(",") if b.strip()]
    else:
        bridges = [args.bridge]
    chunks = build_chunks(args.start, args.end, args.chunk)
    month = int(args.start[5:7])
    quarter = (month - 1) // 3 + 1
    log(f"共 {len(chunks)} 段; 桥梁: {', '.join(bridges)} ({args.start} ~ {args.end})")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=args.headless,
            args=["--start-maximized"],
        )
        # 窗口要足够宽：图表右边缘约 1625px，太窄会把工具箱图标裁掉
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)

        # 登录
        page.locator("input[name='username']").wait_for(timeout=60000)
        page.locator("input[name='username']").fill(user)
        page.locator("input[name='password']").fill(pwd)
        log("用户名/密码已填入，等待手动输入验证码并登录...")
        wait_login_success(page)

        # 进入旧平台
        click_by_text(page, "基础设施监测")
        time.sleep(2)
        infra_frame = wait_infra_frame(page)
        log("基础设施监测 iframe 已加载")
        original_url = page.url
        click_in_frame(page, infra_frame, "湖南高速公路集团桥梁结构健康监测平台(旧)")
        page = find_platform_page(page.context, page, original_url)
        log(f"已进入旧平台页面: {page.url}")
        ensure_logged_in(page, timeout_s=60)
        handle_user_select_dialog(page)
        close_modals(page)
        time.sleep(3)

        # 逐桥下载
        failed = []
        for bridge in bridges:
            if args.out and len(bridges) == 1:
                out_dir = Path(args.out)
            else:
                out_dir = Path(f"inputs/{bridge}_车道统计_{args.start[:4]}Q{quarter}")
            out_dir.mkdir(parents=True, exist_ok=True)
            progress_file = out_dir / "_progress.json"
            done = load_progress(progress_file)
            log(f"===== 开始处理桥梁: {bridge} -> {out_dir} =====")
            try:
                navigate_to_lane_stats(page, bridge)
            except Exception as ex:
                log(f"导航到「{bridge}」失败: {ex}")
                failed.append((bridge, "导航", str(ex)))
                continue
            for i, (s, e) in enumerate(chunks, 1):
                if (s, e) in done:
                    log(f"[{bridge} {i}/{len(chunks)}] 已下载过，跳过: {s} ~ {e}")
                    continue
                log(f"[{bridge} {i}/{len(chunks)}] 开始下载: {s} ~ {e}")
                try:
                    download_chunk(page, s, e, out_dir)
                    done.add((s, e))
                    save_progress(progress_file, done)
                except Exception as ex:
                    log(f"[{bridge} {i}/{len(chunks)}] 失败: {ex}")
                    failed.append((bridge, f"{s}~{e}", str(ex)))
                    if args.once:
                        break
                    continue
                if args.once:
                    log(f"--once 模式，{bridge} 已下载第一段，退出。")
                    break
            if args.once:
                break
        browser.close()

    if failed:
        log(f"完成，但有 {len(failed)} 个失败: {failed}")
        sys.exit(2)
    log("全部完成")


if __name__ == "__main__":
    main()
