# -*- coding: utf-8 -*-
"""E17: 快捷键面板检测校准 - 测量面板区域红/绿像素基线。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
from trader.pcwin import find_pid, find_window_hwnd, grab_screen, real_click

TITLE = "网上股票交易系统5.0"
pid = find_pid("hexin.exe")
# hexin主窗标题动态变化(页面名后缀), 用前缀匹配
import win32gui
main_hwnd = None


def _handler(hwnd, _):
    global main_hwnd
    import win32process
    if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        t = win32gui.GetWindowText(hwnd)
        if wpid == pid and t.startswith("同花顺"):
            main_hwnd = hwnd


win32gui.EnumWindows(_handler, None)
rect = win32gui.GetWindowRect(main_hwnd)
left, top, right, bottom = rect
w, h = right - left, bottom - top
print(f"hexin主窗: hwnd={main_hwnd:#x} rect={rect} ({w}x{h})")


def measure(tag, x0, y0, x1, y1):
    img = grab_screen(x0, y0, x1, y1)
    if img is None:
        print(f"  [{tag}] 截屏失败")
        return
    arr = np.asarray(img)
    r = arr[:, :, 0].astype(int)
    g = arr[:, :, 1].astype(int)
    b = arr[:, :, 2].astype(int)
    red = int(((r > 150) & (r - g > 60) & (r - b > 40)).sum())
    green = int(((g > 130) & (g - r > 50) & (g - b > 50)).sum())
    pink = int(((r > 200) & (g > 90) & (g < 190) & (b > 110) & (b < 200)
                & (r - b > 60)).sum())
    print(f"  [{tag}] 区域({x0},{y0})-({x1},{y1}) {x1-x0}x{y1-y0} "
          f"红={red} 绿={green} 粉={pink} 红绿合计={red+green}")


# 面板应在的区域: 主窗顶部按钮行(按第二张图比例 x 10%-62%, y 3-42px)
measure("面板行", left + int(w * 0.10), top + 3,
        left + int(w * 0.62), top + 42)
# 对照区: 面板行下方的工具栏(无大色块)
measure("下方工具栏对照", left + int(w * 0.10), top + 46,
        left + int(w * 0.62), top + 80)

print("\n-- 模拟激活点击(右侧图区, 应无害) 后复测 --")
real_click(right - 190, min(top + 500, bottom - 120))
measure("点击后面板行", left + int(w * 0.10), top + 3,
        left + int(w * 0.62), top + 42)
