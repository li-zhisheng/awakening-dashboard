# -*- coding: utf-8 -*-
"""交易规则回归测试集(无手机/无交易窗口依赖, 可离线运行)。

运行: python tests/test_rules.py  或  python main.py selftest
覆盖: 挂单台账状态机/二次挂单价格条件/涨跌停分支/停牌拦截/queue_only透传/
F1F3键位/开盘5分钟窗口/封单监控/大盘风控/行情双源/尾盘中断点/收盘日报/挂单页。
网络用例(双源真实行情)失败不致命, 仅在无网络时跳过。
"""
import inspect
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import load_config
from models.positions import PositionStore
from models.result import StockResult, StockStatus, Signal, DetectionInfo
from decision.decision import DecisionEngine, Action, ActionType
from trader.risk_control import RiskController
from trader.trader import PaperTrader
import trader.hotkey_trader as htmod
import trader.risk_control as rcmod
import ths.quote as qmod
from monitor.seal_watcher import SealWatcher
from monitor.market_guard import MarketGuard

PASS, FAIL, NETSKIP = 0, 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def mkres(code, sig):
    return StockResult(stock_code=code, signal=sig, status=StockStatus.OK,
                       detection=DetectionInfo(source="test"),
                       capture_time=time.time())


tmp = tempfile.mkdtemp(prefix="rules_regression_")
cfg = load_config(os.path.join(ROOT, "config.yaml"))
cfg.risk.state_file = os.path.join(tmp, "logs", "trade_state.json")
cfg.risk.enable = True
os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)

# ---------- S0 配置 ----------
print("S0 配置项")
check("sell_variant=latest(F3)", cfg.hotkey.sell_variant == "latest")
check("buy_variant=latest(F1)", cfg.hotkey.buy_variant == "latest")
check("opening_pending_wait=300", cfg.hotkey.opening_pending_wait == 300)
check("buy_pending_wait=120", cfg.hotkey.buy_pending_wait == 120)
check("sessions上午09:25起", cfg.risk.sessions[0][0] == "09:25:00")
check("大盘风控默认启用", cfg.risk.market_guard_enable is True)
check("大盘熔断线-4%", cfg.risk.market_crash_pct == -4.0)
check("大盘预警线-3%", cfg.risk.market_warn_pct == -3.0)
check("监控指数上证指数", cfg.risk.market_index == "sh000001")
check("买侧熔断标志默认kill_buy.flag",
      cfg.risk.buy_halt_file == "logs/kill_buy.flag")
check("熔断回升自动解除阈值1%", cfg.risk.market_crash_recovery_pct == 0.01)
# 第二轮整改(2026-09-15)新增配置默认值
check("尾盘主动买截止默认14:55且启用",
      cfg.risk.buy_deadline_enable is True and cfg.risk.buy_deadline == "14:55:00")
check("单票每日重挂上限默认3", cfg.risk.max_requeue_per_day == 3)
check("扫描卡死默认abort策略", cfg.monitor.stuck_policy == "abort")
check("云自选删除护栏默认开30%",
      cfg.watchlist.delete_guard_enable is True
      and cfg.watchlist.delete_guard_max_pct == 0.30)
check("看门狗重启护栏默认开/3次/14:57",
      cfg.monitor.watchdog_restart_enable is True
      and cfg.monitor.watchdog_restart_max_daily == 3
      and cfg.monitor.watchdog_restart_deadline == "14:57:00")
check("时钟守卫默认开30s/120s",
      cfg.monitor.clock_guard_enable is True
      and cfg.monitor.clock_skew_warn_sec == 30.0
      and cfg.monitor.clock_stale_warn_sec == 120.0)
check("价格守卫默认告警不拦截/买0.8%/卖1.5%",
      cfg.hotkey.price_guard_enable is True
      and cfg.hotkey.price_guard_enforce is False
      and cfg.hotkey.price_guard_buy_pct == 0.008
      and cfg.hotkey.price_guard_sell_pct == 0.015)
check("F6连续异常只读对账默认2次/冷却1800s",
      cfg.hotkey.f6_fallback_enable is True
      and cfg.hotkey.f6_fallback_threshold == 2
      and cfg.hotkey.f6_fallback_cooldown == 1800.0)
check("卖出F6预检与F8核对默认启用",
      cfg.hotkey.sell_precheck_f6 is True
      and cfg.hotkey.cancel_verify_enable is True)

def patch_dt(h, m, s=0):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            from datetime import timedelta
            d = datetime(2026, 9, 14, h, m, s)
            while d.weekday() >= 5:
                d = d + timedelta(days=1)
            return d
    rcmod.datetime = FakeDT


# ---------- S1 台账状态流转 ----------
print("S1 挂单尝试台账")
risk = RiskController(cfg.risk, project_root=tmp)
risk.record_attempt("600010", "BUY", 10.0, "pending")
check("pending不计成交名额", risk.filled_buy_count("600010") == 0)
risk.set_attempt_status("600010", "BUY", "canceled")
check("canceled不计成交名额", risk.filled_buy_count("600010") == 0)
check("台账读回canceled+价格",
      risk.get_attempt("600010", "BUY")["status"] == "canceled"
      and risk.get_attempt("600010", "BUY")["price"] == 10.0)
risk.set_attempt_status("600010", "BUY", "filled")
check("filled计成交名额", risk.filled_buy_count("600010") == 1)
pre = risk.pre_check("600010", "BUY", 100)
check("filled后再买被限买拦截", not pre.ok and "限买" in pre.reason, pre.reason)
risk2 = RiskController(cfg.risk, project_root=tmp)
check("台账持久化重建仍filled",
      risk2.get_attempt("600010", "BUY")["status"] == "filled")
risk2.record_attempt("600020", "SELL", 9.5, "canceled")
check("all_attempts枚举",
      set(risk2.all_attempts().keys()) == {"600010", "600020"})
# 熔断开关(当前真实时间可能为周末, 打工作日补丁)
patch_dt(10, 0)
check("熔断前未触发", risk.pre_check("600099", "BUY", 100).ok)
ok = risk.trip_kill_switch("回归测试熔断")
check("trip写入熔断flag", ok and os.path.isfile(
    os.path.join(tmp, "logs", "kill_switch.flag")))
check("熔断后拒单", not risk.pre_check("600099", "BUY", 100).ok)
check("重复trip不覆盖", risk.trip_kill_switch("再次") is False)
os.remove(os.path.join(tmp, "logs", "kill_switch.flag"))
rcmod.datetime = datetime

# ---------- S1b 交易时段 ----------
print("S1b 交易时段窗口")


patch_dt(9, 20)
check("9:20集合竞价不允许交易", not risk2.pre_check("600098", "BUY", 100).ok)
patch_dt(9, 27)
check("9:27允许挂单", risk2.pre_check("600098", "BUY", 100).ok)
patch_dt(14, 59)
check("14:59尾盘窗口允许", risk2.pre_check("600098", "SELL", 100).ok)
# 裁定6: 14:55主动买截止(卖单/止损/queue_only结算单不受影响)
patch_dt(14, 54, 59)
check("14:54:59主动买单仍放行", risk2.pre_check("600843", "BUY", 100).ok)
patch_dt(14, 55)
_pre_1455_buy = risk2.pre_check("600840", "BUY", 100)
check("14:55起新主动买单截止",
      not _pre_1455_buy.ok and "截止" in _pre_1455_buy.reason, _pre_1455_buy.reason)
check("14:55后卖单不受买截止影响", risk2.pre_check("600841", "SELL", 100).ok)
check("14:55后queue_only结算买单旁路",
      risk2.pre_check("600842", "BUY", 100, queue_only=True).ok)
rcmod.datetime = datetime

# ---------- S2 决策分支 ----------
print("S2 决策分支(含停牌拦截)")
pos = PositionStore(os.path.join(tmp, "positions.json"))
pos.add("600001", name="测试持仓股")


class FakeSeals:
    def __init__(self):
        self.up, self.down = [], []

    def watch_up(self, code, name, vol):
        self.up.append(code)

    def watch_down(self, code, name, vol):
        self.down.append(code)

    def remove(self, code):
        pass


class FakeTrader:
    def __init__(self, rk):
        self.risk = rk
        self.seal_watcher = FakeSeals()
        self.calls = []
        self.closing_flags = []
        self.next_result = {"ok": True}

    def execute_order(self, code, action, price=0, qty=0, name="",
                      queue_only=False, closing_auction=False):
        self.calls.append((code, action, queue_only))
        self.closing_flags.append((code, action, closing_auction))
        return dict(self.next_result)


tr = FakeTrader(risk2)
eng = DecisionEngine(pos, tr, tmp, name_map={}, mode="paper",
                     notifier=None,
                     universe_cfg=SimpleNamespace(enable=False),
                     max_positions=4)
SNAP = {}
eng._snapshot = lambda c: SNAP.get(c)

r = eng.decide(mkres("600100", Signal.LONG))
check("首次多头->BUY", r.type == ActionType.BUY)

risk2.record_attempt("600101", "BUY", 10.0, "canceled")
SNAP["600101"] = {"price": 10.20, "at_limit_up": False, "halted": False}
r = eng.decide(mkres("600101", Signal.LONG))
check("二次多头现价>首价->IGNORE不追高",
      r.type == ActionType.IGNORE and "不追高" in r.reason, r.reason)
SNAP["600101"]["price"] = 9.90
r = eng.decide(mkres("600101", Signal.LONG))
check("二次多头现价≤首价->BUY补单", r.type == ActionType.BUY, r.reason)

risk2.record_attempt("600102", "BUY", 10.0, "pending")
SNAP["600102"] = {"price": 10.0, "at_limit_up": False, "halted": False}
r = eng.decide(mkres("600102", Signal.LONG))
check("买单pending->不重复挂单",
      r.type == ActionType.IGNORE and "挂单中" in r.reason, r.reason)

SNAP["600103"] = {"price": 11.0, "at_limit_up": True, "halted": False,
                  "seal_vol": 1000}
r = eng.decide(mkres("600103", Signal.LONG))
check("涨停多头->不排队买入",
      r.type == ActionType.IGNORE and "涨停" in r.reason, r.reason)
check("涨停注册封单监控", "600103" in tr.seal_watcher.up)

# 停牌买入拦截
SNAP["600104"] = {"price": 0.0, "at_limit_up": False, "halted": True}
r = eng.decide(mkres("600104", Signal.LONG))
check("停牌多头->不买入",
      r.type == ActionType.IGNORE and "停牌" in r.reason, r.reason)
r2 = eng.decide(mkres("600104", Signal.LONG))
check("停牌告警当日去重(不崩)", r2.type == ActionType.IGNORE)

risk2.record_attempt("600001", "SELL", 10.0, "canceled")
SNAP["600001"] = {"price": 9.50, "at_limit_down": False, "halted": False}
r = eng.decide(mkres("600001", Signal.SHORT))
check("二次空头现价<首价->不低价卖",
      r.type == ActionType.IGNORE and "不低价" in r.reason, r.reason)
SNAP["600001"]["price"] = 10.20
r = eng.decide(mkres("600001", Signal.SHORT))
check("二次空头现价≥首价->SELL重挂", r.type == ActionType.SELL, r.reason)

risk2.record_attempt("600001", "SELL", 10.0, "pending")
r = eng.decide(mkres("600001", Signal.SHORT))
check("卖单pending->不重复挂单", r.type == ActionType.IGNORE, r.reason)

risk2.set_attempt_status("600001", "SELL", "canceled")
SNAP["600001"] = {"price": 9.0, "at_limit_down": True, "halted": False,
                  "seal_vol": 2000}
r = eng.decide(mkres("600001", Signal.SHORT))
check("跌停->SELL排队单", r.type == ActionType.SELL and r.queue_only)
check("跌停注册封单监控(撤单回调)", "600001" in tr.seal_watcher.down)

# 停牌卖出拦截(CRITICAL)
SNAP["600001"] = {"price": 0.0, "at_limit_down": False, "halted": True}
r = eng.decide(mkres("600001", Signal.SHORT))
check("停牌空头->不挂卖单+等复牌",
      r.type == ActionType.IGNORE and "停牌" in r.reason, r.reason)

for c in ("6000a", "6000b", "6000c"):
    pos.add(c, name="满")
r = eng.decide(mkres("600109", Signal.LONG))
check("满仓4只->多头只记录不开仓",
      r.type == ActionType.IGNORE and "满仓" in r.reason, r.reason)
for c in ("6000a", "6000b", "6000c"):
    pos.remove(c)

# ---------- S3 queue_only透传 ----------
print("S3 queue_only透传")
pos.remove("600001")
tr.calls.clear()
tr.next_result = {"ok": False, "queue_only": True}
eng.execute(Action(ActionType.BUY, "600200", "LONG", "尾盘买", queue_only=True))
check("execute透传queue_only=True",
      tr.calls and tr.calls[-1] == ("600200", "BUY", True), str(tr.calls))
check("queue_only返回不建仓", not pos.is_held("600200"))
alert_txt = ""
ap = os.path.join(tmp, "alerts.jsonl")
if os.path.exists(ap):
    alert_txt = open(ap, encoding="utf-8").read()
check("queue_only不误报下单失败", "下单失败" not in alert_txt)

patch_dt(9, 27)
pt = PaperTrader(risk2)
rr = pt.execute_order("600201", "BUY", 0, 100, name="纸面排队",
                      queue_only=True)
rcmod.datetime = datetime
check("PaperTrader收queue_only即时成交",
      rr.get("ok") and risk2.get_attempt("600201", "BUY")["status"] == "filled")

# ---------- S4 热键/开盘窗口 ----------
print("S4 热键选择/挂单等待时长")
ht = htmod.HotkeyTrader(cfg, risk2)
check("BUY->F1", ht._pick_key("BUY") == "{F1}")
check("SELL->F3(最新价不核卖)", ht._pick_key("SELL") == "{F3}")


class FakeDT2(datetime):
    fixed = datetime(2026, 9, 14, 9, 27)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


htmod.datetime = FakeDT2
check("9:27挂单等待5分钟(300s)", ht._pending_wait_seconds() == 300)
FakeDT2.fixed = datetime(2026, 9, 14, 10, 0)
check("10:00挂单等待2分钟(120s)", ht._pending_wait_seconds() == 120)
FakeDT2.fixed = datetime(2026, 9, 14, 9, 24, 59)
check("9:24:59仍走常规2分钟", ht._pending_wait_seconds() == 120)
htmod.datetime = datetime

# ---------- S5 封单监控(10%秒级异动新规则) ----------
print("S5 封单监控")
_ORIG_Q = qmod.realtime_quote
seal_notes = []
cancelled = []
pending_map = {}

# 固定工作日10:00, 使14:57收盘竞价门禁不影响既有用例(确定性)
import monitor.seal_watcher as swmod


class _SealDT(datetime):
    fixed = datetime(2026, 9, 14, 10, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


swmod.datetime = _SealDT


class _SealNotify:
    def send(self, title, content, level="INFO"):
        seal_notes.append((title, level))


def seal_q(price, lid, ldown, bidv, askv, vol):
    return lambda code, timeout=5.0: {
        "price": price, "limit_up": lid, "limit_down": ldown,
        "bid1_vol": bidv, "ask1_vol": askv, "seal_vol": vol,
        "halted": False}


sw = SealWatcher(notifier=_SealNotify(),
                 cancel_fn=lambda c, side: cancelled.append((c, side)) or True,
                 pending_fn=lambda c: pending_map.get(c, ""),
                 interval=3600)
sw.watch_down("600300", "跌停股", 1000.0)

qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 1000.0)
sw._poll()
check("封单持平不动作", cancelled == [] and seal_notes == [])
qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 950.0)
sw._poll()
check("封单变动<10%不动作", cancelled == [] and seal_notes == [])

# 小基数噪声保护(独立小票): 40手->400手不判, 基线只抬高不告警
sw.watch_down("600305", "低流动股", 40.0)
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           950.0 if c == "600300" else
           (400.0 if c == "600305" else 9.0))(c))
sw._poll()
check("50手以下基数不判异动", cancelled == [] and seal_notes == []
      and sw._down["600305"]["last_vol"] == 400.0)

# 骤减10%无挂单: CRITICAL告警(其他票保持原值不干扰)
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           360.0 if c == "600300" else
           (400.0 if c == "600305" else 9.0))(c))
sw._poll()
check("骤减≥10%无挂单->CRITICAL告警",
      seal_notes and seal_notes[-1][1] == "CRITICAL" and cancelled == [])
# 冷却内再减不重复告警(600305保持400基线)
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           300.0 if c == "600300" else
           (400.0 if c == "600305" else 9.0))(c))
sw._poll()
check("同票180s冷却不重复告警", len(seal_notes) == 1)

# 有SELL挂单再骤减: 必须撤单(不受冷却限制)
sw._down["600300"]["last_alert_ts"] = 0
pending_map["600300"] = "SELL"
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           250.0 if c == "600300" else
           (400.0 if c == "600305" else 9.0))(c))
sw._poll()
check("骤减+SELL挂单->F8撤单带方向", cancelled == [("600300", "SELL")])
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           200.0 if c == "600300" else
           (400.0 if c == "600305" else 9.0))(c))
sw._poll()
check("每票只撤一次", len(cancelled) == 1)

# 暴增(无挂单, 新票): WARNING
sw.watch_down("600302", "跌停股B", 1000.0)
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(9.0, 11.0, 9.0, 0, 0,
           200.0 if c == "600300" else
           (1150.0 if c == "600302" else
            (400.0 if c == "600305" else 9.0)))(c))
sw._poll()
b_notes = [n for t, n in seal_notes if "600302" in t]
check("封单暴增->WARNING", b_notes and b_notes[-1] == "WARNING")

# 涨停侧骤减+BUY挂单(尾盘queue_only兜底场景): 撤买单不买入
sw.watch_up("600303", "涨停股B", 2000.0)
pending_map["600303"] = "BUY"
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(11.0, 11.0, 9.0, 0, 0,
           200.0 if c == "600300" else
           (1150.0 if c == "600302" else
            (1700.0 if c == "600303" else
             (400.0 if c == "600305" else 9.0))))(c))
sw._poll()
check("涨停侧异动+BUY挂单->撤买单", ("600303", "BUY") in cancelled)
check("涨停异动只告警不买入", all(t.startswith(("涨停异动", "跌停异动"))
                                  for t, _ in seal_notes))

# 涨停开板: 一次性告警摘除, 无挂单不撤
sw.watch_up("600304", "涨停股C", 1000.0)
qmod.realtime_quote = lambda c, timeout=5.0: (
    seal_q(10.95, 11.0, 9.0, 100, 300, 0.0)(c) if c == "600304"
    else seal_q(11.0, 11.0, 9.0, 0, 0,
                400.0 if c == "600305" else 1000.0)(c))
n_before = len(seal_notes)
sw._poll()
check("涨停开板告警并摘除",
      len(seal_notes) == n_before + 1 and "600304" not in sw._up)
sw.stop()

# 14:57-15:00收盘集合竞价F8硬门禁(独立watcher, 独立收集)
close_cancelled, close_notes = [], []


class _CloseNotify:
    def send(self, title, content, level="INFO"):
        close_notes.append((title, level, content))


swc = SealWatcher(notifier=_CloseNotify(),
                  cancel_fn=lambda c, side: close_cancelled.append((c, side))
                  or True,
                  pending_fn=lambda c: "SELL", interval=3600)
# 本用例组全部同步调用_poll做确定性断言: 预置停止事件, 禁止后台线程
# 首轮抢跑(否则会在行情lambda切换间隙读到上一用例的陈旧报价)
swc._stop.set()
swc.watch_down("600310", "尾盘跌停股", 1000.0)
_SealDT.fixed = datetime(2026, 9, 14, 14, 58, 0)
qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 500.0)
swc._poll()
check("14:58封单骤减+卖单->禁F8留单",
      close_cancelled == [] and swc._down["600310"]["close_blocked"] is True)
check("门禁专用告警CRITICAL且含集合竞价",
      any("收盘竞价禁撤" in t and lv == "CRITICAL"
          for t, lv, _ in close_notes)
      and any("15:00" in c for _, _, c in close_notes))
qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 200.0)
swc._poll()
check("门禁专用告警每票只一次",
      sum(1 for t, _, _ in close_notes if "收盘竞价禁撤" in t) == 1
      and close_cancelled == [])
# 14:56:59 边界: 未进窗口, F8正常撤
_SealDT.fixed = datetime(2026, 9, 14, 14, 56, 59)
swc.watch_down("600311", "尾盘跌停股B", 1000.0)
qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 800.0)
swc._poll()
check("14:56:59仍正常F8撤单", ("600311", "SELL") in close_cancelled)
# 周日14:58: 非交易日不触发门禁(集合竞价仅工作日)
_SealDT.fixed = datetime(2026, 9, 13, 14, 58, 0)
swc.watch_down("600312", "周日跌停股", 1000.0)
qmod.realtime_quote = seal_q(9.0, 11.0, 9.0, 0, 0, 500.0)
swc._poll()
check("周日14:58不拦撤单", ("600312", "SELL") in close_cancelled)
swc.stop()
swmod.datetime = datetime
qmod.realtime_quote = _ORIG_Q

# ---------- S6 行情快照: 停牌/涨跌停/指数符号 ----------
print("S6 行情快照构造")
from ths.quote import _build_snapshot, _limit_prices
hs = _build_snapshot("停牌股", 0.0, 10.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "t")
check("现价0+昨收有值->halted", hs["halted"] is True)
ns = _build_snapshot("正常", 10.5, 10.0, 10.1, 10.6, 9.9, 5.0,
                     10.5, 100, 10.51, 50, 11.0, 9.0, "t")
check("正常快照不停牌/不封板",
      not ns["halted"] and not ns["at_limit_up"] and not ns["at_limit_down"])
us = _build_snapshot("涨停", 11.0, 10.0, 10.1, 11.0, 10.0, 10.0,
                     11.0, 5000, 0, 0, 11.0, 9.0, "t")
check("涨停无卖盘->sealed_up+封单5000",
      us["sealed_up"] and us["seal_vol"] == 5000)
ds = _build_snapshot("跌停", 9.0, 10.0, 9.5, 9.5, 9.0, -10.0,
                     0, 0, 9.0, 3000, 11.0, 9.0, "t")
check("跌停无买盘->sealed_down+封单3000",
      ds["sealed_down"] and ds["seal_vol"] == 3000)
check("涨停价10元->11.00", _limit_prices(10.0) == (11.0, 9.0))
check("ST涨停价3.47->3.64", _limit_prices(3.47, "ST股") == (3.64, 3.30))
check("指数代码直传解析", qmod._market("000001") == "sz")
import re as _re
check("sh000001带前缀直通", bool(_re.fullmatch(r"(sh|sz)(\d{6})", "sh000001")))

# 真实双源(无网络自动跳过)
try:
    q = _ORIG_Q("600487", 6)
    if q:
        check("腾讯主源真实行情", q["price"] > 0 and q["source"] == "tencent")
        sq = qmod._sina_quote("sh600487", 6)
        check("新浪备源真实行情一致", sq and abs(
            sq["price"] - q["price"]) < 0.01)
        si = _ORIG_Q("sh000001", 6)
        check("上证指数行情", si and si["price"] > 0)
    else:
        NETSKIP += 3
        print("  [SKIP] 无网络, 跳过真实双源行情3项")
except Exception as e:
    NETSKIP += 3
    print(f"  [SKIP] 网络异常跳过双源行情: {e}")

# ---------- S7 大盘风控(买侧熔断: 只禁买/卖出撤单照常/回升解除) ----------
print("S7 大盘系统性风控")
ks_path = os.path.join(tmp, "logs", "kill_switch.flag")
kb_path = os.path.join(tmp, "logs", "kill_buy.flag")
for _p in (ks_path, kb_path):
    if os.path.exists(_p):
        os.remove(_p)
mg_risk = RiskController(cfg.risk, project_root=tmp)
notes = []


class FakeNotify:
    def send(self, title, content, level="INFO"):
        notes.append((title, level, content))


mg = MarketGuard(cfg, mg_risk, notifier=FakeNotify())
import monitor.market_guard as mgmod


class _FakeGuardDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 14, 10, 0)


mgmod.datetime = _FakeGuardDT
patch_dt(10, 0)   # 风控时段同步固定工作日10:00(SELL放行断言依赖)


def fake_q(pct_v, price=3800.0):
    return lambda code, timeout=5.0: {"name": "上证指数", "price": price,
                                      "pct": pct_v, "halted": False}


qmod.realtime_quote = fake_q(-1.0)
check("跌1%->ok不动作", mg.check() == "ok" and notes == [])
qmod.realtime_quote = fake_q(-3.2)
check("跌3.2%->warn预警", mg.check() == "warn"
      and notes and notes[-1][1] == "WARNING")
mg.check()
check("预警当日只发一次", len([n for n in notes if n[1] == "WARNING"]) == 1)
qmod.realtime_quote = fake_q(-4.1)
check("跌4.1%->halt买侧熔断(不写kill_switch)", mg.check() == "halt"
      and os.path.isfile(kb_path) and not os.path.isfile(ks_path))
_flag = mg_risk.read_buy_halt()
check("熔断flag记录指数与低点", _flag.get("index") == "sh000001"
      and abs(_flag.get("low", 0) - 3800.0) < 0.001)
check("熔断告警为CRITICAL", any(lv == "CRITICAL" for _, lv, _ in notes))
_pre_buy = mg_risk.pre_check("600500", "BUY", 100)
check("熔断后BUY被拒(原因含买侧熔断)",
      not _pre_buy.ok and "买侧熔断" in _pre_buy.reason)
check("熔断后SELL放行(风控止损不停)",
      mg_risk.pre_check("600500", "SELL", 100).ok)
qmod.realtime_quote = fake_q(-5.0)
check("熔断中halted不重复告警", mg.check() == "halted"
      and len([n for n in notes if n[1] == "CRITICAL"]) == 1)
check("熔断中SELL仍放行", mg_risk.pre_check("600500", "SELL", 100).ok)
# 回升自动解除: 低点3800, 阈值1% => 现价>=3838解除
qmod.realtime_quote = fake_q(-2.0, 3840.0)
check("低点回升1%->recovered自动解除", mg.check() == "recovered"
      and not os.path.isfile(kb_path))
check("解除发恢复通知", any("解除" in t for t, _, _ in notes))
check("解除后BUY放行", mg_risk.pre_check("600501", "BUY", 100).ok)
# 同日再跌穿-4%: 允许重新触发
qmod.realtime_quote = fake_q(-4.5, 3700.0)
check("同日再跌-4.5%->重新halt", mg.check() == "halt"
      and os.path.isfile(kb_path))
# 进程重启: 新Guard从今日flag恢复跟踪指数/低点, 回升仍可解除
mg_restart = MarketGuard(cfg, mg_risk, notifier=FakeNotify())
qmod.realtime_quote = fake_q(-4.4, 3702.0)
check("重启后读flag保持halted", mg_restart.check() == "halted")
qmod.realtime_quote = fake_q(-3.0, 3750.0)
check("重启后回升仍可自动解除", mg_restart.check() == "recovered"
      and not os.path.isfile(kb_path))
# 跨天残留flag: 次日首轮自动清除(买侧熔断不跨天)
mg_risk.trip_buy_halt("陈旧熔断", "sh000001", 3000.0, date="2000-01-01")
mg_day = MarketGuard(cfg, mg_risk, notifier=FakeNotify())
qmod.realtime_quote = fake_q(-1.0)
# 陈旧flag在首轮check()内才清除: 先跑check再断言flag已删+结果ok
_r_stale = mg_day.check()
check("非今日买侧flag自动清除并正常判定",
      not os.path.isfile(kb_path) and _r_stale == "ok")
mg_fresh = MarketGuard(cfg, mg_risk, notifier=FakeNotify())
qmod.realtime_quote = lambda code, timeout=5.0: None
check("行情全失败->noquote且不误熔断",
      mg_fresh.check() == "noquote"
      and not os.path.isfile(kb_path) and not os.path.isfile(ks_path))
rcmod.datetime = datetime
mgmod.datetime = datetime
qmod.realtime_quote = _ORIG_Q

# ---------- S8 尾盘14:57中断点 ----------
print("S8 尾盘扫描中断点")
from scanner.scanner import StockScanner
check("scan_loop支持should_break",
      "should_break" in inspect.signature(
          StockScanner.scan_loop).parameters)
from scanner.scheduler import AutoRoundScheduler, CLOSING_SETTLE_START
check("结算窗口常量14:57", CLOSING_SETTLE_START == "14:57:00")


class _SchedulerStub:
    pass


stub = _SchedulerStub()
fn = AutoRoundScheduler._closing_deadline_reached


class FakeDT3(datetime):
    fixed = datetime(2026, 9, 14, 14, 56, 59)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


import scanner.scheduler as schmod
schmod.datetime = FakeDT3
check("14:56:59不中断", fn(stub) is None)
FakeDT3.fixed = datetime(2026, 9, 14, 14, 57, 0)
check("14:57:00触发中断", "尾盘" in (fn(stub) or ""))
FakeDT3.fixed = datetime(2026, 9, 13, 14, 58)   # 周日
check("周末不触发中断", fn(stub) is None)
schmod.datetime = datetime

# ---------- S9 两份日报(上午/收盘+大盘+成交明细) ----------
print("S9 日报组装")
# 独立台账目录, 避免前面章节的attempts干扰计数
tmp2 = tempfile.mkdtemp(prefix="rules_closing_")
os.makedirs(os.path.join(tmp2, "logs"), exist_ok=True)
cfg.risk.state_file = os.path.join(tmp2, "logs", "trade_state.json")
rrisk = RiskController(cfg.risk, project_root=tmp2)
# 台账成交时间固定为今日14:30: 消除"上午时段跑selftest"的时钟依赖
# (收盘分组要求下午/午盘日报要求上午成交0笔)
_pm_ts = time.mktime(time.strptime(
    time.strftime("%Y%m%d") + " 14:30:00", "%Y%m%d %H:%M:%S"))
_orig_time_time = rcmod.time.time
rcmod.time.time = lambda: _pm_ts
rrisk.record_attempt("600700", "BUY", 10.0, "filled")
rrisk.record_attempt("600701", "SELL", 11.0, "canceled")
rcmod.time.time = _orig_time_time
import monitor.report as rpmod
from monitor.report import build_report, build_closing_report, _read_attempts, \
    _filled_trades, maybe_send_closing_report
att = _read_attempts(cfg)
check("日报读台账2只", set(att.keys()) == {"600700", "600701"})
rpos = PositionStore(os.path.join(tmp, "pos2.json"))
rpos.add("600700", name="日报持仓")
# 大盘段打桩(离线确定性), 行情打桩(持仓现价)
_ORIG_MARKET_LINES = rpmod._market_lines
rpmod._market_lines = lambda title, with_losers=False: [
    f"{title}: 上证指数-1.18% | 深证成指-1.08%",
    "全A 5284只: 涨619 跌4619 平46",
    "领涨板块: 通信线缆+5.84%"]
qmod.realtime_quote = lambda code, timeout=5.0: (
    {"name": "日报持仓", "price": 10.5, "pct": 2.0,
     "entry_x": 0} if code == "600700" else None)
txt = build_closing_report(cfg, rpos, rounds_today=3)
check("收盘日报含标题", "收盘日报" in txt)
check("收盘日报含全天大盘", "全天大盘" in txt and "全A 5284只" in txt)
check("收盘日报含领涨板块", "领涨板块" in txt)
check("收盘日报含上下午成交分组",
      "今日成交明细" in txt and "下午(买1/卖0)" in txt)
check("收盘日报成交流水带时间/价",
      "买入 600700 日报持仓 @10.00" in txt)
check("收盘日报含台账终态", "买成1" in txt and "撤单1" in txt)
check("收盘日报含持仓浮盈", "600700" in txt and "浮盈" in txt)
ltxt = build_report(cfg, rpos, rounds_today=1)
check("午盘日报含上午大盘", "盘中日报(上午)" in ltxt and "上午大盘" in ltxt)
# record_attempt成交时间为当前(晚间), 不计入上午时段
check("午盘日报上午成交计数兜底", "上午成交: 买入0笔" in ltxt)
trades = _filled_trades(cfg)
check("成交流水读filled不读canceled",
      len(trades) == 1 and trades[0][1] == "BUY" and trades[0][2] == "600700")
check("非窗口不发送", maybe_send_closing_report(cfg, rpos, None, 3) is False)
rpmod._market_lines = _ORIG_MARKET_LINES  # 还原
qmod.realtime_quote = _ORIG_Q

# ---------- S10 Web挂单状态页数据 ----------
print("S10 Web /api/attempts 数据采集")
from webapp.app import _State
state = _State(cfg)
state.logs_dir = lambda: os.path.join(tmp2, "logs")
cfg.positions.file = os.path.join(tmp, "pos2.json")
state.quotes = lambda codes: {}
state.name_map = lambda events, need=None: {"600700": "日报持仓",
                                            "600701": "撤单股"}
d = state.attempts()
check("台账返回2行", len(d["rows"]) == 2)
_order = [r["status"] for r in d["rows"]]
_expect = sorted(_order,
                 key=lambda s: {"pending": 0, "unknown": 1,
                                "filled": 2, "canceled": 3}[s])
check("pending/unknown置顶排序", _order == _expect, str(_order))
buy_row = [r for r in d["rows"] if r["code"] == "600700"][0]
check("行含首挂价/方向/持仓标志",
      buy_row["first_price"] == 10.0 and buy_row["action"] == "BUY"
      and buy_row["held"] is True and "halted" in buy_row)
check("返回kill_switch字段", "kill_switch" in d and d["kill_switch"] is False)
check("返回buy_halt字段", "buy_halt" in d and d["buy_halt"] is False)

# ---------- S11 市场概貌(日报大盘段) ----------
print("S11 市场概貌")
from ths.market import format_overview
ov = {"indices": [{"code": "sh000001", "name": "上证指数",
                   "price": 3888.11, "pct": -1.18},
                  {"code": "sz399001", "name": "深证成指",
                   "price": 13471.26, "pct": -1.08}],
      "breadth": {"up": 619, "down": 4619, "flat": 46, "total": 5284},
      "sectors": {"gainers": [("通信线缆", 5.84), ("玻纤", 4.67)],
                  "losers": [("期货", -5.85)]}}
ml = format_overview(ov, "上午大盘")
check("概貌含指数行", any("上证指数-1.18%" in x and "深证成指-1.08%" in x
                          for x in ml))
check("概貌含涨跌家数", any("涨619 跌4619 平46" in x for x in ml))
check("概貌含领涨/领跌板块",
      any("通信线缆+5.84%" in x for x in ml)
      and any("期货-5.85%" in x for x in ml))
check("概貌全空降级返回[]", format_overview({}) == [])
# 真实东财接口(无网/盘口异常自动跳过2项)
try:
    from ths import market as mktmod
    b = mktmod.market_breadth()
    if b and b["total"] > 1000 and b["up"] + b["down"] + b["flat"] == b["total"]:
        check("东财涨跌家数真实接口", True)
    else:
        NETSKIP += 1
        print("  [SKIP] 涨跌家数口径异常(非交易时段), 跳过")
    s = mktmod.hot_sectors(top=3)
    check("东财板块榜真实接口", len(s["gainers"]) == 3
          and s["gainers"][0][0])
except Exception as e:
    NETSKIP += 2
    print(f"  [SKIP] 网络异常跳过东财2项: {e}")

# ---------- S12 生产加固(单实例锁/日志轮转/持仓损坏自愈) ----------
print("S12 生产加固")
import logging.handlers
from instance_lock import SingleInstance, IS_WINDOWS
from models.positions import PositionFileCorruptError

# 12.1 跨进程单实例锁(POSIX测flock互斥; Windows互斥量逻辑真机验证)
lock_file = os.path.join(tmp, "app.lock")
if not IS_WINDOWS:
    la, lb = SingleInstance(lock_file), SingleInstance(lock_file)
    check("首个交易实例获锁", la.acquire() is True)
    check("第二个实例获锁被拒", lb.acquire() is False)
    la.release()
    lc = SingleInstance(lock_file)
    check("持锁实例释放后可重新获锁", lc.acquire() is True)
    check("锁文件写入持有方PID", "pid=" in open(lock_file, encoding="utf-8").read())
    lc.release()
else:
    NETSKIP += 3
    print("  [SKIP] Windows平台跳过flock单实例锁3项(互斥量真机验证)")

# 12.2 日志按大小轮转
import main as mainmod
rot_dir = tempfile.mkdtemp(prefix="rules_rotlog_")
mainmod.setup_logging(rot_dir)
_rot = [h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)]
check("文件日志为RotatingFileHandler", bool(_rot))
check("单日志10MB上限/保留3份",
      _rot and _rot[0].maxBytes == mainmod.LOG_MAX_BYTES == 10 * 1024 * 1024
      and _rot[0].backupCount == 3)

# 12.3 持仓滚动快照 + 损坏自愈
rb_dir = tempfile.mkdtemp(prefix="rules_rebuild_")
rb_logs = os.path.join(rb_dir, "logs")
os.makedirs(rb_logs, exist_ok=True)
rb_file = os.path.join(rb_dir, "positions.json")
rb_store = PositionStore(rb_file)
rb_store.add("600800", name="快照基线股", entry_price=8.0)
snap_path = os.path.join(rb_logs, "positions.snapshot.json")
check("写持仓同步滚动快照", os.path.isfile(snap_path))
# 构造"快照滞后": 手动把快照改回只有基线股且ts置旧, 再手写后续事件
_now = time.time()
with open(snap_path, "w", encoding="utf-8") as f:
    json.dump({"ts": _now - 3600, "date": "2000-01-01",
               "positions": [{"code": "600799", "name": "快照独有股",
                              "entry_time": "2026-09-10 10:00:00",
                              "entry_price": 7.5, "note": "snap"}]}, f,
              ensure_ascii=False)
_ev_path = os.path.join(rb_logs, f"events_{time.strftime('%Y%m%d')}.jsonl")
with open(_ev_path, "w", encoding="utf-8") as f:
    for evt in [
        {"kind": "trade", "stage": "position_add", "code": "600800",
         "name": "事件建仓股", "entry_price": 9.9,
         "entry_time": "2026-09-15 09:35:00", "note": "", "ts": _now - 200},
        {"kind": "signal", "code": "600800", "from": "NEW", "to": "LONG",
         "ts": _now - 190},   # 非trade事件必须被忽略
        {"kind": "trade", "stage": "position_remove", "code": "600800",
         "name": "事件建仓股", "ts": _now - 180},
        {"kind": "trade", "stage": "position_add", "code": "600801",
         "name": "最终持仓股", "entry_price": 12.3,
         "entry_time": "2026-09-15 10:00:00", "note": "", "ts": _now - 170},
    ]:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")
with open(rb_file, "w", encoding="utf-8") as f:
    f.write("{broken-json!!!")   # 事实源损坏
rb2 = PositionStore(rb_file)
check("损坏自愈: 快照基线股保留", rb2.is_held("600799"))
check("损坏自愈: 事件建仓后已清仓不复活", not rb2.is_held("600800"))
check("损坏自愈: 事件新增股在仓", rb2.is_held("600801"))
check("重建保留建仓价/时间",
      rb2.get("600801").entry_price == 12.3
      and rb2.get("600801").entry_time == "2026-09-15 10:00:00")
check("损坏原件已隔离(.corrupt-*)",
      any(x.startswith("positions.json.corrupt-")
          for x in os.listdir(rb_dir)))
check("重建后事实源已写回且可再写",
      rb2.add("600802", name="重建后新仓") and rb2.is_held("600802"))

# 无快照+无事件: 不能凭空重建, 维持隔离拒写(2026-09-10防护不退化)
no_dir = tempfile.mkdtemp(prefix="rules_norebuild_")
os.makedirs(os.path.join(no_dir, "logs"), exist_ok=True)
no_file = os.path.join(no_dir, "positions.json")
with open(no_file, "w", encoding="utf-8") as f:
    f.write("<<<not json>>>")
no_store = PositionStore(no_file)
_raised = False
try:
    no_store.save()
except PositionFileCorruptError:
    _raised = True
check("无重建数据源维持拒写", _raised)
with open(no_file, encoding="utf-8") as f:
    check("拒写不覆盖损坏原件", f.read() == "<<<not json>>>")

# 12.4 运行资源守卫(零依赖: 快照/阈值分级/边沿去重/恢复/开关)
from monitor.resources import ResourceGuard


class _ResNotify:
    def __init__(self):
        self.items = []

    def send(self, title, content, level="INFO"):
        self.items.append((title, level, content))


check("资源阈值默认值2/5/500/800",
      cfg.monitor.disk_critical_gb == 2.0 and cfg.monitor.disk_warn_gb == 5.0
      and cfg.monitor.logs_size_warn_mb == 500.0
      and cfg.monitor.rss_warn_mb == 800.0)
rn = _ResNotify()
rg = ResourceGuard(cfg, notifier=rn)
snap = rg.snapshot()
check("资源快照含磁盘/日志体量/内存",
      isinstance(snap.get("disk_free_gb"), float) and snap["disk_free_gb"] > 0
      and snap.get("logs_mb", -1) >= 0
      and (snap.get("rss_mb") is None or snap["rss_mb"] > 0))
# 注入假快照: 磁盘1.2GB(critical)+日志600MB+内存900MB
rg.snapshot = lambda: {"disk_free_gb": 1.2, "disk_total_gb": 100.0,
                       "disk_free_pct": 1.2, "logs_mb": 600.0,
                       "rss_mb": 900.0}
check("磁盘<2GB->critical", rg.check() == "critical"
      and any(lv == "CRITICAL" for _, lv, _ in rn.items))
check("critical告警含磁盘与清理指引",
      any("磁盘" in c and "清理" in c for _, _, c in rn.items))
rg.check()
check("同级持续异常只推送一次", len(rn.items) == 1)
# 磁盘回到3GB(脱离critical), 但日志/内存仍warn: 降级不重推
rg.snapshot = lambda: {"disk_free_gb": 3.0, "disk_total_gb": 100.0,
                       "disk_free_pct": 3.0, "logs_mb": 600.0,
                       "rss_mb": 900.0}
check("critical缓解为warn不重复推送", rg.check() == "warn"
      and len(rn.items) == 1)
# 全部恢复正常: 推一条恢复提醒
rg.snapshot = lambda: {"disk_free_gb": 50.0, "disk_total_gb": 100.0,
                       "disk_free_pct": 50.0, "logs_mb": 10.0,
                       "rss_mb": 100.0}
check("资源恢复->ok并推恢复提醒", rg.check() == "ok"
      and len(rn.items) == 2 and "恢复" in rn.items[-1][0])
# 纯warn场景(独立guard): 日志体量+内存超阈值
rn2 = _ResNotify()
rw = ResourceGuard(cfg, notifier=rn2)
rw.snapshot = lambda: {"disk_free_gb": 50.0, "disk_total_gb": 100.0,
                       "disk_free_pct": 50.0, "logs_mb": 600.0,
                       "rss_mb": 900.0}
check("日志/内存超阈值->WARNING", rw.check() == "warn"
      and rn2.items[-1][1] == "WARNING")
# 开关: resource_check_enable / monitor.enable 任一关闭即禁用
cfg.monitor.resource_check_enable = False
check("resource_check_enable=false->disabled", rg.check() == "disabled")
cfg.monitor.resource_check_enable = True
cfg.monitor.enable = False
check("monitor总开关关闭->联动disabled", rg.check() == "disabled")
cfg.monitor.enable = True

# ---------- S13 第二轮整改(2026-09-15 八项裁定) ----------
print("S13 第二轮整改护栏")

# 13.1 R2 配置schema语义校验 + 未知键warn-only
from config import validate_config, _build, MonitorConfig
check("当前config零schema硬错误", validate_config(cfg) == [],
      str(validate_config(cfg)))
_uw = []
_build(MonitorConfig, {"stuck_policy": "abort", "不存在的键": 1},
       "monitor", _uw)
check("未知配置键收集为warn且不阻断",
      any("monitor.不存在的键" in x for x in _uw))
cfg.monitor.stuck_policy = "bogus"
check("stuck_policy非法枚举被捕获",
      any("stuck_policy" in x for x in validate_config(cfg)))
cfg.monitor.stuck_policy = "abort"
cfg.risk.buy_deadline = "14:55"
check("buy_deadline时间格式校验",
      any("buy_deadline" in x for x in validate_config(cfg)))
cfg.risk.buy_deadline = "14:55:00"
cfg.watchlist.delete_guard_max_pct = 0.0
check("删除护栏比例越界校验",
      any("delete_guard" in x for x in validate_config(cfg)))
cfg.watchlist.delete_guard_max_pct = 0.30
cfg.monitor.watchdog_restart_max_daily = 99
check("看门狗重启次数越界校验",
      any("watchdog_restart_max_daily" in x for x in validate_config(cfg)))
cfg.monitor.watchdog_restart_max_daily = 3

# 13.2 裁定7: 单票每日重挂3次上限(首挂不计/买卖分别/queue_only旁路)
_rq_state = os.path.join(tmp, "logs", "rq_state.json")
if os.path.exists(_rq_state):
    os.remove(_rq_state)
cfg.risk.state_file = _rq_state
rq = RiskController(cfg.risk, project_root=tmp)
patch_dt(10, 0)
check("首次挂单不计重挂且放行", rq.pre_check("600850", "BUY", 100).ok)
rq.record_attempt("600850", "BUY", 10.0, "pending")
for _i in (1, 2):
    rq.set_attempt_status("600850", "BUY", "canceled")
    rq.record_attempt("600850", "BUY", 10.0, "pending")
    check(f"第{_i}次重挂放行且计数={_i}",
          rq.pre_check("600850", "BUY", 100).ok
          and rq.requeue_count("600850", "BUY") == _i)
rq.set_attempt_status("600850", "BUY", "canceled")
rq.record_attempt("600850", "BUY", 10.0, "pending")
_rq_block = rq.pre_check("600850", "BUY", 100)
check("第3次重挂达上限被拦截",
      rq.requeue_count("600850", "BUY") == 3
      and not _rq_block.ok and "重挂" in _rq_block.reason, _rq_block.reason)
check("重挂上限对queue_only结算单旁路",
      rq.pre_check("600850", "BUY", 100, queue_only=True).ok)
check("买卖重挂分别计数: SELL不受BUY次数影响",
      rq.requeue_count("600850", "SELL") == 0
      and rq.pre_check("600850", "SELL", 100).ok)
rcmod.datetime = datetime

# 13.3 台账同态幂等/跨态覆写提示
_rmsgs = []


class _RH(logging.Handler):
    def emit(self, record):
        _rmsgs.append(record.getMessage())


_rlg = logging.getLogger("risk")
_rh = _RH()
_rlg.addHandler(_rh)
_rlg.setLevel(logging.WARNING)
rq.record_attempt("600860", "SELL", 9.0, "filled")
_rmsgs.clear()
rq.record_attempt("600860", "SELL", 9.0, "filled")
check("filled->filled同态重复静默", not any("覆写" in m for m in _rmsgs))
rq.record_attempt("600861", "BUY", 9.0, "canceled")
_rmsgs.clear()
rq.record_attempt("600861", "BUY", 9.0, "filled")
check("canceled->filled跨态覆写仍提示", any("覆写" in m for m in _rmsgs))
_rlg.removeHandler(_rh)

# 13.4 裁定8: 云自选同步删除护栏(两条删除路径之前拦截)
from ths.watchlist import CloudWatchlist, WatchlistDeleteGuardError
_dg_cur = [(f"60090{i}", "17") for i in range(10)]
_dg = CloudWatchlist("userid=1; sessionid=2", timeout=2,
                     delete_guard_enable=True, delete_guard_max_pct=0.30)
_dg.list_self = lambda: _dg_cur
_v1_calls = []
_dg._replace_v1 = lambda codes: _v1_calls.append(codes)
_dg_raised = False
try:
    _dg.sync(["600900", "600901", "600902", "600903", "600904", "600905"])
except WatchlistDeleteGuardError:
    _dg_raised = True
check("待删6/10>30%中止且不触达删除接口", _dg_raised and _v1_calls == [])
_dg.sync(["600900", "600901", "600902", "600903",
          "600904", "600905", "600906"])
check("待删恰好30%放行v1", len(_v1_calls) == 1)
_dg_off = CloudWatchlist("userid=1; sessionid=2", timeout=2,
                         delete_guard_enable=False,
                         delete_guard_max_pct=0.30)
_dg_off.list_self = lambda: _dg_cur
_dg_off._replace_v1 = lambda codes: None
check("护栏开关可关(全删不拦)",
      len(_dg_off.sync(["600900"])["removed"]) == 9)

# 13.5 裁定1: 发键前价格守卫(默认只告警, 阈值/queue_only/fail-open)
_orig_logs_dir = cfg.paths.logs_dir
cfg.paths.logs_dir = os.path.join(tmp, "logs")
_orig_rp = qmod.realtime_price
qmod.realtime_price = lambda code, timeout=5.0: 10.08
_pg = ht._price_guard_check("600910", "BUY", "守卫股", 10.0)
check("买入现价高于信号+0.8%->bad",
      _pg and _pg["bad"] is True and abs(_pg["deviation"] - 0.008) < 1e-9)
qmod.realtime_price = lambda code, timeout=5.0: 10.07
_pg = ht._price_guard_check("600911", "BUY", "守卫股", 10.0)
check("买入+0.7%未越限", _pg and _pg["bad"] is False)
qmod.realtime_price = lambda code, timeout=5.0: 9.84
_pg = ht._price_guard_check("600912", "SELL", "守卫股", 10.0)
check("卖出现价低于信号-1.5%->bad", _pg and _pg["bad"] is True)
qmod.realtime_price = lambda code, timeout=5.0: 9.86
_pg = ht._price_guard_check("600913", "SELL", "守卫股", 10.0)
check("卖出-1.4%未越限", _pg and _pg["bad"] is False)
check("queue_only跳过价格守卫",
      ht._price_guard_check("600914", "BUY", "x", 10.0,
                            queue_only=True) is None)
qmod.realtime_price = lambda code, timeout=5.0: 0.0
check("取价失败fail-open放行",
      ht._price_guard_check("600915", "BUY", "x", 10.0) is None)
qmod.realtime_price = _orig_rp

# 13.6 F6检测质量滑窗(近20次异常率>=30%每日告一次, 区别于连续异常降级)
_fn = []
ht._notify = lambda title, content, level="INFO": _fn.append(level)
for _ok in [True] * 14 + [False] * 6:
    ht._f6_quality_sample(_ok)
check("F6滑窗异常率30%触发1次WARNING", _fn == ["WARNING"], str(_fn))
ht._f6_quality_sample(False)
check("F6质量告警当日边沿只告一次", len(_fn) == 1)

# 13.7 ClockGuard时钟偏差/陈旧/边沿/开关 + 双源时间戳解析
from monitor.clock_guard import ClockGuard
_cg = ClockGuard(30.0, 120.0, True)
# 固定工作日(2026-09-16周三)10:30时刻, 不随selftest运行钟变化(原time.time()
# 写法在15:05后/周末运行时in_window=False导致假FAIL, 与S9同类运行时刻依赖)
_now = time.mktime(time.strptime("2026-09-16 10:30:00",
                                 "%Y-%m-%d %H:%M:%S"))
check("行情时间偏差>30s->skew",
      _cg.check({"quote_ts": _now - 40, "local_ts": _now}) == "skew")
check("skew持续边沿不重复",
      _cg.check({"quote_ts": _now - 40, "local_ts": _now}) == "skew")
check("滞后>120s升级stale",
      _cg.check({"quote_ts": _now - 200, "local_ts": _now}) == "stale")
check("时钟恢复正常",
      _cg.check({"quote_ts": _now - 1, "local_ts": _now}) == "")
check("时钟守卫可关",
      ClockGuard(30.0, 120.0, False).check(
          {"quote_ts": _now - 999, "local_ts": _now}) == "")
check("腾讯紧凑时间戳解析/坏值回0",
      qmod._parse_compact_ts("20260916103000") > 0
      and qmod._parse_compact_ts("bad") == 0)
check("新浪日期+时间解析/坏值回0",
      qmod._parse_hms_ts("2026-09-16", "10:30:00") > 0
      and qmod._parse_hms_ts("x", "y") == 0)

# 13.8 R6 TradingGate只读聚合(/api/gate数据源)
from monitor.trading_gate import build_gate_snapshot
_gs = build_gate_snapshot(cfg)
check("闸门快照三向+计数结构",
      all(k in _gs for k in ("buy", "sell", "cancel", "watchdog",
                             "heartbeat", "today_counters")))
check("闸门计数采集到本轮守卫事件",
      _gs["today_counters"]["price_guard_bad"] == 2
      and _gs["today_counters"]["exec_quality"] == 2
      and _gs["today_counters"]["f6_quality"] == 1,
      str(_gs["today_counters"]))
_ks_gate = os.path.join(tmp, "logs", "gate_kill_switch.flag")
with open(_ks_gate, "w", encoding="utf-8") as _f:
    _f.write("selftest")
_orig_ks_cfg = cfg.risk.kill_switch_file
cfg.risk.kill_switch_file = _ks_gate
_gs2 = build_gate_snapshot(cfg)
check("kill_switch人工全停->买卖双向拦截",
      not _gs2["buy"]["allowed"] and not _gs2["sell"]["allowed"]
      and any("kill_switch" in b for b in _gs2["buy"]["blockers"]))
cfg.risk.kill_switch_file = _orig_ks_cfg

# 13.9 R1 trace_id/round_id贯穿下单三事件
import decision.decision as dmod
from models.audit import EventLog
_tdir = tempfile.mkdtemp(prefix="rules_trace_")
_el = EventLog(_tdir)
_orig_getel = dmod.get_event_log
dmod.get_event_log = lambda d: _el


class _TTrader:
    def execute_order(self, *a, **k):
        return {"ok": True, "filled_price": 10.0}


_eng = DecisionEngine(PositionStore(os.path.join(_tdir, "p.json")),
                      _TTrader(), _tdir, mode="paper", max_positions=4)
_eng.set_round(42)
_eng.execute(Action(ActionType.BUY, "600920", "LONG", "trace演练"))
_evs = _el.read_day()
_req = [e for e in _evs if e.get("stage") == "order_request"]
_res = [e for e in _evs if e.get("stage") == "order_result"]
check("request/result带同一trace_id",
      len(_req) == 1 and len(_res) == 1
      and _req[0]["trace_id"] == _res[0]["trace_id"]
      and len(_req[0]["trace_id"]) == 17)
check("round_id注入下单事件",
      _req[0]["round"] == 42 and _res[0]["round"] == 42)
dmod.get_event_log = _orig_getel

# 13.10 裁定5: 看门狗带护栏自动重启(时间窗/kill_switch/kill_buy/上限/preflight)
import monitor.heartbeat as hbmod
from monitor.heartbeat import Watchdog
_wd = Watchdog(cfg, notifier=None, config_path=os.path.join(ROOT, "config.yaml"))
_wd.restart_state_path = os.path.join(tmp, "logs", "wd_restart.json")
_wd.alerts_dir = os.path.join(tmp, "logs")
if os.path.exists(_wd.restart_state_path):
    os.remove(_wd.restart_state_path)
check("重启计数初始0", _wd._read_restart_state()["count"] == 0)
_lt0, _sf0 = hbmod.time.localtime, hbmod.time.strftime


def _patch_clock(ts_str):
    _st = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    hbmod.time.localtime = lambda ts=None: _st if ts is None else _lt0(ts)
    def _sf(fmt, t=None):
        if fmt == "%H:%M:%S":
            return ts_str[11:]
        if fmt == "%Y%m%d":
            return ts_str[:10].replace("-", "")
        return _sf0(fmt, t)
    hbmod.time.strftime = _sf


_patch_clock("2026-09-16 14:58:00")
check("14:58超过重启截止不重启", _wd._restart_time_ok() is False)
_patch_clock("2026-09-16 10:00:00")
check("工作日10:00在重启窗口", _wd._restart_time_ok() is True)
_patch_clock("2026-09-19 10:00:00")
check("周六不重启", _wd._restart_time_ok() is False)
_patch_clock("2026-09-16 10:00:00")
_hb = {"pid": 12345, "phase": "scan", "round": 9, "time": "10:00:00"}
_ks_wd = os.path.join(tmp, "logs", "wd_kill_switch.flag")
_kb_wd = os.path.join(tmp, "logs", "wd_kill_buy.flag")
_orig_ks2, _orig_kb2 = cfg.risk.kill_switch_file, cfg.risk.buy_halt_file
cfg.risk.kill_switch_file = _ks_wd
cfg.risk.buy_halt_file = _kb_wd
_run0, _popen0 = hbmod.subprocess.run, hbmod.subprocess.Popen
_rc, _pc = [], []
hbmod.subprocess.run = lambda *a, **k: _rc.append(a) or SimpleNamespace(
    returncode=0, stdout="", stderr="")
hbmod.subprocess.Popen = lambda *a, **k: _pc.append(a) or SimpleNamespace(
    pid=777)
with open(_ks_wd, "w", encoding="utf-8") as _f:
    _f.write("x")
check("kill_switch期间不重启(不跑preflight不拉起)",
      _wd._guarded_restart(_hb) is False and _rc == [] and _pc == [])
os.remove(_ks_wd)
with open(_kb_wd, "w", encoding="utf-8") as _f:
    _f.write("x")
check("kill_buy买侧熔断不阻止重启",
      _wd._guarded_restart(_hb) is True and len(_pc) == 1
      and "auto_round" in _pc[-1][0] and any("--config" in x
                                              for x in _pc[-1][0]))
check("重启计数落盘=1且宽限期内不重复拉起",
      _wd._read_restart_state()["count"] == 1
      and _wd._restart_in_flight(time.time()) is True)
os.remove(_kb_wd)
_st = _wd._read_restart_state()
_st.update(count=3, last_ts=0.0)
_wd._write_restart_state(_st)
_rc.clear()
_pc.clear()
check("当日重启达3次上限不再拉起",
      _wd._guarded_restart(_hb) is False and _rc == [] and _pc == [])
_st["count"] = 0
_wd._write_restart_state(_st)
hbmod.subprocess.run = lambda *a, **k: SimpleNamespace(
    returncode=2, stdout="", stderr="bad")
check("preflight returncode!=0不拉起",
      _wd._guarded_restart(_hb) is False and _pc == [])
hbmod.subprocess.run, hbmod.subprocess.Popen = _run0, _popen0
hbmod.time.localtime, hbmod.time.strftime = _lt0, _sf0
cfg.risk.kill_switch_file, cfg.risk.buy_halt_file = _orig_ks2, _orig_kb2
cfg.paths.logs_dir = _orig_logs_dir

# ---------- S14 尾盘集合竞价顶格报价热键(2026-09-16裁定) ----------
print("S14 尾盘竞价顶格报价热键")
# 14.1 默认配置: 开关开, 键位空(用户须先在同花顺绑定后填入)
check("顶格报价开关默认开", cfg.hotkey.closing_limit_key_enable is True)
check("顶格买键默认空", cfg.hotkey.closing_buy_limit_key == "")
check("顶格卖键默认空", cfg.hotkey.closing_sell_limit_key == "")
# 14.2 键选择: 未配置回退常规键+按方向告警去重; 配置后顶格; 盘中单不受影响
ht14 = htmod.HotkeyTrader(cfg, risk2)
check("未配置时尾盘买单回退F1",
      ht14._pick_key("BUY", closing_auction=True) == "{F1}")
check("未配置时尾盘卖单回退F3",
      ht14._pick_key("SELL", closing_auction=True) == "{F3}")
check("未配置告警按方向去重(再发不重复入集合)",
      ht14._pick_key("BUY", closing_auction=True) == "{F1}"
      and ht14._pick_key("SELL", closing_auction=True) == "{F3}"
      and len(ht14._closing_key_warned) == 2)
cfg.hotkey.closing_buy_limit_key = "{F9}"
cfg.hotkey.closing_sell_limit_key = "{F10}"
check("配置后尾盘买单走顶格键F9",
      ht14._pick_key("BUY", closing_auction=True) == "{F9}")
check("配置后尾盘卖单走顶格键F10",
      ht14._pick_key("SELL", closing_auction=True) == "{F10}")
check("盘中普通买单仍走F1(不受顶格配置影响)",
      ht14._pick_key("BUY") == "{F1}")
check("盘中跌停排队卖(非尾盘)仍走F3",
      ht14._pick_key("SELL") == "{F3}")
cfg.hotkey.closing_limit_key_enable = False
check("开关关闭后尾盘买单回常规键",
      ht14._pick_key("BUY", closing_auction=True) == "{F1}")
check("开关关闭后尾盘卖单回常规键",
      ht14._pick_key("SELL", closing_auction=True) == "{F3}")
cfg.hotkey.closing_limit_key_enable = True
cfg.hotkey.closing_buy_limit_key = ""
cfg.hotkey.closing_sell_limit_key = ""
# 14.3 schema: 键位格式形如{F9}, 空字符串合法(回退告警)
cfg.hotkey.closing_buy_limit_key = "F9"
check("顶格键缺花括号格式非法被schema收集",
      any("closing_buy_limit_key" in x for x in validate_config(cfg)))
cfg.hotkey.closing_buy_limit_key = "{F9}"
check("合法{键}配置无closing相关schema错误",
      not [x for x in validate_config(cfg) if "closing_" in x],
      str([x for x in validate_config(cfg) if "closing_" in x]))
cfg.hotkey.closing_buy_limit_key = ""
# 14.4 Action->决策层->trader透传
patch_dt(10, 0)
eng.execute(Action(ActionType.BUY, "600930", "LONG", "尾盘竞价买单透传",
                   "closing_auction", queue_only=True,
                   closing_auction=True))
check("尾盘结算买单透传closing_auction=True",
      tr.closing_flags[-1] == ("600930", "BUY", True))
eng.execute(Action(ActionType.SELL, "600931", "SHORT", "盘中跌停排队卖",
                   "goto", queue_only=True))
check("盘中跌停排队卖closing_auction=False",
      tr.closing_flags[-1] == ("600931", "SELL", False))
rcmod.datetime = datetime
# 14.5 scheduler尾盘结算构造的买卖单自动带closing_auction标记


class _FakeScanner14:
    def scan_one_goto(self, code):
        return StockResult(
            stock_code=code,
            signal=Signal.SHORT if code == "600935" else Signal.LONG,
            status=StockStatus.OK,
            detection=DetectionInfo(source="test"),
            capture_time=time.time())

    def _log_result(self, *a, **k):
        pass


class _Risk14:
    def all_attempts(self):
        return {"600935": {"SELL": {"status": "canceled"}},
                "600936": {"BUY": {"status": "canceled"}}}


class _Pos14:
    def codes(self):
        return ["600935"]


_captured14 = []


class _Decision14:
    max_positions = 4
    name_map = {}

    def _risk(self):
        return _Risk14()

    def execute(self, act):
        if act.type in (ActionType.BUY, ActionType.SELL):
            _captured14.append(act)


_sched14 = SimpleNamespace(decision=_Decision14(),
                           scanner=_FakeScanner14(),
                           positions=_Pos14())
_orig_rq14 = qmod.realtime_quote
qmod.realtime_quote = lambda code, timeout=5.0: {
    "halted": False, "at_limit_up": False, "price": 10.0}
schmod.AutoRoundScheduler._closing_settle(_sched14)
qmod.realtime_quote = _orig_rq14
_sell14 = next((a for a in _captured14 if a.code == "600935"), None)
_buy14 = next((a for a in _captured14 if a.code == "600936"), None)
check("尾盘结算兜底卖单带closing_auction+queue_only",
      _sell14 is not None and _sell14.closing_auction and _sell14.queue_only)
check("尾盘结算兜底买单带closing_auction+queue_only",
      _buy14 is not None and _buy14.closing_auction and _buy14.queue_only)

# ---------- S15 第二版十模型点评采纳(2026-09-18) ----------
print("S15 第二版点评采纳护栏")
import copy
import zipfile as _zfile
from config import (WatchlistConfig, PositionsConfig, HotkeyConfig,
                    RiskConfig as _RCdataclass, SchemaConfig, BackupConfig,
                    strict_violations, validate_config)

# 15.1 新增配置字段默认值(原则: 默认=现行行为, 新功能默认关)
_wc0 = WatchlistConfig()
check("删除护栏两段式默认关",
      _wc0.delete_guard_confirm_enable is False)
check("删除护栏hard_pct默认0.60",
      _wc0.delete_guard_hard_pct == 0.60)
_pc0 = PositionsConfig()
check("浮亏只读告警默认开",
      _pc0.loss_alert_enable is True)
check("浮亏告警阈值-10%/-15%",
      _pc0.loss_warn_pct == -0.10 and _pc0.loss_critical_pct == -0.15)
_hk0 = HotkeyConfig()
check("键位白名单默认开/allowed_keys空",
      _hk0.key_whitelist_enable is True and _hk0.allowed_keys == [])
check("F8 fail-open计数默认开/窗口1800/上限3",
      _hk0.cancel_fail_alert_enable is True
      and _hk0.cancel_fail_window_sec == 1800.0
      and _hk0.cancel_fail_max == 3)
check("收盘复查退避默认[5,600,1800]",
      _hk0.closing_recheck_delays == [5, 600, 1800])
check("撤单竞态F6终核默认关",
      _hk0.cancel_race_check_enable is False)
check("买熔断解除冷却默认0(立即解除)",
      _RCdataclass().buy_halt_recover_cooldown == 0.0)
check("unknown悬置15min/60min",
      _RCdataclass().unknown_stuck_critical_sec == 900.0
      and _RCdataclass().unknown_stuck_report_sec == 3600.0)
check("schema严格模式默认关",
      SchemaConfig().strict_enable is False)
_bu0 = BackupConfig()
check("每日备份默认关/保留30天",
      _bu0.enable is False and _bu0.retain_days == 30)

# 15.2 schema分级strict开关: 默认不阻断, 开启后只阻断risk/seal/timing段
check("strict默认关闭时无阻断", strict_violations(cfg) == [])
_rq_save15 = cfg.risk.max_requeue_per_day
_mode_save15 = cfg.execution.mode
cfg.risk.max_requeue_per_day = 99    # risk段越界
cfg.execution.mode = "bogus"         # ""段枚举问题
cfg.schema.strict_enable = True
_sv15 = strict_violations(cfg)
check("strict开启: risk段越界被阻断",
      any("max_requeue_per_day" in m for m in _sv15))
check("strict开启: 枚举(空段)问题不阻断",
      not any("bogus" in m for m in _sv15))
check("validate_config tagged返回(section,msg)元组",
      all(isinstance(t, tuple) and len(t) == 2
          for t in validate_config(cfg, tagged=True)))
cfg.schema.strict_enable = False
cfg.risk.max_requeue_per_day = _rq_save15
cfg.execution.mode = _mode_save15

# 15.3 unknown挂单悬置超时升级(独立风控副本, 不污染共享cfg)
_rcfg15 = copy.deepcopy(cfg.risk)
_rcfg15.state_file = os.path.join(tmp, "s15_state.json")
risk15 = RiskController(_rcfg15, project_root=tmp)
risk15.record_attempt("600900", "SELL", 10.0, "unknown")
risk15.get_attempt("600900", "SELL")["ts"] = time.time() - 1000
_ev15a = risk15.scan_unknown_stuck()
check("悬置≥15min升CRITICAL且<60min不报日报",
      len(_ev15a) == 1 and _ev15a[0]["level"] == "CRITICAL"
      and _ev15a[0]["stage"] == "unknown_stuck_critical")
check("悬置CRITICAL同档只触发一次",
      risk15.scan_unknown_stuck() == [])
risk15.record_attempt("600901", "BUY", 10.0, "unknown")
risk15.get_attempt("600901", "BUY")["ts"] = time.time() - 4000
_ev15b = risk15.scan_unknown_stuck()
_stages15 = sorted(e["stage"] for e in _ev15b)
check("悬置≥60min同批补report+critical",
      "unknown_stuck_report" in _stages15
      and "unknown_stuck_critical" in _stages15)

# 15.4 卖出笔数sell_count记录(超量只WARNING不拦卖)
risk15.record_order("600902", "SELL", ok=True)
check("卖出成功record_order计sell_count",
      risk15._state.get("sell_count") == 1)

# 15.5 键位白名单: 内置F1-F12+配置键放行, 非功能键拦截
ht15 = htmod.HotkeyTrader(cfg, risk2)
_set15 = ht15._allowed_key_set()
check("白名单内置含F1-F12",
      all(f"F{i}" in _set15 for i in range(1, 13)))
check("常规键F1/带括号F3放行",
      ht15._key_allowed("F1") and ht15._key_allowed("{F3}"))
check("非功能键XX被拦截", not ht15._key_allowed("XX"))
_save_cb15 = cfg.hotkey.closing_buy_limit_key
cfg.hotkey.closing_buy_limit_key = "{F11}"
check("配置的尾盘顶格键F11放行", ht15._key_allowed("{F11}"))
cfg.hotkey.closing_buy_limit_key = _save_cb15
_save_ak15 = cfg.hotkey.allowed_keys
cfg.hotkey.allowed_keys = ["F5"]
check("allowed_keys生效: F5放行/未配置F10拦",
      ht15._key_allowed("F5") and not ht15._key_allowed("F10"))
cfg.hotkey.allowed_keys = _save_ak15
ht15.notifier = None
for _ in range(cfg.hotkey.cancel_fail_max):
    ht15._record_cancel_failopen("600900")
check("F8 fail-open滑窗达阈值升级告警",
      ht15._cancel_failopen_alerted is True
      and len(ht15._cancel_failopen_ts) == cfg.hotkey.cancel_fail_max)

# 15.6 大盘买熔断解除冷却防抖
_mg_risk_cfg = copy.deepcopy(cfg.risk)
_mg_risk = RiskController(_mg_risk_cfg, project_root=tmp)
mg15 = MarketGuard(cfg, _mg_risk)
mg15._halt_date = time.strftime("%Y-%m-%d")
mg15._halt_idx = "sh000001"
mg15._halt_low = 3000.0
cfg.risk.buy_halt_recover_cooldown = 300.0
mg15._quote_index = lambda idx: {"price": 3031.0, "name": "上证指数"}
check("首次达阈进recovering观察",
      mg15._try_recover(cfg.risk, mg15._halt_date) == "recovering"
      and mg15._recover_resume_ts > 0)
check("观察期未持续站稳仍recovering",
      mg15._try_recover(cfg.risk, mg15._halt_date) == "recovering")
mg15._recover_resume_ts = time.time() - 301
check("站稳冷却后recovered并清熔断态",
      mg15._try_recover(cfg.risk, mg15._halt_date) == "recovered"
      and mg15._halt_date == "")
# 观察期内再跌穿应取消解除
mg15._halt_date = time.strftime("%Y-%m-%d")
mg15._halt_idx = "sh000001"
mg15._halt_low = 3000.0
mg15._try_recover(cfg.risk, mg15._halt_date)
mg15._quote_index = lambda idx: {"price": 3005.0, "name": "上证指数"}
check("观察期内再跌穿阈值取消解除",
      mg15._try_recover(cfg.risk, mg15._halt_date) == "halted"
      and mg15._recover_resume_ts == 0.0)
cfg.risk.buy_halt_recover_cooldown = 0.0

# 15.7 每日备份: 强制产出zip且不含凭据
from cleanup.backup import run_backup
_save_be15 = cfg.backup.enable
_save_bd15 = cfg.backup.dir
cfg.backup.enable = True
cfg.backup.dir = os.path.join(tmp, "backups")
_bres15 = run_backup(cfg, force=True)
check("备份强制产出zip(含config.yaml)",
      (not _bres15["skipped"]) and _bres15["files"] >= 1
      and os.path.isfile(_bres15["path"]))
_names15 = _zfile.ZipFile(_bres15["path"]).namelist()
check("备份zip不含cookie凭据",
      not any("cookie" in n.lower() for n in _names15))
cfg.backup.enable = _save_be15
cfg.backup.dir = _save_bd15

print(f"\n==== 结果: PASS={PASS} FAIL={FAIL} SKIP={NETSKIP} ====")
sys.exit(1 if FAIL else 0)
