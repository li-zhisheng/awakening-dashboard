# -*- coding: utf-8 -*-
"""E2: hexin F12登录流程诊断 + 盘后F1下单行为观察。"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

OUT = r"d:\Awakening\logs\e2"
os.makedirs(OUT, exist_ok=True)

import pywinauto
from PIL import ImageGrab


def shot_win(win, name):
    try:
        win.set_focus()
        time.sleep(0.5)
        r = win.rectangle()
        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
        p = os.path.join(OUT, f"{name}.png")
        img.save(p)
        print(f"  [截图] {p} rect=({r.left},{r.top},{r.right},{r.bottom})")
    except Exception as e:
        print(f"  [截图失败 {name}] {e}")


def find_pid(image_name):
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}",
         "/FO", "CSV", "/NH"],
        capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split('","')
        if len(parts) >= 2 and image_name.lower() in parts[0].lower():
            return int(parts[1].strip('"'))
    return None


def xiadan_win(app=None):
    """找xiadan主窗口, 返回(pywinauto窗口或None)。"""
    try:
        if app is None:
            pid = find_pid("xiadan.exe")
            if not pid:
                return None
            app = pywinauto.Application().connect(process=pid, timeout=3)
        return app.window(title="网上股票交易系统5.0")
    except Exception:
        return None


print("=== 1. hexin当前状态 ===")
pid = find_pid("hexin.exe")
app = pywinauto.Application().connect(process=pid, timeout=5)
win = None
for w in app.windows(visible_only=True):
    if "同花顺" in (w.window_text() or ""):
        win = w
        break
if win is None:
    win = app.top_window()
print(f"  hexin主窗: '{win.window_text()}'")
shot_win(win, "10_hexin_before")

print("\n=== 2. 按F12 (应弹出交易登录/主窗, 自动登录) ===")
win.set_focus()
time.sleep(0.5)
win.type_keys("{F12}")
print("  已按F12, 等8s自动登录...")
time.sleep(8)

xd = xiadan_win()
if xd is not None:
    try:
        print(f"  xiadan窗口出现: '{xd.window_text()}' visible={xd.is_visible()}")
        shot_win(xd, "11_xiadan_after_f12")
    except Exception as e:
        print(f"  xiadan窗口异常: {e}")
else:
    print("  xiadan窗口未出现!")
    shot_win(win, "11_hexin_no_xiadan")

print("\n=== 3. 关闭xiadan窗口 ===")
if xd is not None:
    try:
        xd.close()
        time.sleep(2)
        print(f"  已关闭, 仍存在={xiadan_win() is not None}")
    except Exception as e:
        print(f"  关闭异常: {e}")
shot_win(win, "12_hexin_after_close")

print("\n=== 4. hexin上F1试下单(601288) 观察盘后行为 ===")
win.set_focus()
time.sleep(0.4)
win.type_keys("{ESC}")
time.sleep(0.3)
win.type_keys("601288")
time.sleep(1.2)
win.type_keys("{ENTER}")
time.sleep(2.5)
shot_win(win, "13_goto_601288")
win.type_keys("{F1}")
time.sleep(3)
shot_win(win, "14_after_f1")

print("\n=== 5. 重开xiadan查当日委托 ===")
pid2 = find_pid("xiadan.exe")
print(f"  xiadan进程: {pid2}")
if pid2 is None:
    exe_path = None
    try:
        from config import load_config
        cfg = load_config(r"d:\Awakening\config.yaml")
        exe_path = cfg.easytrader.exe_path
    except Exception:
        pass
    print(f"  重新启动xiadan: {exe_path}")
    if exe_path:
        subprocess.Popen([exe_path])
        time.sleep(6)
xd = xiadan_win()
if xd is not None:
    shot_win(xd, "15_xiadan_reopened")
else:
    print("  xiadan重启失败!")
