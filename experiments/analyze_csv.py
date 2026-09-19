# -*- coding: utf-8 -*-
"""分析pipeline CSV: 状态分布、异常行、扫描顺序校验。"""
import collections
import csv
import sys

sys.stdout.reconfigure(encoding="utf-8")

rows = list(csv.DictReader(
    open(r"d:\Awakening\logs\pipeline_20260908_204953.csv", encoding="utf-8-sig")))
print("总行数:", len(rows))
print("字段:", list(rows[0].keys()))
st = collections.Counter(r["status"] for r in rows)
sg = collections.Counter(r["signal"] for r in rows)
print("status分布:", dict(st))
print("signal分布:", dict(sg))
print()
print("前10行:")
for r in rows[:10]:
    print(f"  {r['stock_code']} {r['signal']} {r['status']} "
          f"{r['elapsed_time']}s err={r['error'][:60]}")
print()
errs = [r for r in rows if r["error"]]
print("带error的行数:", len(errs))
for r in errs[:10]:
    print(f"  {r['stock_code']} {r['signal']} {r['status']} {r['error'][:90]}")
print()
print("多头行(扫描序):")
for i, r in enumerate(rows, 1):
    if r["signal"] == "LONG":
        print(f"  #{i} {r['stock_code']} {r['elapsed_time']}s")
