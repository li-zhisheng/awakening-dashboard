# -*- coding: utf-8 -*-
"""E3: 键盘精灵逐键诊断 - 每步截图看精灵是否弹出。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e3"
os.makedirs(OUT, exist_ok=True)


def shot(name):
    r = win.rectangle()
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    p = os.path.join(OUT, f"{name}.png")
    img.save(p)
    print(f"  [截图] {name}")


def find_pid(image_name):
    out = os.popen(f'tasklist /FI "IMAGENAME eq {image_name}" /FO CSV /NH').read()
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and image_name.lower() in parts[0].lower():
            return int(parts[1].strip('"'))
    return None


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
if win is None:
    win = app.top_window()
print(f"hexin窗: '{win.window_text()}'")

print("== 0. 前置焦点 ==")
win.set_focus()
time.sleep(0.6)
top = pywinauto.findwindows.find_elements(active_only=True)
shot("00_focused")

print("== 1. ESC ==")
win.type_keys("{ESC}")
time.sleep(0.5)
shot("01_esc")

print("== 2. 敲 '6' ==")
win.type_keys("6")
time.sleep(1.2)
shot("02_typed_6")

print("== 3. 继续 '01288' ==")
win.type_keys("01288", pause=0.08)
time.sleep(1.2)
shot("03_typed_full")

print("== 4. ENTER ==")
win.type_keys("{ENTER}")
time.sleep(3)
shot("04_after_enter")

print("== 5. 枚举hexin所有顶层窗口(找键盘精灵) ==")
for w in app.windows(visible_only=True):
    try:
        print(f"  '{w.window_text()}' [{w.class_name()}]")
    except Exception:
        pass
