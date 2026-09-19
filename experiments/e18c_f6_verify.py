"""e18c: F6持仓浮层检测函数端到端验证。

用例:
1. 负样本: goto个股页, 不按F6(无浮层) -> 应判定 False
2. 正样本: 按F6(浮层"当前股票无持仓", 账户现无持仓股) -> 应判定 True
3. 清理: ESC关闭浮层回列表, 再按F6恢复个股页继续下一用例
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config
from trader.risk_control import RiskController
from trader.hotkey_trader import HotkeyTrader
from trader.pcwin import f6_no_position

cfg = load_config("config.yaml")
t = HotkeyTrader(cfg, RiskController(cfg.risk, cfg.project_root))
t._connect_hexin()
win = t._hexin_win

for code in ["600227", "600127"]:
    t._goto_stock(code)

    # 负样本: 无浮层
    ok0, s0, _ = f6_no_position(win.rectangle())
    print(f"[{code}] 无浮层(应False): 判定={ok0} 得分={s0:.3f}")

    # 正样本: F6浮层
    win.set_focus()
    time.sleep(0.3)
    win.type_keys("{F6}")
    time.sleep(1.2)
    ok1, s1, _ = f6_no_position(win.rectangle())
    print(f"[{code}] F6浮层(应True):  判定={ok1} 得分={s1:.3f}")

    # 清理浮层(ESC回列表), goto会重新进个股页
    win.type_keys("{ESC}")
    time.sleep(0.8)

print("done")
