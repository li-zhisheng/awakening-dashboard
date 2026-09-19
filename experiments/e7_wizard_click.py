# -*- coding: utf-8 -*-
"""E7: 键盘精灵(独立窗口)鼠标点行导航 + F1盘后下单验证。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
import win32api
import win32con
import win32gui
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e7"
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


def real_click(x, y, double=False):
    win32api.SetCursorPos((x, y))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def find_wizard(pid):
    hits = []

    def cb(h, _):
        _, wpid = win32process.GetWindowThreadProcessId(h)
        if wpid == pid and win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if "键盘精灵" in t:
                hits.append((h, t))
        return True
    win32process = sys.modules.get("win32process")
    import win32process as wp
    hits.clear()

    def cb2(h, _):
        _, wpid = wp.GetWindowThreadProcessId(h)
        if wpid == pid and win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if "键盘精灵" in t:
                hits.append((h, t))
        return True
    win32gui.EnumWindows(cb2, None)
    return hits


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
pid = find_pid("hexin.exe")
print(f"主窗: '{win.window_text()}' pid={pid}")

# 搜索Edit
from pywinauto import findwindows
from pywinauto.controls.hwndwrapper import HwndWrapper
elem = findwindows.find_element(control_id=62267, class_name="Edit",
                                top_level_only=False)
edit = HwndWrapper(elem.handle)
er = edit.rectangle()
print(f"搜索Edit rect=({er.left},{er.top},{er.right},{er.bottom})")

print("== 1. 点击Edit输入601288 ==")
real_click((er.left + er.right) // 2, (er.top + er.bottom) // 2)
time.sleep(0.5)
win.type_keys("601288", pause=0.1)
time.sleep(1.5)

print("== 2. 按修正偏移点击精灵首行 (相对Edit中心 +63,-279) ==")
ecx, ecy = (er.left + er.right) // 2, (er.top + er.bottom) // 2
row1_x, row1_y = ecx + 63, ecy - 279
print(f"  点击({row1_x},{row1_y})")
print(f"== 3. 鼠标点击首行建议 ({row1_x},{row1_y}) ==")
real_click(row1_x, row1_y)
time.sleep(3)
shot("11_after_click_row")

print("== 4. F1下单 (601288页面) ==")
win.set_focus()
time.sleep(0.4)
win.type_keys("{F1}")
time.sleep(3)
shot("12_after_f1")
print("最终主窗标题:", win.window_text())
