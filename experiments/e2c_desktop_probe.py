# -*- coding: utf-8 -*-
"""E2c简化: 子进程对照 + SetThreadDesktop。"""
import ctypes
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
u32 = ctypes.windll.user32

print("== A. 本进程 SetCursorPos ==")
try:
    u32.SetCursorPos(500, 500)
    print("  成功")
except Exception as e:
    print(f"  失败: {e}")

print("\n== B. 子进程 SetCursorPos ==")
child = ("import ctypes\n"
         "u32 = ctypes.windll.user32\n"
         "try:\n"
         "    u32.SetCursorPos(500, 500)\n"
         "    print('child OK')\n"
         "except Exception as e:\n"
         "    print('child FAIL', e)\n")
r = subprocess.run([sys.executable, "-c", child],
                   capture_output=True, text=True, timeout=20)
print(" ", r.stdout.strip(), r.stderr.strip()[:120])
