# -*- coding: utf-8 -*-
"""E15: 托盘恢复修复回归 - HIDDEN状态下恢复+读委托 (对应上线前发现的崩溃场景)。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

from config import load_config
from trader.hotkey_trader import HotkeyTrader
from trader.risk_control import RiskController

cfg = load_config(r"d:\Awakening\config.yaml")
trader = HotkeyTrader(cfg, RiskController(cfg.risk, cfg.project_root))

import win32gui
hwnd = trader._find_xiadan_hwnd(trader._find_pid("xiadan.exe") or 0)
if hwnd:
    print(f"修复前状态: 主窗hwnd={hwnd:#x}, "
          f"visible={win32gui.IsWindowVisible(hwnd)} (预期False=HIDDEN)")
else:
    print("修复前状态: 主窗不存在")

trader._connect_xiadan()
for e in (trader._td_user.today_entrusts or []):
    if e.get("证券代码") == "601288":
        print("601288委托:", e.get("操作"), e.get("委托数量"), "@",
              e.get("委托价格"), "状态:", e.get("状态"),
              "成交:", e.get("成交数量"), "编号:", e.get("合同编号"))
trader._close_xiadan()
hwnd2 = trader._find_xiadan_hwnd(trader._find_pid("xiadan.exe") or 0)
print(f"关窗后: visible={win32gui.IsWindowVisible(hwnd2) if hwnd2 else '-'} (预期False)")
print("E15 PASS" if not (hwnd2 and win32gui.IsWindowVisible(hwnd2)) else "E15 FAIL")
