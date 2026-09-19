# -*- coding: utf-8 -*-
"""E5: 枚举hexin的真实Edit/可聚焦控件, 寻找确定性输入通道。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pywinauto


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
print(f"主窗: '{win.window_text()}'")

print("\n== 所有Edit/ComboBox类子控件 ==")
found = []
for w in win.descendants():
    try:
        cls = w.class_name()
        if cls in ("Edit", "ComboBox", "ComboBoxEx32", "RichEdit", "RichEdit20W"):
            r = w.rectangle()
            visible = w.is_visible()
            found.append((cls, w.element_info.control_id, visible,
                          (r.left, r.top, r.right, r.bottom),
                          (w.window_text() or "")[:30]))
    except Exception:
        pass
for f in found:
    print(f"  [{f[0]}] id={f[1]} vis={f[2]} rect={f[3]} text='{f[4]}'")
if not found:
    print("  (无Edit类控件 - 全自绘UI)")

print("\n== 顶层子窗口类名分布 ==")
from collections import Counter
cnt = Counter()
for w in win.descendants():
    try:
        cnt[w.class_name()] += 1
    except Exception:
        pass
for cls, n in cnt.most_common(15):
    print(f"  {cls}: {n}")
