# -*- coding: utf-8 -*-
"""E13: 云同步跳页行为实测 (盘后安全)。

实验: 云端自选列表 +1只(600000) -> 采样手机页面状态 -> 恢复删除 -> 再采样。
记录: 是否跳页/跳页延迟/跳页后页面形态(列表页or行情页), 为平滑过渡方案提供依据。
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config

cfg = load_config(r"d:\Awakening\config.yaml")
adb = cfg.device.adb_path
if adb:
    exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

from adb.control import UIController, center_of
from adb.device import Device
from ths.navigator import Navigator
from ths.watchlist import CloudWatchlist

device = Device(cfg.device.serial)
control = UIController(device)
nav = Navigator(control, cfg, {})


def sample(tag):
    """采样当前页面关键状态。"""
    try:
        xml = control.dump_ui()
    except Exception as e:
        print(f"  [{tag}] dump失败: {e}")
        return
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        print(f"  [{tag}] XML解析失败")
        return
    kline = kline_sel = None
    has_next = False
    rows = 0
    title = ""
    for node in root.iter("node"):
        t = (node.get("text") or "").strip()
        rid = node.get("resource-id") or ""
        if t == "日K" and kline is None:
            kline = True
            kline_sel = (node.get("selected") or "").lower() == "true"
        if rid.endswith("al_rightbutton"):
            has_next = True
        if rid.endswith("fixed_column") and "#" in (node.get("content-desc") or ""):
            rows += 1
        if "navi_title_text" in rid and t and not title:
            title = t
    state = (f"日K={'选中' if kline_sel else ('存在未选中' if kline else '无')} "
             f"'>'箭头={has_next} 列表行={rows} 标题={title or '-'}")
    print(f"  [{tag}] {state}")


with open(os.path.join(cfg.project_root, cfg.resolve(cfg.watchlist.cookie_file)),
          "r", encoding="utf-8") as f:
    cookie = f.read().strip()
wl = CloudWatchlist(cookie, timeout=cfg.watchlist.request_timeout)
cur = [c for c, _ in wl.list_self()]
print(f"云端自选当前{len(cur)}只")

print("== 阶段0: 初始页面状态 ==")
sample("t=0")

print("== 阶段1: 云端+600000, 轮询手机页面20s ==")
wl.sync(cur + ["600000"])
t0 = time.time()
for i in range(5):
    time.sleep(4)
    sample(f"t={time.time()-t0:.0f}s")

print("== 阶段2: 恢复(删除600000), 轮询手机页面20s ==")
wl.sync(cur)
t0 = time.time()
for i in range(5):
    time.sleep(4)
    sample(f"t={time.time()-t0:.0f}s")
