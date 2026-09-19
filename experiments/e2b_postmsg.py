# -*- coding: utf-8 -*-
"""E2b: 非交互桌面下的hexin控制实验 (PostMessage按键 + PrintWindow截图)。"""
import ctypes
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import win32con
import win32gui
import win32ui

OUT = r"d:\Awakening\logs\e2"
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def find_hexin():
    hits = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if "同花顺" in t:
                hits.append((hwnd, t))
        return True
    win32gui.EnumWindows(cb, None)
    return hits


def capture(hwnd, name):
    """PrintWindow截图 (不依赖交互桌面)。"""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(int(hdc))
    save = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save.SelectObject(bmp)
    PW_RENDERFULLCONTENT = 0x2
    ok = user32.PrintWindow(hwnd, save.GetSafeHdc(), PW_RENDERFULLCONTENT)
    info = bmp.GetInfo()
    data = bmp.GetBitmapBits(True)
    import numpy as np
    arr = np.frombuffer(data, dtype=np.uint8).reshape(
        info["bmHeight"], info["bmWidth"], 4)
    import cv2
    p = os.path.join(OUT, f"{name}.png")
    cv2.imwrite(p, cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR))
    win32gui.DeleteObject(bmp.GetHandle())
    save.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    print(f"  [PrintWindow={ok}] {p} ({w}x{h})")


def send_key_char(hwnd, ch):
    """PostMessage发送字符键。"""
    vk = ord(ch.upper())
    sc = user32.MapVirtualKeyW(vk, 0)
    lparam_dn = 1 | (sc << 16)
    lparam_up = 1 | (sc << 16) | (1 << 30) | (1 << 31)
    user32.PostMessageW(hwnd, win32con.WM_KEYDOWN, vk, lparam_dn)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, win32con.WM_KEYUP, vk, lparam_up)


hits = find_hexin()
print("hexin窗口:", [(hex(h), t) for h, t in hits])
if not hits:
    sys.exit(1)
hwnd, title = hits[0]

print(f"\n=== 1. 基线截图 '{title}' ===")
capture(hwnd, "20_baseline")

print("\n=== 2. PostMessage发送 '6' (键盘精灵应弹出) ===")
send_key_char(hwnd, "6")
time.sleep(1.5)
capture(hwnd, "21_after_6")

print("\n=== 3. 继续发送 00127 + ENTER ===")
for ch in "00127":
    send_key_char(hwnd, ch)
    time.sleep(0.25)
time.sleep(1.2)
send_key_char(hwnd, "\r")
time.sleep(3)
capture(hwnd, "22_after_enter")

print("\n=== 4. 前台窗口检查 ===")
fg = win32gui.GetForegroundWindow()
print(f"  foreground={fg} '{win32gui.GetWindowText(fg) if fg else ''}'")
