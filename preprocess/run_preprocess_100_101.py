# -*- coding: utf-8 -*-
"""
批量预处理：传感器 100、101，2026 年 1 月 1 日 ~ 3 月 31 日
==========================================================

调用同目录下的 preprocess_sensor_data.py，
结果输出到 D 盘文件夹 D:\\preprocess_sensor_data
（不存在会自动创建）。

用法:
    python run_preprocess_100_101.py
"""

import os
import subprocess
import sys

# ---------------- 需要按服务器修改的部分 ----------------
OUTPUT_ROOT = r"D:\preprocess_sensor_data"          # 结果输出目录(D 盘)
DATA_ROOT = r"D:\信科采集软件解析数据"              # 原始数据根目录
SENSORS = "100,101"                                 # 传感器编号
START_DATE = "2026-01-01"                           # 开始日期
END_DATE = "2026-03-31"                             # 结束日期
WORKERS = 8                                         # 并行进程数
# -------------------------------------------------------


def main():
    # 输出目录不存在则创建
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    print(f"[信息] 输出目录: {OUTPUT_ROOT}")

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "preprocess_sensor_data.py")
    cmd = [
        sys.executable, script,
        "--mode", "all",
        "--data-root", DATA_ROOT,
        "--output-root", OUTPUT_ROOT,
        "--sensors", SENSORS,
        "--start", START_DATE,
        "--end", END_DATE,
        "--workers", str(WORKERS),
    ]
    print("[信息] 执行命令:", " ".join(cmd))
    print()
    subprocess.run(cmd, check=True)
    print(f"\n[完成] 结果已保存到: {OUTPUT_ROOT}")
    print("       daily/<传感器>/<特征>/<日期>.csv、inventory.csv、summary.csv")


if __name__ == "__main__":
    main()
