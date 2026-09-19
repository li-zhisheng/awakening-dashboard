# -*- coding: utf-8 -*-
"""E9: 精灵高亮行颜色检测 + 双击导航(最终方案验证)。"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

import cv2
import numpy as np
import pywinauto
import win32api
import win32con
from PIL import ImageGrab

OUT = r"d:\Awakening\logs\e9"
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


def find_highlight_bar(screen_left, screen_top, region_img):
    """在精灵区域图里找高亮蓝条, 返回屏幕坐标中心(或None)。

    高亮条特征: 整行连续的蓝色块(B显著高于R/G), 行宽>100px。
    """
    img = region_img
    b, g, r = img[:, :, 0].astype(int), img[:, :, 1].astype(int), \
        img[:, :, 2].astype(int)
    mask = (b > 140) & (b - r > 40) & (b - g > 25)
    best = None
    for y in range(img.shape[0]):
        xs = np.where(mask[y])[0]
        if len(xs) > 100:
            run_start, runs = xs[0], []
            prev = xs[0]
            for x in xs[1:]:
                if x != prev + 1:
                    runs.append((run_start, prev))
                    run_start = x
                prev = x
            runs.append((run_start, prev))
            for s, e in runs:
                if e - s > 100:
                    if best is None or (e - s) > best[2]:
                        best = (y, (s + e) // 2, e - s)
    if best is None:
        return None
    y, cx, w = best
    return (screen_left + cx, screen_top + y, w)


app = pywinauto.Application().connect(process=find_pid("hexin.exe"), timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
print(f"主窗: '{win.window_text()}'")

from pywinauto import findwindows
from pywinauto.controls.hwndwrapper import HwndWrapper
elem = findwindows.find_element(control_id=62267, class_name="Edit",
                                top_level_only=False)
edit = HwndWrapper(elem.handle)
er = edit.rectangle()
ecx, ecy = (er.left + er.right) // 2, (er.top + er.bottom) // 2

for target in ("601288", "600127"):
    print(f"\n===== 导航 {target} =====")
    real_click(ecx, ecy)
    time.sleep(0.4)
    # 清空已有输入
    win.type_keys("^a{DEL}")
    time.sleep(0.3)
    win.type_keys(target, pause=0.1)
    time.sleep(1.5)

    # 截精灵区域: Edit上方区域 (窗口坐标)
    wr = win.rectangle()
    reg_l = max(0, ecx - wr.left - 260)
    reg_t = max(0, ecy - wr.top - 330)
    reg_r = min(wr.right - wr.left, ecx - wr.left + 300)
    reg_b = ecy - wr.top
    img = ImageGrab.grab(bbox=(wr.left + reg_l, wr.top + reg_t,
                               wr.left + reg_r, wr.top + reg_b))
    arr = np.array(img)[:, :, ::-1]  # RGB->BGR
    bar = find_highlight_bar(wr.left + reg_l, wr.top + reg_t, arr)
    print(f"  高亮条: {bar}")
    if bar is None:
        # 保存区域图供调色
        cv2.imwrite(os.path.join(OUT, f"region_{target}.png"),
                    np.array(img)[:, :, ::-1])
        print(f"  未找到高亮条, 区域图已存")
        continue
    hx, hy, w = bar
    real_click(hx, hy, double=True)
    time.sleep(3)
    print(f"  导航后标题: '{win.window_text()}'")
    r2 = win.rectangle()
    ImageGrab.grab(bbox=(r2.left, r2.top, r2.right, r2.bottom)).save(
        os.path.join(OUT, f"after_{target}.png"))
    print(f"  [截图] after_{target}.png")
