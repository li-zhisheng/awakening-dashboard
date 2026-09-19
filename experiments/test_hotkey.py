"""HotkeyTrader离线测试: mock行情窗+xiadan, 验证快捷键下单与委托回查逻辑。

T1 键位选择: BUY默认半仓, SELL全仓
T2 回查命中-挂单未成交(pending): ok=True status=pending
T3 回查命中-全部成交(filled) / 部分成交(partial)
T4 超时无新委托(按键未生效): ok=False
T5 回查读取异常(验证码): uncertain=True
T6 execute_order全链路(mock): 风控通过->定位->发键->回查
T7 execute_order风控拒绝: 不连客户端
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")


def make_trader(entrusts_seq=None, entrusts_exc=None, hexin_keys=None):
    """构造绕过连接的HotkeyTrader。
    entrusts_seq: today_entrusts 每次返回值列表(轮询用)
    entrusts_exc: 读取时抛的异常
    hexin_keys: 记录发送的按键
    """
    from config import AppConfig
    from trader.hotkey_trader import HotkeyTrader

    cfg = AppConfig()
    cfg.hotkey.verify_timeout = 3.0
    cfg.hotkey.verify_interval = 0.2
    cfg.hotkey.goto_settle_seconds = 0.0
    cfg.risk.enable = False          # 风控单独测

    class FakeRisk:
        def pre_check(self, *a, **k):
            from trader.risk_control import RiskResult
            return RiskResult(ok=True)

        def record_order(self, *a, **k):
            pass

    t = HotkeyTrader(cfg, FakeRisk())

    class FakeTD:
        def __init__(self):
            self.calls = 0
        @property
        def today_entrusts(self):
            self.calls += 1
            if entrusts_exc is not None:
                raise entrusts_exc
            if entrusts_seq is None:
                return []
            return entrusts_seq[min(self.calls - 1, len(entrusts_seq) - 1)]

    t._td_user = FakeTD()
    t._td_connected = True

    class FakeWin:
        def __init__(self):
            self.sent = []
            self.focused = 0
        def set_focus(self):
            self.focused += 1
        def type_keys(self, keys):
            self.sent.append(keys)
    win = FakeWin()
    t._hexin_win = win
    t._hexin_app = object()
    return t, win


def E(cid, code, op, qty=100, filled=0, price=9.24):
    return {"合同编号": cid, "证券代码": code, "操作": op,
            "委托数量": qty, "成交数量": filled, "委托价格": price}


def main():
    from trader.hotkey_trader import HotkeyTrader

    # T1 键位选择
    t, _ = make_trader()
    assert t._pick_key("BUY") == "{F1}", "BUY默认最新价F1"
    t.hk.buy_variant = "ask1"
    assert t._pick_key("BUY") == "{F2}", "BUY极端应F2卖一价"
    t.hk.buy_variant = "latest"
    assert t._pick_key("SELL") == "{F3}", "SELL默认最新价清仓F3"
    t.hk.sell_variant = "bid1"
    assert t._pick_key("SELL") == "{F4}", "SELL核卖应F4买一价"
    t.hk.sell_variant = "latest"
    print("T1 键位选择: BUY=F1/极端F2  SELL=F3/核卖F4  OK")

    # T2 挂单未成交
    t, _ = make_trader(entrusts_seq=[
        [E("1", "300418", "卖出", 2700, 0)],          # 旧单
        [E("1", "300418", "卖出", 2700, 0),
         E("2", "600000", "买入", 100, 0)],           # 新买入单
    ])
    ok, r = t._verify("600000", "BUY", {"1"})
    assert ok and r["status"] == "pending" and r["entrust_no"] == "2", f"T2 {r}"
    print(f"T2 挂单未成交: ok={ok} status={r['status']} 合同{r['entrust_no']}  OK")

    # T3 全成/部分
    t, _ = make_trader(entrusts_seq=[[
        E("9", "300418", "卖出", 2700, 2700, 46.60)]])
    ok, r = t._verify("300418", "SELL", set())
    assert ok and r["status"] == "filled" and r["filled_price"] == 46.60, f"T3a {r}"
    t2, _ = make_trader(entrusts_seq=[[
        E("9", "300418", "卖出", 2700, 1300, 46.60)]])
    ok, r = t2._verify("300418", "SELL", set())
    assert ok and r["status"] == "partial", f"T3b {r}"
    print("T3 全部成交=filled / 部分成交=partial  OK")

    # T4 按键未生效(始终无新单)
    t, _ = make_trader(entrusts_seq=[[E("1", "300418", "卖出", 2700, 0)]])
    t.hk.verify_timeout = 0.6
    t.hk.verify_interval = 0.2
    ok, r = t._verify("600000", "BUY", {"1"})
    assert not ok and "未生效" in r["error"], f"T4 {r}"
    print(f"T4 超时无新委托: ok={ok} error='{r['error']}'  OK")

    # T5 回查异常(验证码)
    t, _ = make_trader(entrusts_exc=RuntimeError("拷贝验证码"))
    ok, r = t._verify("600000", "BUY", set())
    assert not ok and r.get("uncertain") and "人工确认" in r["error"], f"T5 {r}"
    print(f"T5 回查异常: uncertain={r['uncertain']}  OK")

    # T6 execute_order全链路
    t, win = make_trader(entrusts_seq=[
        [],
        [E("7", "600000", "买入", 100, 0)],
    ])
    r = t.execute_order("600000", "BUY")
    assert r["ok"] and r["status"] == "pending" and r["entrust_no"] == "7", f"T6 {r}"
    # 按键序列: ESC, 6,0,0,0,0,0, ENTER, F1
    assert "{ESC}" in win.sent and "{ENTER}" in win.sent and "{F1}" in win.sent, \
        f"T6 keys: {win.sent}"
    assert "600000" in "".join(k for k in win.sent if not k.startswith("{")), \
        f"T6 代码未输入: {win.sent}"
    print(f"T6 全链路: {win.sent} -> ok={r['ok']} 合同{r['entrust_no']}  OK")

    # T7 风控拒绝不连接
    from config import AppConfig
    from trader.risk_control import RiskResult
    cfg7 = AppConfig()
    cfg7.risk.enable = False
    class BlockRisk:
        def pre_check(self, *a, **k):
            return RiskResult(False, "非交易时段")
        def record_order(self, *a, **k):
            pass
    t7 = HotkeyTrader(cfg7, BlockRisk())
    r = t7.execute_order("600000", "BUY")
    assert not r["ok"] and "非交易时段" in r["error"] and t7._hexin_win is None
    print("T7 风控拒绝: 不连接客户端  OK")

    print("\n全部通过 (7/7)")


if __name__ == "__main__":
    main()
