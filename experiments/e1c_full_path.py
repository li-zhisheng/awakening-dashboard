# -*- coding: utf-8 -*-
"""E1c: 分时页是否显示多空标签 + 列表->落页->动态切日K全链路验证。"""
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


def tabs():
    xml = control.dump_ui()
    root = ET.fromstring(xml)
    out = {}
    for node in root.iter("node"):
        t = (node.get("text") or "").strip()
        if t in ("分时", "日K", "周K", "月K", "五日"):
            nums = [int(x) for x in re.findall(r"\d+", node.get("bounds", ""))]
            if len(nums) == 4:
                out[t] = ((nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2,
                          node.get("selected"))
    return out


def first_row():
    xml = control.dump_ui()
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        rid = node.get("resource-id") or ""
        if rid.endswith("fixed_column"):
            m = re.match(r"^(.+)#(\d{6})$",
                         (node.get("content-desc") or "").strip())
            if m:
                nums = [int(x) for x in re.findall(r"\d+",
                                                   node.get("bounds", ""))]
                return m.group(2), (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
    return None


print("=== 1. 切分时, 看是否有多空标签 ===")
ts = tabs()
if "分时" in ts:
    control.tap(*ts["分时"][:2])
    time.sleep(2.5)
    snap("20_minute_page")
    print(f"  分时 selected={tabs().get('分时', ('?', '?', '?'))[2]}")

print("\n=== 2. 切回日K(动态坐标) ===")
ts = tabs()
if "日K" in ts:
    control.tap(*ts["日K"][:2])
    time.sleep(2.5)
    snap("21_back_daily")

print("\n=== 3. back到自选列表 ===")
control.back()
time.sleep(2.5)
row = first_row()
print(f"  第一行: {row}")
snap("22_list_back")

print("\n=== 4. 点行落页 -> 记录默认图表模式 ===")
if row:
    control.tap(row[1], row[2])
    time.sleep(3.5)
    snap("23_row_landed")
    ts = tabs()
    for k, (x, y, sel) in ts.items():
        if sel == "true":
            print(f"  落页默认选中: {k}")

print("\n=== 5. 动态找日K并点击+验证 ===")
ts = tabs()
if ts.get("日K", ("?", "?", "false"))[2] != "true":
    target = ts["日K"][:2]
    print(f"  点日K真实坐标 {target}")
    control.tap(*target)
    time.sleep(2.5)
    ts2 = tabs()
    ok = ts2.get("日K", ("?", "?", "false"))[2] == "true"
    print(f"  切换后日K selected={ts2.get('日K', ('?', '?', '?'))[2]} -> {'成功' if ok else '失败'}")
    snap("24_after_dynamic_switch")
else:
    print("  落页已是日K")
