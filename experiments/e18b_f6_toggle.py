"""e18b: 验证F6浮层toggle行为与清理方式。

步骤: goto 600227 -> F6开浮层(截屏1) -> 再按F6(截屏2看是否关闭)
-> 若未关则按ESC(截屏3看效果, 确认ESC是否把页面退回列表)
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from config import load_config
from trader.risk_control import RiskController
from trader.hotkey_trader import HotkeyTrader
from trader.pcwin import grab_screen, dpi_scale

CODE = sys.argv[1] if len(sys.argv) > 1 else "600227"

cfg = load_config("config.yaml")
t = HotkeyTrader(cfg, RiskController(cfg.risk, cfg.project_root))
t._connect_hexin()
t._goto_stock(CODE)

out = cfg.resolve(cfg.paths.logs_dir)
s = dpi_scale()
r = t._hexin_win.rectangle()
w = int(r.right * s) + 20
h = int(r.bottom * s) + 20

win = t._hexin_win


def snap(tag):
    p = os.path.join(out, f"e18b_{tag}.png")
    grab_screen(0, 0, w, h).save(p)
    print("saved:", p)


win.set_focus()
time.sleep(0.3)
win.type_keys("{F6}")
time.sleep(1.2)
snap("1_open")

win.type_keys("{F6}")
time.sleep(1.0)
snap("2_toggle")

win.type_keys("{ESC}")
time.sleep(1.0)
snap("3_esc")
print("done")
