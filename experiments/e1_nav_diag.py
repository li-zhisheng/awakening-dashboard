# -*- coding: utf-8 -*-
"""E1: 手机导航复现诊断 - enter_watchlist逐步截图取证。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from adb.control import UIController
from adb.device import Device
from adb.screenshot import Screenshotter
from config import load_config

cfg = load_config("config.yaml")
import os
adb = cfg.device.adb_path
if adb:
    exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

device = Device(cfg.device.serial)
control = UIController(device)
shot = Screenshotter(device, cfg.device.screenshot_max_retries)
OUT = r"d:\Awakening\logs\e1"

os.makedirs(OUT, exist_ok=True)


def snap(name):
    img = shot.capture()
    p = os.path.join(OUT, f"{name}.png")
    import cv2
    cv2.imwrite(p, img)
    print(f"  [截图] {p}")


def dump_rows():
    xml = control.dump_ui()
    import re
    rows = re.findall(r'content-desc="([^"#]+)#(\d{6})"', xml)
    print(f"  列表行: {rows[:6]}{'...' if len(rows) > 6 else ''} 共{len(rows)}")
    return rows


print("=== 0. 当前页面状态 ===")
snap("00_current")
dump_rows()

print("\n=== 1. 模拟云同步后的列表页: 下拉刷新 ===")
control.swipe(540, 920, 540, 1500, 450)
time.sleep(2)
snap("01_after_refresh")
dump_rows()

print("\n=== 2. 点第一行(打开行情页, 默认分时?) ===")
rows = dump_rows()
xml = control.dump_ui()
# 取第一行坐标
import xml.etree.ElementTree as ET
root = ET.fromstring(xml)
first_pos = None
for node in root.iter("node"):
    rid = node.get("resource-id") or ""
    if rid.endswith("fixed_column"):
        import re as _re
        m = _re.match(r"^(.+)#(\d{6})$", (node.get("content-desc") or "").strip())
        if m:
            b = node.get("bounds", "")
            nums = [int(x) for x in _re.findall(r"\d+", b)]
            if len(nums) == 4:
                first_pos = ((nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2)
            break
print(f"  第一行坐标: {first_pos}")
if first_pos:
    control.tap(*first_pos)
    time.sleep(3)
    snap("02_after_row_tap")
    print("  (应为此刻页面: 分时 or 日K?)")

print("\n=== 3. 点日K tab(227,664) ===")
control.tap(227, 664)
time.sleep(2.5)
snap("03_after_kline_tab")

print("\n=== 4. UI dump当前页状态 ===")
xml = control.dump_ui()
root = ET.fromstring(xml)
for node in root.iter("node"):
    rid = node.get("resource-id") or ""
    t = (node.get("text") or "").strip()
    sel = node.get("selected")
    if any(k in rid for k in ("navi_title", "tab", "kline", "minute")) and (t or sel):
        print(f"  [{rid.split('/')[-1]}] text='{t}' selected={sel}")
