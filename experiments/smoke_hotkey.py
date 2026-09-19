"""冒烟测试(只读): 验证hotkey链路两端可用性, 不发下单键。

1. hexin行情端连接 + 键盘精灵定位股票( harmless, 只切行情页 )
2. xiadan交易端连接 + 当日委托/资金读取
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")


def main():
    from config import load_config
    from trader.hotkey_trader import HotkeyTrader

    cfg = load_config("config.yaml")

    class NoRisk:
        def pre_check(self, *a, **k):
            from trader.risk_control import RiskResult
            return RiskResult(ok=True)

        def record_order(self, *a, **k):
            pass

    t = HotkeyTrader(cfg, NoRisk())

    print("[1] 连接行情端hexin ...")
    t._connect_hexin()
    print(f"    OK: '{t._hexin_win.window_text()}'")

    print("[2] 键盘精灵定位 601288 (只切行情页, 不下单) ...")
    t._goto_stock("601288")
    print(f"    OK: 已发送 ESC->601288->ENTER, 等待{cfg.hotkey.goto_settle_seconds}s")

    print("[3] 连接交易端xiadan + 读取当日委托/资金 ...")
    t._connect_xiadan()
    ents = t._td_user.today_entrusts or []
    print(f"    当日委托 {len(ents)} 条")
    for e in ents[:5]:
        print(f"      {e.get('证券代码')} {e.get('证券名称')} {e.get('操作')} "
              f"{e.get('委托数量')}股@{e.get('委托价格')} 成交{e.get('成交数量')} "
              f"[{e.get('状态','')}] 合同{e.get('合同编号')}")
    bal = t._td_user.balance or {}
    print(f"    可用资金: {bal.get('可用金额')}  总资产: {bal.get('总资产')}")

    print("\n冒烟测试通过: 行情端定位+交易端回查 两端就绪")


if __name__ == "__main__":
    main()
