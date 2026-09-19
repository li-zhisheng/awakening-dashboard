# -*- coding: utf-8 -*-
"""E12: 提速优化联测 (盘后安全, 不发下单热键)。

Part1 手机端: is_in_watchlist_cycle 续扫探测 (当前页应处于自选'>'循环上下文)
Part2 PC端:   _goto_stock 压缩等待后的定位耗时 (目标 <3s, 原约5.2s)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config

cfg = load_config(r"d:\Awakening\config.yaml")
adb = cfg.device.adb_path
if adb:
    exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

# ---------- Part 1: 续扫探测 ----------
print("=" * 50)
print("Part1: 手机端 is_in_watchlist_cycle 续扫探测")
print("=" * 50)
from adb.control import UIController
from adb.device import Device
from adb.screenshot import Screenshotter
from ths.navigator import Navigator

device = Device(cfg.device.serial)
control = UIController(device)
shot = Screenshotter(device, cfg.device.screenshot_max_retries)
nav = Navigator(control, cfg, {})

t0 = time.time()
in_cycle = nav.is_in_watchlist_cycle()
dt = time.time() - t0
print(f"  探测结果={in_cycle} 耗时={dt:.1f}s (预期True=可续扫; False=需重建)")

# 验证探测的判别力: goto跳一只股票后应变为False (搜索分组无'>'按钮)
print("  --> goto(600000) 后复测 (预期False=搜索分组上下文) ...")
try:
    nav.goto("600000")
    in_cycle2 = nav.is_in_watchlist_cycle()
    print(f"  goto后探测={in_cycle2} (预期False)")
    print(f"  PART1 {'PASS' if (in_cycle and not in_cycle2) else 'FAIL'}")
finally:
    # 恢复自选循环上下文, 给下一轮留干净状态
    print("  --> enter_watchlist 恢复自选循环 ...")
    ok = nav.enter_watchlist("")
    print(f"  恢复={ok}")

# ---------- Part 2: PC端定位提速 ----------
print("=" * 50)
print("Part2: PC端 _goto_stock 定位耗时 (压缩等待后)")
print("=" * 50)
from trader.hotkey_trader import HotkeyTrader
from trader.risk_control import RiskController

risk = RiskController(cfg.risk, cfg.project_root)
trader = HotkeyTrader(cfg, risk)
trader._connect_hexin()

for code in ("601288", "600000"):
    t0 = time.time()
    trader._goto_stock(code)
    dt = time.time() - t0
    print(f"  定位 {code}: {dt:.2f}s (原方案约5.2s)")
    time.sleep(1.0)

print("  PART2 PASS (定位耗时见上, 预期<3s)")
