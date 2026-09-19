# -*- coding: utf-8 -*-
"""E1b: 验证盘后横条挤压tab坐标假设 + 列表落页的日K切换。"""
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8")

from adb.control import UIController
from adb.device import Device
from adb.screenshot import Screenshotter
from config import load_config

cfg = load_config("config.yaml")
adb = cfg.device.adb_path
if adb:
    exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

device = Device(cfg.device.serial)
control = UIController(device)
shot = Screenshotter(device, cfg.device.screenshot_max_retries)
OUT = r"d:\Awakening\logs\e1"


def snap(name):
    import cv2
    p = os.path.join(OUT, f"{name}.png")
    cv2.imwrite(p, shot.capture())
    print(f"  [截图] {p}")


def kline_tab_bounds():
    """UI dump找日K/分时tab的真实bounds。"""
    xml = control.dump_ui()
    root = ET.fromstring(xml)
    out = []
    for node in root.iter("node"):
        t = (node.get("text") or "").strip()
        if t in ("分时", "日K", "周K", "月K", "五日"):
            b = node.get("bounds", "")
            nums = [int(x) for x in re.findall(r"\d+", b)]
            if len(nums) == 4:
                cx, cy = (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
                out.append((t, tuple(nums), (cx, cy), node.get("selected")))
    return out


print("=== 1. 当前个股页tab真实坐标 ===")
for t, b, c, sel in kline_tab_bounds():
    mark = " <-配置坐标(227,664)" if t == "日K" else ""
    print(f"  {t}: bounds={b} center={c} selected={sel}{mark}")

print("\n=== 2. 回自选列表 ===")
control.tap(450, 2359)   # 底部自选tab
time.sleep(2.5)
snap("10_watchlist_page")

print("\n=== 3. 点第一行 -> 看落页默认图表 ===")
xml = control.dump_ui()
root = ET.fromstring(xml)
first = None
for node in root.iter("node"):
    rid = node.get("resource-id") or ""
    if rid.endswith("fixed_column"):
        m = re.match(r"^(.+)#(\d{6})$", (node.get("content-desc") or "").strip())
        if m:
            nums = [int(x) for x in re.findall(r"\d+", node.get("bounds", ""))]
            first = (m.group(2), (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2)
            break
print(f"  第一行: {first}")
if first:
    control.tap(first[1], first[2])
    time.sleep(3.5)
    snap("11_row_landed")
    print("  落页tab状态:")
    for t, b, c, sel in kline_tab_bounds():
        print(f"    {t}: center={c} selected={sel}")

print("\n=== 4. 点配置坐标(227,664) ===")
control.tap(227, 664)
time.sleep(2.5)
snap("12_tap_config_pos")
tabs = {t: (c, sel) for t, b, c, sel in kline_tab_bounds()}
print(f"  日K selected={tabs.get('日K', ('?','?'))[1]}")

print("\n=== 5. 若仍未日K, 点日K真实坐标 ===")
tabs = {t: (c, sel) for t, b, c, sel in kline_tab_bounds()}
if tabs.get("日K", (None, "false"))[1] != "true":
    c = tabs["日K"][0]
    print(f"  点真实坐标 {c}")
    control.tap(*c)
    time.sleep(2.5)
    snap("13_tap_real_pos")
    tabs = {t: (c2, sel) for t, b, c2, sel in kline_tab_bounds()}
    print(f"  日K selected={tabs.get('日K', ('?', '?'))[1]}")
else:
    print("  已是日K, 跳过")
