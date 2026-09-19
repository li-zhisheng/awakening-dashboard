"""盘中实测: 验证用户自定义快捷键(F1买/F3卖)全链路。

用法(仅交易时段内有效, 风控会自动拦截非时段):
    python trade_hotkey_test.py buy 601288      # F1 最新价买25%
    python trade_hotkey_test.py buy 601288 -v ask1   # F2 卖一价买25%(极端)
    python trade_hotkey_test.py sell 600000     # F3 最新价清仓
    python trade_hotkey_test.py sell 600000 -v bid1  # F4 买一价核卖

注意:
- A股T+1: 当日买入的股票次日才能卖, sell测试需账户已有可用持仓
- 判定说明:
    ok+pending   -> 按键生效, 委托已挂单未成交(正常)
    ok+filled    -> 按键生效且已成交
    uncertain    -> 回查异常(如拷贝验证码), 需人工打开xiadan看当日委托
    失败+超时    -> 按键未生效(需检查键位配置/页面状态)
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main():
    from config import load_config
    from trader.risk_control import RiskController
    from trader.hotkey_trader import HotkeyTrader

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["buy", "sell"])
    ap.add_argument("code", help="6位证券代码")
    ap.add_argument("-v", "--variant", default="latest",
                    choices=["latest", "ask1", "bid1"],
                    help="buy: latest=F1/ask1=F2; sell: latest=F3/bid1=F4")
    a = ap.parse_args()

    cfg = load_config("config.yaml")
    risk = RiskController(cfg.risk, cfg.project_root)
    t = HotkeyTrader(cfg, risk)

    action = a.action.upper()
    if action == "BUY":
        cfg.hotkey.buy_variant = a.variant
    else:
        cfg.hotkey.sell_variant = a.variant

    print(f"执行: {action} {a.code} variant={a.variant}")
    r = t.execute_order(a.code, action)

    print("\n===== 结果判定 =====")
    if not r.get("ok"):
        if "非交易时段" in str(r.get("error", "")):
            print(f"[被风控拦截] {r['error']}")
            print("请在交易时段(工作日 09:30-11:30 / 13:00-15:00)运行")
        elif r.get("uncertain"):
            print(f"[不确定] {r['error']}")
            print("请人工打开xiadan'查询->当日委托'确认是否已下单")
        else:
            print(f"[失败] {r.get('error')}")
            if "未生效" in str(r.get("error", "")):
                print("排查: 1)键位配置是否与客户端一致 2)行情端是否在个股页 "
                      "3)键盘精灵是否被占用")
    else:
        status = r.get("status")
        no = r.get("entrust_no")
        price = r.get("filled_price")
        if status == "pending":
            print(f"[成功] 按键生效, 委托已挂单未成交 合同{no} 委托价{price}")
        elif status in ("filled", "partial"):
            print(f"[成功] 按键生效, 已成交({status}) 合同{no} 价格{price}")
        else:
            print(f"[成功] {r}")
    print(f"原始返回: {r}")


if __name__ == "__main__":
    main()
