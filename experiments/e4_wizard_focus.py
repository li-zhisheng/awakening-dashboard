# -*- coding: utf-8 -*-
"""E4: 键盘精灵焦点修复实验 - 点击精灵输入行/搜索框确保焦点。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e4"
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
print(f"hexin窗: '{win.window_text()}' rect={win.rectangle()}")

print("== 1. 焦点hexin, 敲600000 (慢速) ==")
win.set_focus()
time.sleep(0.5)
win.type_keys("{ESC}")
time.sleep(0.4)
for ch in "600000":
    win.type_keys(ch)
    time.sleep(0.25)
time.sleep(1.5)
shot("01_typed_600000")

print("== 2. 回车前先看精灵输入行位置: 点击窗口中央避免焦点丢失? ==")
# 直接回车
win.type_keys("{ENTER}")
time.sleep(3)
shot("02_after_enter")

print("== 3. 再试: 敲603000后用鼠标点击精灵第一行区域 ==")
win.set_focus()
time.sleep(0.4)
for ch in "603000":
    win.type_keys(ch)
    time.sleep(0.25)
time.sleep(1.5)
shot("03_typed_603000")
# 精灵在窗口右下, 输入行约在窗口高度72%处, 第一条建议在输入行下方~25px
r = win.rectangle()
W, H = r.right - r.left, r.bottom - r.top
hx = r.left + int(W * 0.955)
hy = r.top + int(H * 0.71)
print(f"  鼠标点击精灵区域: ({hx},{hy}) (窗口{W}x{H})")
import win32api
import win32con


def real_click(x, y):
    win32api.SetCursorPos((x, y))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


real_click(hx, hy)
time.sleep(0.4)
# 点击后焦点进精灵, 回车确认
win.type_keys("{ENTER}")
time.sleep(3)
shot("04_after_click_enter")
print("最终标题:", win.window_text())
