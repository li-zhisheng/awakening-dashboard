"""TradingGate 只读交易闸门总览 (R6, 2026-09-15)。

一屏回答"此刻系统允许什么": 聚合本机时间/交易时段/双层熔断(kill_switch
全停 vs kill_buy只禁买)/14:55尾盘买截止/收盘竞价禁撤/当日买入额度/
看门狗带护栏重启状态/心跳新鲜度, 以及当日新增质量事件计数
(价格守卫/F6降级对账/F6质量/删自选护栏/14:57禁撤留单)。

纯读文件、零副作用、零新依赖; 供 Web /api/gate 调用。
注意: 14:57的queue_only结算单旁路买截止与重挂上限, 本视图不反映该特例。
"""
import json
import logging
import os
import time

log = logging.getLogger("gate")


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _phase(hms: str, sessions) -> str:
    for start, end in sessions:
        if start <= hms <= end:
            return "连续竞价(上午)" if hms < "12:00:00" else "连续竞价(下午)"
    if "09:15:00" <= hms < "09:30:00":
        return "开盘集合竞价"
    if "09:25:00" <= hms < "09:30:00":
        return "开盘集合竞价(可挂单)"
    if "11:30:00" <= hms < "13:00:00":
        return "午间休市"
    if "14:57:00" <= hms <= "15:00:00":
        return "收盘集合竞价(禁F8撤单)"
    if hms < "09:15:00":
        return "盘前"
    return "盘后"


def _today_event_counters(cfg) -> dict:
    """从当日事件JSONL聚合本轮新增守卫的触发次数(文件缺失返回0)。"""
    counters = {
        "price_guard_bad": 0,    # 发键前价格偏差超阈(默认仅告警)
        "exec_quality": 0,       # 正常发单滑点台账样本
        "f6_fallback": 0,        # F6连续异常->xiadan只读对账
        "f6_quality": 0,         # F6滑窗异常率质量告警
        "cancel_blocked_closing": 0,  # 14:57后禁撤留单
        "watchlist_delete_guard": 0,   # 云自选删除护栏中止
    }
    try:
        from models.audit import EventLog
        events = EventLog(cfg.resolve(cfg.paths.logs_dir)).read_day()
    except Exception:
        return counters
    for e in events:
        stage = e.get("stage", "")
        if e.get("kind") == "trade":
            if stage == "price_guard":
                # price_guard 事件即超阈样本(未超阈的正常单发 exec_quality)
                counters["price_guard_bad"] += 1
            elif stage == "exec_quality":
                counters["exec_quality"] += 1
            elif stage == "f6_fallback_reconcile":
                counters["f6_fallback"] += 1
            elif stage == "f6_quality":
                counters["f6_quality"] += 1
            elif stage == "pending_cancel_blocked_closing":
                counters["cancel_blocked_closing"] += 1
        elif e.get("kind") == "anomaly" \
                and stage == "watchlist_delete_guard":
            counters["watchlist_delete_guard"] += 1
    return counters


def build_gate_snapshot(cfg) -> dict:
    r, m = cfg.risk, cfg.monitor
    now = time.time()
    lt = time.localtime(now)
    weekday = lt.tm_wday < 5
    hms = time.strftime("%H:%M:%S", lt)
    in_session = weekday and any(s <= hms <= e for s, e in r.sessions)

    kill_switch = os.path.exists(cfg.resolve(r.kill_switch_file))
    buy_halt = os.path.exists(cfg.resolve(r.buy_halt_file))
    closing = "14:57:00" <= hms <= "15:00:00"

    st = _read_json(cfg.resolve(r.state_file), {}) or {}
    order_count = int(st.get("order_count", 0) or 0)
    filled_buy_codes = len(st.get("daily_buys", {}) or {})
    deadline_on = bool(getattr(r, "buy_deadline_enable", True))
    deadline = getattr(r, "buy_deadline", "14:55:00") or ""
    past_deadline = bool(deadline_on and deadline and hms >= deadline)

    buy_block = []
    if kill_switch:
        buy_block.append("kill_switch人工全停")
    if buy_halt:
        buy_block.append("大盘-4%买侧熔断")
    if not weekday:
        buy_block.append("非工作日")
    if not in_session:
        buy_block.append("非交易时段")
    if past_deadline:
        buy_block.append(f"已过尾盘买截止{deadline}")
    if order_count >= r.max_orders_per_day:
        buy_block.append(f"当日买入委托数{order_count}>="
                         f"{r.max_orders_per_day}")

    sell_block = []
    if kill_switch:
        sell_block.append("kill_switch人工全停")
    if not weekday:
        sell_block.append("非工作日")
    if not in_session:
        sell_block.append("非交易时段")

    cancel_block = []
    if closing:
        cancel_block.append("14:57-15:00收盘集合竞价严禁F8撤单")

    # 看门狗重启状态
    rp = m.watchdog_restart_state
    rp = rp if os.path.isabs(rp) else os.path.join(cfg.project_root, rp)
    rs = _read_json(rp, {}) or {}
    today = time.strftime("%Y%m%d")
    restarts = int(rs.get("count", 0)) if rs.get("date") == today else 0
    can_restart = bool(
        m.watchdog_restart_enable and not kill_switch and weekday
        and hms < m.watchdog_restart_deadline
        and restarts < max(0, m.watchdog_restart_max_daily))

    # 心跳
    hb = _read_json(os.path.join(cfg.project_root, "logs",
                                 "heartbeat.json"), {}) or {}
    hb_age = round(now - float(hb.get("ts", 0)), 1) if hb.get("ts") else None
    hb_fresh = (hb_age is not None
                and hb_age <= max(30.0, m.heartbeat_stale))

    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S", lt),
        "weekday": weekday,
        "phase": _phase(hms, r.sessions),
        "in_session": in_session,
        "kill_switch": kill_switch,
        "buy_halt": buy_halt,
        "closing_auction": closing,
        "buy": {
            "allowed": not buy_block,
            "blockers": buy_block,
            "deadline": deadline if deadline_on else "",
            "orders_today": order_count,
            "max_orders_per_day": r.max_orders_per_day,
            "filled_buy_codes": filled_buy_codes,
        },
        "sell": {"allowed": not sell_block, "blockers": sell_block},
        "cancel": {"allowed": not cancel_block, "blockers": cancel_block},
        "watchdog": {
            "restart_enable": m.watchdog_restart_enable,
            "restarts_today": restarts,
            "max_daily": m.watchdog_restart_max_daily,
            "deadline": m.watchdog_restart_deadline,
            "can_restart_now": can_restart,
        },
        "heartbeat": {
            "age_sec": hb_age, "fresh": hb_fresh,
            "pid": hb.get("pid"), "phase": hb.get("phase"),
            "round": hb.get("round"),
        },
        "today_counters": _today_event_counters(cfg),
        "notes": [
            "14:57-15:00的queue_only收盘结算单旁路买截止/重挂上限",
            "卖出(风控止损)不占当日买入委托额度",
            "kill_buy买侧熔断不拦看门狗自动重启; kill_switch全停才拦",
        ],
    }
