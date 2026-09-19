# -*- coding: utf-8 -*-
"""E17c: hexin前台化后面板检测(物理坐标) + 点击唤出实验。"""
import sys
import time
sys.stdout.reconfigure(encoding="utf-8")
import ctypes
import win32gui
import numpy as np
from trader.pcwin import find_pid, grab_screen, real_click

user32 = ctypes.windll.user32
logical_w = user32.GetSystemMetrics(0)          # 逻辑宽(不感知进程)
phys_w = win32gui.GetSystemMetrics(1) if False else None
from PIL import ImageGrab
phys_w = ImageGrab.grab().size[0]               # 物理宽
scale = phys_w / logical_w
print(f"逻辑宽={logical_w} 物理宽={phys_w} scale={scale}")

pid = find_pid("hexin.exe")
main_hwnd = None


def _handler(hwnd, _):
    global main_hwnd
    import win32process
    if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid == pid and win32gui.GetWindowText(hwnd).startswith("同花顺"):
            main_hwnd = hwnd


win32gui.EnumWindows(_handler, None)
rect = win32gui.GetWindowRect(main_hwnd)
print(f"hexin hwnd={main_hwnd:#x} 逻辑rect={rect}")


def panel_measure(tag):
    """前台化后面板检测(逻辑->物理坐标换算)。"""
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(0.8)
    L, T, R, B = rect
    # 面板行: 窗口顶部逻辑 y 2-36, x 12%-60%
    lx0, ly0 = L + (R - L) * 0.12, T + 2
    lx1, ly1 = L + (R - L) * 0.60, T + 36
    img = grab_screen(lx0 * scale, ly0 * scale, lx1 * scale, ly1 * scale)
    arr = np.asarray(img)
    r = arr[:, :, 0].astype(int)
    g = arr[:, :, 1].astype(int)
    b = arr[:, :, 2].astype(int)
    red = int(((r > 150) & (r - g > 60) & (r - b > 40)).sum())
    green = int(((g > 130) & (g - r > 50) & (g - b > 50)).sum())
    print(f"  [{tag}] 红={red} 绿={green} 合计={red+green}")
    return red + green


print("== 前台化后面板基线 ==")
base = panel_measure("前台化")

if base < 500:
    print("\n== 面板不可见, 尝试点击唤出 ==")
    L, T, R, B = rect
    for tag, (lx, ly) in {
        "右侧图区": (R - 190, T + 500),
        "中央": (L + (R - L) * 0.5, T + (B - T) * 0.5),
        "右上标签空白": (R - 420, T + 12),
    }.items():
        real_click(lx, ly, settle=0.8)
        v = panel_measure(f"点击{tag}后")
        if v >= 500:
            print(f"  *** 点击'{tag}'唤出成功! ***")
            break
else:
    print("面板可见(基线即有效), 记录阈值基线")
