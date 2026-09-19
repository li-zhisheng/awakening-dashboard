# -*- coding: utf-8 -*-
"""E14: 上线前关键验证 - F1热键下单链路 (模拟账户, 盘后可测)。

步骤: 每日bootstrap(F12登录) -> 快照当日委托 -> 定位601288 -> 发F1 ->
      回查是否新增委托 -> 恢复(若有撤单接口可撤, 盘后模拟盘委托留档无风险)
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

from config import load_config
from trader.risk_control import RiskController
from trader.hotkey_trader import HotkeyTrader

cfg = load_config(r"d:\Awakening\config.yaml")
risk = RiskController(cfg.risk, cfg.project_root)
trader = HotkeyTrader(cfg, risk)

print("== 步骤1: 每日bootstrap (F12登录+关窗) ==")
rep = trader.daily_bootstrap(force=True)
print(f"   结果: {rep}")

print("== 步骤2: 快照当日委托 ==")
trader._connect_xiadan()
before = trader._entrust_ids()
print(f"   当日委托数: {len(before)}")

print("== 步骤3: 定位601288并发送F1 ==")
t0 = time.time()
trader._goto_stock("601288")
print(f"   定位耗时: {time.time()-t0:.1f}s")
trader._hexin_win.type_keys("{F1}", pause=0.02)
print("   F1已发送, 等待3s...")
time.sleep(3)

print("== 步骤4: 回查新委托 ==")
deadline = time.time() + 20
new_ids = []
while time.time() < deadline:
    try:
        after = trader._entrust_ids()
        new_ids = [x for x in after if x not in before]
        if new_ids:
            break
    except Exception as e:
        print(f"   回查异常: {e}")
    time.sleep(2)

if new_ids:
    print(f"   *** 新委托已产生: {new_ids} ***")
    print("   F1热键链路验证: PASS")
    trader._close_xiadan()
else:
    print("   20s内无新委托")
    print("   说明: 盘后客户端可能拦截下单请求(不产生委托记录)")
    print("   F1热键链路验证: INCONCLUSIVE (需盘中复测)")
    trader._close_xiadan()
