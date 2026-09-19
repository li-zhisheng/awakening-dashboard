"""e18: 实测行情端F6"查个股持仓"浮层样式。

用户键位: F6=查当前个股持仓(行情端浮层, 不弹xiadan); F7=查当日委托
(会弹交易系统页面, 弃用)。买卖后用F6持仓变化判定交易是否成功,
本脚本截取F6前/后全屏, 为浮层定位与数字识别提供样本。
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config
from trader.risk_control import RiskController
from trader.hotkey_trader import HotkeyTrader

CODE = sys.argv[1] if len(sys.argv) > 1 else "600227"

cfg = load_config("config.yaml")
t = HotkeyTrader(cfg, RiskController(cfg.risk, cfg.project_root))
t._connect_hexin()
t._goto_stock(CODE)

out = cfg.resolve(cfg.paths.logs_dir)
os.makedirs(out, exist_ok=True)
from trader.pcwin import grab_screen
r = t._hexin_win.rectangle()
from trader.pcwin import dpi_scale
s = dpi_scale()
bbox = (0, 0, int(r.right * s) + 20, int(r.bottom * s) + 20)

p_before = os.path.join(out, "e18_before.png")
grab_screen(0, 0, bbox[2], bbox[3]).save(p_before)

win = t._hexin_win
win.set_focus()
time.sleep(0.3)
win.type_keys("{F6}")          # 用户自定义: 查当前个股持仓浮层
time.sleep(1.2)

p_after = os.path.join(out, "e18_after.png")
grab_screen(0, 0, bbox[2], bbox[3]).save(p_after)
print("已保存:", p_before, p_after)
