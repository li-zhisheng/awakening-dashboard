# -*- coding: utf-8 -*-
"""E8: 精灵双击/键序/新窗口 三假设一次验证。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
import win32api
import win32con
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e8"
os.makedirs(OUT, exist_ok=True)


def find_pid(image_name):
    out = os.popen(f'tasklist /FI "IMAGENAME eq {image_name}" /FO CSV /NH').read()
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and image_name.lower() in parts[0].lower():
            return int(parts[1].strip('"'))
    return None


def real_click(x, y, double=False):
    win32api.SetCursorPos((x, y))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.06)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def shot(name):
    r = win.rectangle()
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    img.save(os.path.join(OUT, f"{name}.png"))
    print(f"  [截图] {name}")


def app_titles():
    return [(w.window_text(), w.class_name()) for w in
            pywinauto.Application().connect(process=PID, timeout=3)
            .windows(visible_only=True)]


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
PID = find_pid("hexin.exe")
print(f"主窗: '{win.window_text()}'")

from pywinauto import findwindows
from pywinauto.controls.hwndwrapper import HwndWrapper
elem = findwindows.find_element(control_id=62267, class_name="Edit",
                                top_level_only=False)
edit = HwndWrapper(elem.handle)
er = edit.rectangle()
ecx, ecy = (er.left + er.right) // 2, (er.top + er.bottom) // 2
ROW1 = (ecx + 63, ecy - 279)

print("== A. 双击精灵首行 ==")
real_click(ecx, ecy)
time.sleep(0.4)
win.type_keys("601288", pause=0.1)
time.sleep(1.5)
real_click(*ROW1, double=True)
time.sleep(3)
shot("A_double_click")
print(f"  标题: {win.window_text()}")
print(f"  窗口列表: {app_titles()}")

print("== B. 若失败: DOWN+ENTER ==")
if "自选股" in win.window_text():
    real_click(ecx, ecy)
    time.sleep(0.4)
    win.type_keys("601288", pause=0.1)
    time.sleep(1.5)
    win.type_keys("{VK_DOWN}")
    time.sleep(0.3)
    win.type_keys("{ENTER}")
    time.sleep(3)
    shot("B_down_enter")
    print(f"  标题: {win.window_text()}")
    print(f"  窗口列表: {app_titles()}")
