# -*- coding: utf-8 -*-
"""E2d: ctypes vs pywin32 输入对照 + 时间序列抖动检测。"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import ctypes
import win32api

u32 = ctypes.windll.user32

print("== 同进程同时刻对照 x5 ==")
for i in range(5):
    r_ct = None
    try:
        ok = u32.SetCursorPos(600, 600)
        r_ct = f"ok={ok}"
    except Exception as e:
        r_ct = f"EXC {e}"
    try:
        win32api.SetCursorPos(600, 600)
        r_pw = "ok"
    except Exception as e:
        r_pw = f"{e}"
    print(f"  [{i}] ctypes: {r_ct} | pywin32: {r_pw}")
    time.sleep(1.5)

print("\n== 桌面状态序列 (15s) ==")
for i in range(5):
    h = u32.OpenInputDesktop(0, False, 0x02000000)
    name = "<无法打开>"
    if h:
        buf = ctypes.create_unicode_buffer(64)
        need = ctypes.c_uint()
        if u32.GetUserObjectInformationW(h, 2, buf, 64, ctypes.byref(need)):
            name = buf.value
        u32.CloseDesktop(h)
    fg = ctypes.windll.user32.GetForegroundWindow()
    print(f"  [{i}] 输入桌面={name} 前台hwnd={fg}")
    time.sleep(3)
