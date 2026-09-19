"""trader: 下单执行接口。

PaperTrader: 纸面交易(只更新持仓+告警, 不碰真实订单), 同样经过完整
风控预检(kill_switch/每股每日限买/交易时段/日下单数), 与auto模式行为
一致——盘后测试不会留下"违反风控"的模拟持仓。
真实下单由 HotkeyTrader(auto模式) 承担, 见 trader/hotkey_trader.py。
"""
import logging

log = logging.getLogger("trader")


class PaperTrader:
    """纸面交易: 不产生真实订单, 由决策层直接更新持仓文件。

    risk: RiskController, 下单前预检+下单后记录(每日买入名额)。
    传None则跳过风控(仅单元测试用)。
    """

    def __init__(self, risk=None):
        self.risk = risk

    def execute_order(self, code: str, action: str, price: float = 0.0,
                      qty: int = 0, name: str = "",
                      queue_only: bool = False,
                      closing_auction: bool = False) -> dict:
        if self.risk is not None:
            pre = self.risk.pre_check(code, action, qty, queue_only)
            if not pre.ok:
                log.warning("[纸面] 风控拒绝: %s %s -> %s", action, code,
                            pre.reason)
                return {"ok": False, "error": pre.reason, "mode": "paper",
                        "risk_block": True}
        log.info("[纸面] %s %s price=%s qty=%s%s", action, code, price, qty,
                 "(尾盘竞价顶格)" if closing_auction
                 else "(排队单)" if queue_only else "")
        if self.risk is not None:
            # 纸面单视为即时成交, 台账直接记filled(与record_order内BUY逻辑
            # 幂等), 使决策层二次挂单价格条件在paper模式下同样可演练
            self.risk.record_attempt(code, action, price, "filled")
            self.risk.record_order(code, action, ok=True, price=price)
        return {"ok": True, "filled_price": price, "mode": "paper"}
