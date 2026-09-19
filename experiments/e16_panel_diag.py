# -*- coding: utf-8 -*-
"""E16: 快捷键面板窗口诊断 - 找面板hwnd特征 + 检查xiadan弹窗残留。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import subprocess
import win32gui
import win32process
import win32con


def pids_of(name):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}",
                          "/FO", "CSV", "/NH"], capture_output=True,
                         text=True).stdout
    pids = set()
    for line in out.strip().splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == name.lower():
            pids.add(int(parts[1]))
    return pids


def top_windows(pid_acc):
    acc = []

    def handler(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid in pid_acc:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            acc.append((hwnd, cls, title, rect, vis))

    win32gui.EnumWindows(handler, None)
    return acc


hexin_pids = pids_of("hexin.exe")
print(f"hexin PIDs: {hexin_pids}")
wins = top_windows(hexin_pids)
print(f"hexin顶层窗口数: {len(wins)}")
for hwnd, cls, title, rect, vis in wins:
    if vis or title:   # 可见的或带标题的都打印
        print(f"  hwnd={hwnd:#010x} [{ 'V' if vis else 'H'}] cls={cls!r} "
              f"title={title!r} rect={rect}")

# 面板特征猜测: 位置在主窗顶部(第二张图面板y约0-44), 宽度较大
print("\n-- 可见且宽度>500的窗口(面板候选) --")
for hwnd, cls, title, rect, vis in wins:
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    if vis and w > 500:
        print(f"  hwnd={hwnd:#010x} cls={cls!r} title={title!r} "
              f"{w}x{h} @({rect[0]},{rect[1]})")

# xiadan 弹窗残留检查(验证码"提示"对话框)
xd_pids = pids_of("xiadan.exe")
print(f"\nxiadan PIDs: {xd_pids}")
for hwnd, cls, title, rect, vis in top_windows(xd_pids):
    if vis:
        print(f"  hwnd={hwnd:#010x} [{ 'V' if vis else 'H'}] cls={cls!r} "
              f"title={title!r} rect={rect}")
print("done")
