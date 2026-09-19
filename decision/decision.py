"""决策层: 信号 x 持仓状态 -> 动作。

规则 (用户定义):
- 未持仓 + 多 -> BUY (买入, 成为持仓); 已满仓(max_positions只)时仅记录
  信号出现时间+现价(full_signal事件+当日一次告警), 不执行交易逻辑
- 持仓   + 空 -> SELL (卖出清仓)
- 持仓   + 多/无 -> HOLD (买入当日为多, 次日起只可能是无或空; 只要不出现空就继续持有)
- 未持仓 + 空/无 -> IGNORE (不操作)
- 持仓股出现未知/异常/断连 -> ALERT (绝不基于未知自动卖出)
- 未知永不允许降级为无/忽略
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from models.audit import get_event_log
from models.positions import PositionStore
from models.result import Signal, StockResult, StockStatus

log = logging.getLogger("decision")


def _new_trace_id() -> str:
    """单次下单链路关联ID: 串起 order_request/order_result/order_failed,
    与风控台账client_order_id配合实现端到端对账(R1, 2026-09-15)。"""
    return f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


class ActionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    ALERT = "ALERT"
    HOLD = "HOLD"        # 持仓+多: 继续持有
    IGNORE = "IGNORE"    # 无信号或未持仓+空/无


@dataclass
class Action:
    type: ActionType
    code: str
    signal: str
    reason: str
    source: str = ""     # goto / cycle / position_loop
    qty: int = 0         # 下单数量, 0=用cfg.risk.default_qty
    # 信号帧捕获时刻(time.time秒): 随交易事件落盘, 事后精确计算
    # Signal→下单请求→发键→成交全链路延迟, 无需按代码+时间窗模糊拼接
    signal_ts: float = 0.0
    # 排队单: 跌停卖单/尾盘竞价单, 挂出后不做2分钟自动撤单(撤不掉也不该撤)
    queue_only: bool = False
    # 尾盘集合竞价结算单(14:57-15:00): 区别于盘中跌停排队卖, 热键通道据此
    # 改发"涨停价买/跌停价卖"顶格自定义键(2026-09-16用户裁定, 配置在
    # hotkey.closing_*_limit_key; 未配置回退F1/F3)。集合竞价按收盘价撮合。
    closing_auction: bool = False


class DecisionEngine:
    def __init__(self, positions: PositionStore, trader, alerts_dir: str,
                 name_map: Optional[dict] = None, mode: str = "paper",
                 default_qty: int = 100, notifier=None, universe_cfg=None,
                 max_positions: int = 4):
        self.positions = positions
        self.trader = trader
        self.alerts_dir = alerts_dir
        self.name_map = name_map or {}
        self.mode = mode  # paper / auto
        self.default_qty = default_qty
        self.notifier = notifier   # notify.Notifier, 手机推送(None=不推)
        self.universe_cfg = universe_cfg  # UniverseConfig, 买入前零成本防御
        self.max_positions = max_positions  # 分批仓位: 满仓不开新仓
        self._full_seen = {}  # 满仓多头信号当日去重: code -> 已推送日期(防每轮刷屏)
        self._limit_seen = {}  # 涨跌停特殊处置告警当日去重: "UP/DOWN:code" -> 1
        self._unknown_seen = {}  # 未持仓unknown显式事件当日去重: code -> 日期
        self.el = get_event_log(alerts_dir)  # 交易状态机审计(同一JSONL)
        self.round_id = 0   # 当前扫描轮次(由scheduler每轮注入, 随交易事件落盘)

    def set_round(self, rnd: int):
        """scheduler每轮开始时注入轮次号, 交易事件携带round便于按轮追溯。"""
        self.round_id = int(rnd or 0)

    def _tradeable_defense(self, code: str, name: str) -> Optional[str]:
        """买入前零成本防御(前置过滤已兜底, 此处双保险): 返回拒绝原因或None。"""
        u = self.universe_cfg
        if u is None or not getattr(u, "enable", True):
            return None
        if getattr(u, "main_board_only", True):
            from ths.universe_filter import is_main_board
            if not is_main_board(code):
                return "非沪深主板(防御拦截)"
        if getattr(u, "exclude_st", True):
            from ths.universe_filter import is_st
            if is_st(name):
                return "ST股(防御拦截)"
        return None

    def decide(self, r: StockResult) -> Action:
        code = r.stock_code
        name = self.name_map.get(code, "")
        held = self.positions.is_held(code)
        if r.status == StockStatus.DEVICE_LOST:
            return Action(ActionType.ALERT, code, r.signal.value,
                          "设备断开", r.detection.source)
        if r.status != StockStatus.OK:
            return self._with_alert_check(r, f"状态异常({r.status.value})")
        if r.signal == Signal.UNKNOWN:
            return self._with_alert_check(r, "识别为未知")

        if held:
            # 持仓股: 买入当日为多, 次日起信号只可能是无或空 -> 只要不出现空就持有
            if r.signal == Signal.SHORT:
                return self._decide_sell(code, name, r)
            reason = ("持仓股无空信号, 继续持有" if r.signal == Signal.NONE
                      else "持仓股出现多信号, 继续持有")
            return Action(ActionType.HOLD, code, r.signal.value, reason,
                          r.detection.source)
        # 未持仓: 只有"多"才买入, 空/无均不操作
        if r.signal == Signal.LONG:
            return self._decide_buy(code, name, r)
        return Action(ActionType.IGNORE, code, r.signal.value,
                      "未持仓且无买入口径信号", r.detection.source)

    # ---------- 买入/卖出专项决策(含涨跌停+二次挂单价格条件) ----------

    def _risk(self):
        """交易员携带的风控器(挂单尝试台账); 纸面/真实通道均有, 测试可None。"""
        return getattr(self.trader, "risk", None)

    def _snapshot(self, code: str):
        """腾讯行情快照(含涨跌停/盘口); 失败返回None(降级不阻断)。"""
        try:
            from ths.quote import realtime_quote
            return realtime_quote(code, 5.0)
        except Exception as e:
            log.warning("行情快照查询失败 %s: %s", code, e)
            return None

    def _decide_buy(self, code: str, name: str, r: StockResult) -> Action:
        """未持仓+多头: 满仓记录/防御/涨停拦截/二次挂单价格条件 → BUY。"""
        # 分批仓位: 已持满max_positions只不开新仓, 但全量检索持续进行
        if len(self.positions.codes()) >= self.max_positions:
            price_txt = self._record_full_signal(code, name, r)
            self.el.log("decision", code=code, name=name,
                        signal=Signal.LONG.value, decision="RECORD",
                        reason=f"已满仓{self.max_positions}只, 仅记录"
                               f"信号不交易({price_txt})",
                        source=r.detection.source)
            return Action(ActionType.IGNORE, code, r.signal.value,
                          f"已满仓{self.max_positions}只, 仅记录信号"
                          f"({price_txt}), 不开新仓",
                          r.detection.source)
        reject = self._tradeable_defense(code, name)
        if reject:
            self.el.log("decision", code=code, name=name,
                        signal=Signal.LONG.value, decision="IGNORE",
                        reason=f"多信号但{reject}, 不买入",
                        source=r.detection.source)
            return Action(ActionType.IGNORE, code, r.signal.value,
                          f"多信号但{reject}, 不买入", r.detection.source)
        risk = self._risk()
        att = risk.get_attempt(code, "BUY") if risk else None
        # 上笔买单仍在挂单: 绝不重复发单(防重复建仓)
        if att and att.get("status") == "pending":
            return Action(ActionType.IGNORE, code, r.signal.value,
                          "上笔买单仍在挂单中, 不重复挂单", r.detection.source)
        snap = self._snapshot(code)
        price = float(snap.get("price") or 0) if snap else 0.0
        # 停牌/临停: 无法成交, 不挂单(挂了也是废单), 当日告警一次
        if snap and snap.get("halted"):
            self._halt_alert(code, name, "LONG", buy=True)
            return Action(ActionType.IGNORE, code, r.signal.value,
                          "股票停牌/临停中, 无法买入(复牌后信号仍在再买)",
                          r.detection.source)
        # 涨停: 不排队买入, 注册封单监控等开板机会, 告警一次
        if snap and snap.get("at_limit_up"):
            self._register_limit_up(code, name, snap)
            reason = (f"涨停封板不排队买入(现价{price:.2f}), "
                      f"已监控开板/封单大减机会")
            self.el.log("decision", code=code, name=name,
                        signal=Signal.LONG.value, decision="IGNORE",
                        reason=reason, source=r.detection.source)
            return Action(ActionType.IGNORE, code, r.signal.value,
                          reason, r.detection.source)
        # 二次挂单价格条件: 上笔买单撤单后, 现价必须≤下单时价才补(不追高)
        if att and att.get("status") == "canceled" and price > 0:
            first = float(att.get("price") or 0)
            if first > 0 and price > first + 0.005:
                reason = (f"二次多头但现价{price:.2f}>下单时价"
                          f"{first:.2f}, 不追高挂单")
                self.el.log("decision", code=code, name=name,
                            signal=Signal.LONG.value, decision="IGNORE",
                            reason=reason, source=r.detection.source)
                return Action(ActionType.IGNORE, code, r.signal.value,
                              reason, r.detection.source)
        self.el.log("decision", code=code, name=name,
                    signal=Signal.LONG.value, decision="BUY",
                    reason="未持仓股出现多信号", source=r.detection.source)
        return Action(ActionType.BUY, code, r.signal.value,
                      "未持仓股出现多信号", r.detection.source,
                      signal_ts=r.capture_time)

    def _decide_sell(self, code: str, name: str, r: StockResult) -> Action:
        """持仓+空头: 跌停排队/二次挂单价格条件 → SELL。空头信号优先级最高。"""
        risk = self._risk()
        att = risk.get_attempt(code, "SELL") if risk else None
        # 上笔卖单仍在挂单(含跌停排队): 不重复发单
        if att and att.get("status") == "pending":
            return Action(ActionType.IGNORE, code, r.signal.value,
                          "上笔卖单仍在挂单中, 不重复挂单", r.detection.source)
        snap = self._snapshot(code)
        price = float(snap.get("price") or 0) if snap else 0.0
        # 停牌/临停: 卖单无法成交, 当日告警一次, 复牌后第一轮巡检即重判
        if snap and snap.get("halted"):
            self._halt_alert(code, name, "SHORT", buy=False)
            return Action(ActionType.IGNORE, code, r.signal.value,
                          "持仓股停牌/临停中, 空头信号无法卖出, 复牌立即处理",
                          r.detection.source)
        # 跌停: 必须排队卖出(不自动撤单), 注册封单监控(封单大减→撤单+人工卖)
        if snap and snap.get("at_limit_down"):
            self._register_limit_down(code, name, snap)
            self.el.log("decision", code=code, name=name,
                        signal=Signal.SHORT.value, decision="SELL",
                        reason=f"跌停封板, 挂最新价排队卖出(现价{price:.2f}),"
                               f"封单最高级别监控",
                        source=r.detection.source)
            return Action(ActionType.SELL, code, r.signal.value,
                          "跌停封板, 排队卖出+封单最高级别监控",
                          r.detection.source, signal_ts=r.capture_time,
                          queue_only=True)
        # 二次挂单价格条件: 上笔卖单撤单后, 现价必须≥下单时价才重挂(不低价卖)
        if att and att.get("status") == "canceled" and price > 0:
            first = float(att.get("price") or 0)
            if first > 0 and price < first - 0.005:
                reason = (f"二次空头但现价{price:.2f}<下单时价"
                          f"{first:.2f}, 不低价重复挂卖(等反弹/尾盘竞价)")
                self.el.log("decision", code=code, name=name,
                            signal=Signal.SHORT.value, decision="IGNORE",
                            reason=reason, source=r.detection.source)
                return Action(ActionType.IGNORE, code, r.signal.value,
                              reason, r.detection.source)
        self.el.log("decision", code=code, name=name,
                    signal=Signal.SHORT.value, decision="SELL",
                    reason="持仓股出现空信号", source=r.detection.source)
        return Action(ActionType.SELL, code, r.signal.value,
                      "持仓股出现空信号", r.detection.source,
                      signal_ts=r.capture_time)

    def _register_limit_up(self, code: str, name: str, snap: dict):
        """涨停多头: 注册开板监控+当日一次告警。"""
        watcher = getattr(self.trader, "seal_watcher", None)
        if watcher:
            try:
                watcher.watch_up(code, name, float(snap.get("seal_vol") or 0))
            except Exception as e:
                log.warning("涨停监控注册失败 %s: %s", code, e)
        key = f"UP:{code}"
        if self._limit_seen.get(key):
            return
        self._limit_seen[key] = 1
        self._alert(Action(ActionType.ALERT, code, "LONG",
                           f"涨停封板不排队买入(现价{snap.get('price'):.2f}), "
                           f"已启动封单监控: 开板或封单大减时立即告警, "
                           f"人工判断补单机会", "limit_up"),
                    level="WARNING")

    def _register_limit_down(self, code: str, name: str, snap: dict):
        """跌停卖单: 注册封单监控(大减自动撤单)+当日一次最高级别告警。"""
        watcher = getattr(self.trader, "seal_watcher", None)
        if watcher:
            try:
                watcher.watch_down(code, name,
                                   float(snap.get("seal_vol") or 0))
            except Exception as e:
                log.warning("跌停监控注册失败 %s: %s", code, e)
        key = f"DOWN:{code}"
        if self._limit_seen.get(key):
            return
        self._limit_seen[key] = 1
        self._alert(Action(ActionType.ALERT, code, "SHORT",
                           f"跌停封板, 已挂最新价排队卖出"
                           f"(现价{snap.get('price'):.2f}); 封单最高级别监控中, "
                           f"一旦封单大减/开板立即自动撤单并告警, 人工卖出",
                           "limit_down"),
                    level="CRITICAL")

    def _halt_alert(self, code: str, name: str, sig: str, buy: bool):
        """停牌/临停当日一次告警(买=WARNING可等; 持空头卖不出=CRITICAL)。"""
        key = f"HALT:{code}"
        if self._limit_seen.get(key):
            return
        self._limit_seen[key] = 1
        if buy:
            self._alert(Action(ActionType.ALERT, code, sig,
                               f"多头信号但股票停牌/临停中, 无法买入, "
                               f"未挂单; 复牌后若多头信号仍在系统会再买",
                               "halted"), level="WARNING")
        else:
            self._alert(Action(ActionType.ALERT, code, sig,
                               f"持仓股出现空头信号但股票停牌/临停中, "
                               f"卖单无法成交! 复牌后第一轮巡检将立即重判卖出, "
                               f"请人工关注复牌时间", "halted"),
                        level="CRITICAL")

    def _record_full_signal(self, code: str, name: str, r: StockResult) -> str:
        """满仓期间多头信号: 记录信号时间+现价; 当日首次发现推送一次告警。

        每次出现都写审计事件(信号时间/现价/置信度, 供事后回看), 手机推送
        每股每日仅一次(防每轮扫描刷屏)。现价走腾讯行情, 失败降级不阻断。
        返回现价文本(用于决策日志)。
        """
        price = 0.0
        try:
            from ths.quote import realtime_price
            price = realtime_price(code) or 0.0
        except Exception as e:
            log.warning("满仓信号现价查询失败 %s: %s", code, e)
        price_txt = f"现价{price:.2f}" if price > 0 else "现价查询失败"
        self.el.log("full_signal", code=code, name=name,
                    price=price, signal_time=r.capture_time,
                    confidence=round(r.detection.confidence, 3),
                    source=r.detection.source)
        today = time.strftime("%Y-%m-%d")
        if self._full_seen.get(code) != today:
            self._full_seen[code] = today
            try:
                self._alert(Action(
                    ActionType.ALERT, code, "LONG",
                    f"满仓观察: 发现多头信号({price_txt}), 已记录不交易"
                    f"(当日首次, 后续出现只记日志)", r.detection.source))
            except Exception as e:
                log.warning("满仓信号告警失败 %s: %s", code, e)
        else:
            log.info("满仓多头信号: %s %s %s (%s)", code, name, price_txt,
                     r.capture_time)
        return price_txt

    def _with_alert_check(self, r: StockResult, reason: str) -> Action:
        """异常/未知: 持仓股必须告警, 未持仓不交易但写显式事件。"""
        if self.positions.is_held(r.stock_code):
            return Action(ActionType.ALERT, r.stock_code, r.signal.value,
                          f"持仓股{reason}, 需人工确认", r.detection.source)
        # 未持仓unknown/异常: 明确"不交易", 但写显式事件(2026-09-18采纳),
        # 每股当日一次, 便于事后统计识别质量(区别于静默IGNORE)。
        code = r.stock_code
        today = time.strftime("%Y-%m-%d")
        if self._unknown_seen.get(code) != today:
            self._unknown_seen[code] = today
            self.el.log("anomaly", stage="nonheld_unknown", code=code,
                        signal=r.signal.value, source=r.detection.source,
                        reason=str(reason)[:120], round=self.round_id)
        return Action(ActionType.IGNORE, code, r.signal.value,
                      f"未持仓股{reason}(不交易,已记事件)",
                      r.detection.source)

    def execute(self, action: Action) -> Action:
        """执行动作: 一律先经trader.execute_order(过统一风控), 成功才改持仓。

        paper/auto唯一差别: paper通道不产生真实订单(PaperTrader), 但
        风控预检(kill_switch/每股每日限买/时段/限额)两种模式完全一致——
        严禁任何分支绕过trader直接改positions(2026-09-10审计修复)。
        """
        if action.type == ActionType.BUY:
            qty = action.qty or self.default_qty
            trace_id = _new_trace_id()
            self.el.log("trade", stage="order_request", code=action.code,
                        name=self.name_map.get(action.code, ""),
                        action="BUY", qty=qty, mode=self.mode,
                        source=action.source,
                        signal_ts=round(action.signal_ts, 3) or None,
                        signal_latency_ms=round(
                            (time.time() - action.signal_ts) * 1000)
                        if action.signal_ts else None,
                        trace_id=trace_id, round=self.round_id)
            r = self.trader.execute_order(
                action.code, "BUY", 0.0, qty,
                name=self.name_map.get(action.code, ""),
                queue_only=action.queue_only,
                closing_auction=action.closing_auction)
            self._log_order_result(action, "BUY", r, trace_id)
            if r.get("pending") or r.get("queue_only"):
                # 挂单中/排队单(跌停卖不适用BUY; 尾盘竞价买单): 不立即建仓,
                # 成交后后台线程补建仓, 最终结果会推送, 此处不告警
                tag = "排队单(15:00复查)" if r.get("queue_only") else "挂单监控中"
                action.reason += f" ({tag}: {r.get('error')})"
                self._log_action(action)
                return action
            if not r.get("ok"):
                # 风控规则性拒绝(每股每日限买/非时段/kill_switch)只记审计
                # 不告警, 防信号持续期间每轮刷屏
                return self._fail(action, r.get("error", "下单失败"),
                                  silent=bool(r.get("risk_block")),
                                  trace_id=trace_id)
            filled = r.get("filled_price", 0.0)
            try:
                self.positions.add(action.code,
                                   name=self.name_map.get(action.code, ""),
                                   entry_price=filled,
                                   note=f"{'纸面' if self.mode == 'paper' else '自动'}"
                                        f"买入 {time.strftime('%m-%d %H:%M')}")
            except Exception as e:
                # 成交已成事实但持仓事实源写入失败(如文件损坏): 绝不能静默,
                # 紧急告警人工补账, 否则后续轮询对该股状态全错
                log.error("成交后建仓写入失败 %s: %s", action.code, e)
                self._alert(Action(
                    ActionType.ALERT, action.code,
                    self.name_map.get(action.code, ""),
                    f"{action.code}已成交但持仓文件写入失败({e}), 立即人工核对补账",
                    action.source))
                action.reason += " (成交但持仓写入失败, 已紧急告警)"
                self._log_action(action)
                return action
            action.reason += f" (已建仓@{filled})"
        elif action.type == ActionType.SELL:
            qty = action.qty or self.default_qty
            trace_id = _new_trace_id()
            self.el.log("trade", stage="order_request", code=action.code,
                        name=self.name_map.get(action.code, ""),
                        action="SELL", qty=qty, mode=self.mode,
                        source=action.source,
                        signal_ts=round(action.signal_ts, 3) or None,
                        signal_latency_ms=round(
                            (time.time() - action.signal_ts) * 1000)
                        if action.signal_ts else None,
                        trace_id=trace_id, round=self.round_id)
            r = self.trader.execute_order(
                action.code, "SELL", 0.0, qty,
                name=self.name_map.get(action.code, ""),
                queue_only=action.queue_only,
                closing_auction=action.closing_auction)
            self._log_order_result(action, "SELL", r, trace_id)
            if r.get("pending") or r.get("queue_only"):
                # 排队单=跌停排队卖/尾盘竞价卖: 不立即清仓, 成交后后台补清仓
                tag = "排队单(15:00复查)" if r.get("queue_only") else "挂单监控中"
                action.reason += f" ({tag}: {r.get('error')})"
                self._log_action(action)
                return action
            if not r.get("ok"):
                return self._fail(action, r.get("error", "下单失败"),
                                  silent=bool(r.get("risk_block")),
                                  trace_id=trace_id)
            try:
                pos = self.positions.remove(action.code)
            except Exception as e:
                log.error("成交后清仓写入失败 %s: %s", action.code, e)
                self._alert(Action(
                    ActionType.ALERT, action.code,
                    self.name_map.get(action.code, ""),
                    f"{action.code}卖出已成交但持仓文件写入失败({e}), 立即人工核对",
                    action.source))
                action.reason += " (卖出成交但持仓写入失败, 已紧急告警)"
                self._log_action(action)
                return action
            action.reason += (f" (已清仓, 原持仓"
                              f"{pos.entry_time if pos else '?'})")
        elif action.type == ActionType.ALERT:
            self._alert(action)
        self._log_action(action)
        return action

    def _log_order_result(self, action: Action, side: str, r: dict,
                          trace_id: str = ""):
        """交易状态机: 下单结果审计(ok/pending/failed全记录, 对账依据)。

        signal_ts + signal_to_result_ms: 从信号帧捕获到订单结果(成交确认/
        挂单/拒绝)的全链路耗时, 与order_request中的signal_latency_ms
        (信号→决策)配对可拆分决策耗时与客户端操作耗时。
        trace_id/round: 与order_request同键, 跨阶段按ID直接配对(R1)。
        """
        self.el.log("trade", stage="order_result", code=action.code,
                    name=self.name_map.get(action.code, ""), action=side,
                    ok=bool(r.get("ok")), pending=bool(r.get("pending")),
                    uncertain=bool(r.get("uncertain")),
                    status=r.get("status", ""), mode=r.get("mode", ""),
                    filled_price=r.get("filled_price", 0.0),
                    signal_ts=round(action.signal_ts, 3) or None,
                    signal_to_result_ms=round(
                        (time.time() - action.signal_ts) * 1000)
                    if action.signal_ts else None,
                    trace_id=trace_id or None, round=self.round_id or None,
                    error=str(r.get("error", ""))[:300])

    def _fail(self, action: Action, reason: str, silent: bool = False,
              trace_id: str = "") -> Action:
        """下单失败: 不改positions, 升级为ALERT (绝不基于失败自动重试无界)。

        silent=True(风控规则性拒绝, 如"该股今日已买入过"): 只记审计不告警,
        否则信号持续期间每轮扫描都会重复ALERT刷屏。
        """
        self.el.log("trade", stage="order_failed", code=action.code,
                    action=action.type.value, reason=reason[:300],
                    trace_id=trace_id or None,
                    round=self.round_id or None)
        if silent:
            a = Action(ActionType.IGNORE, action.code, action.signal,
                       f"{reason} (风控规则, 当日不再重试)", action.source)
            log.info("跳过下单 %s: %s", action.code, reason)
            self._log_action(a)
            return a
        a = Action(ActionType.ALERT, action.code, action.signal,
                   f"下单失败({reason})。人工手动操作不会同步positions.json, "
                   f"若与预期不符请核对券商实际持仓并手工编辑该文件",
                   action.source)
        self._alert(a)
        self._log_action(a)
        return a

    def _alert(self, action: Action, level: str = "WARNING"):
        msg = (f"告警: {action.code} {self.name_map.get(action.code, '')} "
               f"{action.signal} - {action.reason}")
        log.warning(msg)
        print("\a" + msg)  # 终端响铃
        try:
            path = os.path.join(self.alerts_dir, "alerts.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "code": action.code, "signal": action.signal,
                    "reason": action.reason, "source": action.source,
                }, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("告警写入失败: %s", e)
        # 手机推送(后台线程, 网络IO不阻塞扫描/交易)
        if self.notifier and self.notifier.enabled():
            import threading
            title = f"交易告警 {action.code} {self.name_map.get(action.code, '')}"
            content = (f"信号: {action.signal}\n原因: {action.reason}\n"
                       f"来源: {action.source}\n时间: "
                       f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
            threading.Thread(target=self.notifier.send,
                             args=(title, content),
                             kwargs={"level": level},
                             daemon=True).start()

    @staticmethod
    def _log_action(action: Action):
        log.info("决策: [%s] %s %s (%s)", action.type.value, action.code,
                 action.signal, action.reason)
