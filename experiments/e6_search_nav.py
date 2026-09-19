# -*- coding: utf-8 -*-
"""E6: 搜索框Edit确定性导航 + F1盘后下单行为验证。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
import win32api
import win32con
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e6"
os.makedirs(OUT, exist_ok=True)


def shot(name, region=None):
    r = win.rectangle()
    bbox = (r.left, r.top, r.right, r.bottom)
    if region:
        l, t, rt, b = region
        bbox = (r.left + l, r.top + t, r.left + rt, r.top + b)
    img = ImageGrab.grab(bbox=bbox)
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


def real_click(x, y):
    win32api.SetCursorPos((x, y))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
print(f"主窗: '{win.window_text()}'")

print("== 1. 点击搜索框, 输入601288 ==")
try:
    from pywinauto import findwindows
    from pywinauto.controls.hwndwrapper import HwndWrapper
    elem = findwindows.find_element(control_id=62267, class_name="Edit",
                                    top_level_only=False)
    edit = HwndWrapper(elem.handle)
    r = edit.rectangle()
except Exception as e:
    print(f"  枚举失败({e}), 用上次dump坐标")
    r = type("R", (), {"left": 1576, "top": 1011, "right": 1811,
                       "bottom": 1031})()
print(f"  搜索Edit rect=({r.left},{r.top},{r.right},{r.bottom})")
real_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
time.sleep(0.5)
win.type_keys("601288", pause=0.1)
time.sleep(1.5)
shot("01_typed_in_search", region=(1000, 500, 1929, 1048))

print("== 2. 回车导航 ==")
win.type_keys("{ENTER}")
time.sleep(3)
shot("02_after_enter")

print("== 3. F1下单测试 (601288页面上) ==")
win.set_focus()
time.sleep(0.4)
win.type_keys("{F1}")
time.sleep(3)
shot("03_after_f1")

print("== 4. ESC清理可能的弹层, 再截图 ==")
win.set_focus()
time.sleep(0.3)
win.type_keys("{ESC}")
time.sleep(1)
shot("04_after_esc")
print("最终标题:", win.window_text())
