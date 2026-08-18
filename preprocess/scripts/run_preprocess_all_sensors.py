# -*- coding: utf-8 -*-
"""
全量预处理：全部传感器，2026 年 1 月 1 日 ~ 3 月 31 日
======================================================

步骤:
  1) 枚举 D:\\信科采集软件解析数据 下的所有传感器编号(数字文件夹)，
     打印并保存到结果目录的 sensor_ids.txt;
  2) 调用 preprocess_sensor_data.py 对全部传感器做摸底 + 预处理，
     结果输出到 D:\\preprocess_sensor_data。

海量数据保护:
  - 摸底只扫 2026-01-01 ~ 2026-03-31 范围内的目录;
  - 断点续跑默认开启，已生成过的 daily 日文件自动跳过;
  - 汇总表流式写入，结果不全部攒在内存。

用法:
    python run_preprocess_all_sensors.py
"""

import os
import subprocess
import sys
import datetime as dt

# ---------------- 需要按服务器修改的部分 ----------------
DATA_ROOT = r"D:\信科采集软件解析数据"          # 原始数据根目录
OUTPUT_ROOT = r"D:\preprocess_sensor_data"      # 结果输出目录(D 盘)
START_DATE = "2026-01-01"                       # 开始日期
END_DATE = "2026-03-31"                         # 结束日期
WORKERS = 8                                     # 并行进程数
SENSORS_PER_WORKER = 3                          # 每个进程一次处理几个传感器
RESUME = True                                   # 断点续跑
# -------------------------------------------------------


def log_append(msg):
    """把驱动脚本自己的步骤也追加到结果目录的 preprocess.log。"""
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    path = os.path.join(OUTPUT_ROOT, "preprocess.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                + " " + msg + "\n")


def list_sensor_ids(root):
    """返回数据根目录下所有数字命名的子目录(传感器编号)，按数值排序。"""
    ids = []
    try:
        it = os.scandir(root)
    except OSError as exc:
        print(f"[错误] 无法访问数据根目录: {root} ({exc})")
        sys.exit(1)
    with it:
        for ent in it:
            if ent.is_dir() and ent.name.isdigit():
                ids.append(ent.name)
    return sorted(ids, key=int)


def check_script_version(script):
    """确认配套的 preprocess_sensor_data.py 是新版(支持 --sensors-file)。"""
    try:
        out = subprocess.run([sys.executable, script, "--help"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=60)
        help_text = (out.stdout or "") + (out.stderr or "")
        return "--sensors-file" in help_text and "--resume" in help_text
    except Exception:
        return False


def main():
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    log_append("=" * 28 + " 全量预处理开始 " + "=" * 28)

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "preprocess_sensor_data.py")
    if not check_script_version(script):
        msg = ("[错误] 配套的 preprocess_sensor_data.py 不是最新版，"
               "请更新后重跑")
        print(msg)
        log_append(msg)
        sys.exit(1)

    # ---------- 步骤 1: 枚举全部传感器编号 ----------
    msg = f"[1/2] 枚举传感器编号: {DATA_ROOT}"
    print(msg)
    log_append(msg)
    sensors = list_sensor_ids(DATA_ROOT)
    if not sensors:
        msg = "[错误] 没有找到任何数字命名的传感器目录，请检查 DATA_ROOT"
        print(msg)
        log_append(msg)
        sys.exit(1)
    msg = f"[1/2] 共 {len(sensors)} 个传感器"
    print(msg)
    log_append(msg)
    print(f"      编号: {','.join(sensors)}")
    list_path = os.path.join(OUTPUT_ROOT, "sensor_ids.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sensors) + "\n")
    print(f"      已保存: {list_path}")
    log_append(f"[1/2] 传感器编号已保存: {list_path}")

    # ---------- 步骤 2: 全量摸底 + 预处理 ----------
    cmd = [
        sys.executable, script,
        "--mode", "all",
        "--data-root", DATA_ROOT,
        "--output-root", OUTPUT_ROOT,
        "--sensors-file", list_path,
        "--start", START_DATE,
        "--end", END_DATE,
        "--workers", str(WORKERS),
        "--sensors-per-worker", str(SENSORS_PER_WORKER),
    ]
    if RESUME:
        cmd.append("--resume")

    msg = (f"[2/2] 开始处理 {len(sensors)} 个传感器 "
           f"({START_DATE} ~ {END_DATE}) ...")
    print("\n" + msg)
    log_append(msg)
    print("      执行:", " ".join(cmd))
    print()
    subprocess.run(cmd, check=True)
    msg = f"[完成] 全部传感器预处理结果已保存到: {OUTPUT_ROOT}"
    print("\n" + msg)
    log_append(msg)
    print("       daily/<传感器>/<特征>/<日期>.csv、inventory.csv、summary.csv")


if __name__ == "__main__":
    main()
