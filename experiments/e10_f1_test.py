# -*- coding: utf-8 -*-
"""E10: F1下单全流程验证 (盘后): F1 -> 观察弹层 -> xiadan回查委托。"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
import win32api
import win32con
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e10"
os.makedirs(OUT, exist_ok=True)


def find_pid(image_name):
    out = os.popen(f'tasklist /FI "IMAGENAME eq {image_name}" /FO CSV /NH').read()
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and image_name.lower() in parts[0].lower():
            return int(parts[1].strip('"'))
    return None


def shot(name):
    r = win.rectangle()
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    img.save(os.path.join(OUT, f"{name}.png"))
    print(f"  [截图] {name}")


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
print(f"主窗: '{win.window_text()}' (应在601288分时页)")

print("== 1. F1 ==")
win.set_focus()
time.sleep(0.5)
win.type_keys("{F1}")
time.sleep(1.2)
shot("10_after_f1_1s")
time.sleep(2)
shot("11_after_f1_3s")

print("== 2. 检查新窗口/弹层 ==")
app2 = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=3)
for w in app2.windows(visible_only=True):
    print(f"  窗口: '{w.window_text()}' [{w.class_name()}]")
# xiadan窗口是否也被拉起?
xpid = find_pid("xiadan.exe")
print(f"  xiadan进程: {xpid}")

print("== 3. ESC清理 ==")
win.set_focus()
time.sleep(0.3)
win.type_keys("{ESC}")
time.sleep(1)
shot("12_after_esc")
