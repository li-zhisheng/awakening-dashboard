# -*- coding: utf-8 -*-
"""E11: 两项修复联测 (盘后安全, 无真实下单)。

Part1 手机端: ensure_kline_page 动态日K切换 + enter_watchlist 全流程
      (验证盘后横条挤压下仍能落到日K页, selected=true)
Part2 PC端:  daily_bootstrap F12登录 -> 等自动登录 -> 关闭委托窗口
      (验证热键就绪状态恢复, 状态文件按日期记录)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config

CFG_PATH = r"d:\Awakening\config.yaml"
cfg = load_config(CFG_PATH)

adb = cfg.device.adb_path
if adb:
    exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

# ---------- Part 1: 手机端动态日K ----------
print("=" * 50)
print("Part1: 手机端 ensure_kline_page / enter_watchlist")
print("=" * 50)
from adb.control import UIController
from adb.device import Device
from adb.screenshot import Screenshotter
from ths.navigator import Navigator

device = Device(cfg.device.serial)
control = UIController(device)
shot = Screenshotter(device, cfg.device.screenshot_max_retries)
nav = Navigator(control, cfg, {})

tab = nav._kline_tab_node()
if tab is None:
    print("  当前页无日K节点(可能在列表页), 直接走enter_watchlist")
else:
    pos, selected = tab
    print(f"  当前页日K tab: 坐标={pos} selected={selected}")

print("  --> enter_watchlist('') ...")
t0 = time.time()
ok = nav.enter_watchlist("")
dt = time.time() - t0
print(f"  enter_watchlist 返回={ok} 耗时={dt:.1f}s")

tab = nav._kline_tab_node()
if tab:
    pos, selected = tab
    print(f"  落页日K tab: 坐标={pos} selected={selected}")
    code, name = nav.get_stock_info()
    print(f"  当前股票: {code} {name}")
    print(f"  PART1 {'PASS' if (ok and selected) else 'FAIL'}")
else:
    print("  PART1 FAIL: 落页后仍找不到日K节点")

# ---------- Part 2: PC端每日F12 bootstrap ----------
print("=" * 50)
print("Part2: PC端 daily_bootstrap (F12登录+关窗)")
print("=" * 50)
from trader.hotkey_trader import HotkeyTrader
from trader.risk_control import RiskController

risk = RiskController(cfg.risk, cfg.project_root)
trader = HotkeyTrader(cfg, risk)

marker = os.path.join(cfg.project_root, "logs", "hotkey_bootstrap.json")
if os.path.isfile(marker):
    os.remove(marker)
    print("  已清除旧marker, 强制执行bootstrap")

t0 = time.time()
rep = trader.daily_bootstrap(force=True)
dt = time.time() - t0
print(f"  bootstrap返回: {rep} 耗时={dt:.1f}s")

xd = trader._xiadan_main_window(visible_only=True)
print(f"  bootstrap后xiadan主窗可见: {xd is not None} (预期False=已隐藏到托盘)")
if os.path.isfile(marker):
    with open(marker, "r", encoding="utf-8") as f:
        print(f"  marker内容: {f.read().strip()}")
print(f"  PART2 {'PASS' if rep.get('ok') and xd is None else 'FAIL'}")

# 幂等性: 第二次调用应跳过
rep2 = trader.daily_bootstrap()
print(f"  二次调用(应skip): {rep2}")
