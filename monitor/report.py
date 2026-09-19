"""盘中日报: 午盘休息(11:30-13:00)自动推送一次系统运行摘要。

内容: 持仓/今日轮次与覆盖率/信号生命周期统计/交易记录/告警条数/心跳状态。
数据源: positions.json + logs/events_YYYYMMDD.jsonl + alerts.jsonl。
幂等: logs/lunch_report.date 记录已发送日期, 每交易日只发一次。
"""
import logging
import os
import time
from datetime import datetime

from models.audit import EventLog

log = logging.getLogger("report")

GUARD_FILE = "logs/lunch_report.date"
LUNCH_START = "11:30:00"
LUNCH_END = "13:00:00"

CLOSING_GUARD_FILE = "logs/closing_report.date"
CLOSING_START = "15:05:00"   # 15:00排队单F6复查(每只~6s)之后
CLOSING_END = "15:30:00"


def _is_trading_day_now() -> bool:
    now = datetime.now()
    return now.weekday() < 5


def _in_lunch_window() -> bool:
    hhmm = time.strftime("%H:%M:%S")
    return LUNCH_START <= hhmm <= LUNCH_END


def _fmt_pos(p: dict) -> str:
    return (f"{p.get('code')} {p.get('name','')} "
            f"成本{p.get('entry_price', 0) or '—'} "
            f"建于{p.get('entry_time','')} {p.get('note','')}")


def _today_boundaries():
    """今日0点/11:30/13:00 epoch(用于成交分时段)。"""
    d = datetime.now()
    t0 = datetime(d.year, d.month, d.day).timetuple()
    return (time.mktime(t0),
            time.mktime(datetime(d.year, d.month, d.day, 11, 30).timetuple()),
            time.mktime(datetime(d.year, d.month, d.day, 13).timetuple()))


def _resolve_names(cfg, codes, positions=None) -> dict:
    """code->名称: 持仓 > 今日事件 > 热榜快照。"""
    names = {}
    if positions is not None:
        for p in positions.snapshot():
            names[p.get("code", "")] = p.get("name", "")
    try:
        el = EventLog(cfg.resolve(cfg.paths.logs_dir), enabled=False)
        for e in el.read_day():
            c = e.get("code")
            if c in codes and e.get("name"):
                names.setdefault(c, e["name"])
    except Exception:
        pass
    try:
        import json
        with open(os.path.join(cfg.resolve(cfg.paths.logs_dir),
                               "hotlist_local.json"), "r",
                  encoding="utf-8") as f:
            for s in json.load(f):
                names.setdefault(str(s.get("code", "")).zfill(6),
                                 s.get("name", ""))
    except Exception:
        pass
    return names


def _filled_trades(cfg, t_start: float = 0, t_end: float = 0):
    """台账中filled成交(按时间段筛选), 返回 [(ts,act,code,price)] 时间正序。"""
    attempts = _read_attempts(cfg)
    out = []
    for code, a in attempts.items():
        for act in ("BUY", "SELL"):
            t = a.get(act)
            if not t or t.get("status") != "filled":
                continue
            try:
                ts = float(t.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if t_start and ts < t_start:
                continue
            if t_end and ts > t_end:
                continue
            out.append((ts, act, code, float(t.get("price") or 0)))
    out.sort(key=lambda x: x[0])
    return out


def _fmt_trade_rows(trades: list, names: dict) -> list:
    rows = []
    for ts, act, code, price in trades:
        hhmm = time.strftime("%H:%M:%S", time.localtime(ts))
        arrow = "买入" if act == "BUY" else "卖出"
        rows.append(f"  {hhmm} {arrow} {code} {names.get(code,'')} @{price:.2f}")
    return rows


def _fmt_pos_with_quote(p: dict) -> str:
    """持仓行+现价涨跌幅+浮盈(双源行情, 失败降级只显成本)。"""
    line = f"  {_fmt_pos(p)}"
    try:
        from ths.quote import realtime_quote
        q = realtime_quote(p.get("code", ""), 4.0)
        if q and q.get("price"):
            cp = q["price"]
            line += f" | 现价{cp:.2f}({q.get('pct', 0):+.2f}%)"
            ep = p.get("entry_price") or 0
            if ep:
                line += f" 浮盈{(cp - ep) / ep * 100:+.2f}%"
    except Exception:
        pass
    return line


def _market_lines(title: str, with_losers: bool = False) -> list:
    """大盘概貌段(失败返回[]不阻断日报)。"""
    try:
        from ths.market import market_overview, format_overview
        ov = market_overview(top_sectors=5,
                             bottom_sectors=3 if with_losers else 0)
        return format_overview(ov, title)
    except Exception as e:
        log.warning("大盘概貌获取失败: %s", e)
        return []


def build_report(cfg, positions, rounds_today: int) -> str:
    """组装日报文本(保证含"告警"二字, 兼容群机器人关键词过滤)。"""
    el = EventLog(cfg.resolve(cfg.paths.logs_dir), enabled=False)
    events = el.read_day()
    scans = [e for e in events if e.get("kind") == "scan"]
    sigs = [e for e in events if e.get("kind") == "signal"]
    covs = [e for e in events if e.get("kind") == "coverage"]
    anomalies = [e for e in events if e.get("kind") == "anomaly"]

    longs = [e for e in sigs if e.get("to") == "LONG"]
    shorts = [e for e in sigs if e.get("to") == "SHORT"]
    cov_bad = [e for e in covs if e.get("coverage", 1.0) < cfg.monitor.coverage_min]

    # 告警条数(含决策告警/看门狗等, 本地alerts.jsonl当日行数)
    alert_count = 0
    alerts_path = os.path.join(cfg.resolve(cfg.paths.logs_dir), "alerts.jsonl")
    try:
        today = time.strftime("%Y-%m-%d")
        with open(alerts_path, "r", encoding="utf-8") as f:
            alert_count = sum(1 for line in f if f'"{today}' in line)
    except OSError:
        pass

    pos_list = positions.snapshot()
    # 上午成交台账(09:25-11:30 filled)
    t0, t_noon, _ = _today_boundaries()
    am_trades = _filled_trades(cfg, t_start=t0 - 3600, t_end=t_noon)
    am_buys = [t for t in am_trades if t[1] == "BUY"]
    am_sells = [t for t in am_trades if t[1] == "SELL"]
    names = _resolve_names(
        cfg, {t[2] for t in am_trades} | {p.get("code") for p in pos_list},
        positions)

    lines = [
        f"盘中日报(上午) {time.strftime('%Y-%m-%d %H:%M')} "
        f"(模式={cfg.execution.mode})",
    ]
    ml = _market_lines("上午大盘")
    lines += (ml + [""]) if ml else []
    lines += [f"持仓 {len(pos_list)}/{cfg.positions.max_positions} 只 "
              f"(现价/浮盈为上午收盘):"]
    lines += [_fmt_pos_with_quote(p) for p in pos_list] or ["  (空仓)"]
    lines += ["",
              f"上午成交: 买入{len(am_buys)}笔 卖出{len(am_sells)}笔"]
    lines += _fmt_trade_rows(am_trades, names) or ["  (无成交)"]
    lines += [
        "",
        f"今日轮次: {rounds_today} | 扫描{len(scans)}只次",
        f"覆盖率: {len(covs)}轮, 低于下限{len(cov_bad)}轮"
        + (f" (最近: {cov_bad[-1].get('coverage')})" if cov_bad else ""),
        f"信号: 多{len(longs)} 空{len(shorts)}",
    ]
    lines += [f"  多: {e.get('code')} {e.get('name')} @{e.get('time')}"
              for e in longs[-5:]]
    lines += [f"  空: {e.get('code')} {e.get('name')} @{e.get('time')}"
              for e in shorts[-5:]]
    lines += [
        f"运行异常: {len(anomalies)}次 | 今日告警: {alert_count} 条",
        "午盘休息中, 13:00恢复扫描。",
    ]
    return "\n".join(lines)


def _in_window(start: str, end: str) -> bool:
    hhmm = time.strftime("%H:%M:%S")
    return start <= hhmm <= end


def _read_attempts(cfg) -> dict:
    """读当日挂单台账终态(直接解析trade_state.json, 不构造RiskController)。"""
    import json
    path = cfg.resolve(cfg.risk.state_file)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == time.strftime("%Y-%m-%d"):
            return data.get("attempts", {}) or {}
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _fmt_ts(t: dict) -> str:
    """台账ts为epoch浮点, 显示HH:MM:SS; 容错字符串。"""
    v = t.get("ts")
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(v)))
    except (TypeError, ValueError):
        return str(v or "")[-8:]


def build_closing_report(cfg, positions, rounds_today: int) -> str:
    """组装收盘日报: 持仓收盘价/台账终态/尾盘结算/成交/信号/告警。"""
    el = EventLog(cfg.resolve(cfg.paths.logs_dir), enabled=False)
    events = el.read_day()
    sigs = [e for e in events if e.get("kind") == "signal"]
    trades = [e for e in events if e.get("kind") == "trade"]
    longs = [e for e in sigs if e.get("to") == "LONG"]
    shorts = [e for e in sigs if e.get("to") == "SHORT"]
    # 成交以挂单台账终态为准(含尾盘竞价/跌停排队的15:00补账)
    attempts = _read_attempts(cfg)
    filled_b, filled_s, pending, canceled, unknown = [], [], [], [], []
    for code, a in attempts.items():
        for act in ("BUY", "SELL"):
            t = a.get(act)
            if not t:
                continue
            row = f"{code} @{t.get('price', 0):.2f} {_fmt_ts(t)}"
            st = t.get("status")
            if st == "filled":
                (filled_b if act == "BUY" else filled_s).append(row)
            elif st == "pending":
                pending.append(f"{act} {row}")
            elif st == "canceled":
                canceled.append(f"{act} {row}")
            elif st == "unknown":
                unknown.append(f"{act} {row}")
    # 尾盘结算事件
    closing_events = [e for e in trades
                      if e.get("stage", "").startswith("closing")
                      or e.get("source") == "closing_auction"]
    closing_filled = [e for e in trades
                      if e.get("stage") in ("closing_filled",)]
    closing_unfilled = [e for e in trades
                        if e.get("stage") == "closing_unfilled"]

    alert_count = 0
    alerts_path = os.path.join(cfg.resolve(cfg.paths.logs_dir), "alerts.jsonl")
    try:
        today = time.strftime("%Y-%m-%d")
        with open(alerts_path, "r", encoding="utf-8") as f:
            alert_count = sum(1 for line in f if f'"{today}' in line)
    except OSError:
        pass

    pos_list = positions.snapshot()
    # 成交流水分上午/下午(台账filled, 含15:00尾盘补账)
    t0, t_noon, _ = _today_boundaries()
    day_trades = _filled_trades(cfg, t_start=t0 - 3600)
    am_trades = [t for t in day_trades if t[0] <= t_noon]
    pm_trades = [t for t in day_trades if t[0] > t_noon]
    names = _resolve_names(
        cfg, {t[2] for t in day_trades} | {p.get("code") for p in pos_list},
        positions)

    lines = [
        f"收盘日报 {time.strftime('%Y-%m-%d %H:%M')} (模式={cfg.execution.mode})",
        f"今日轮次: {rounds_today} | 信号: 多{len(longs)} 空{len(shorts)}"
        f" | 告警: {alert_count}条",
    ]
    ml = _market_lines("全天大盘", with_losers=True)
    lines += (ml + [""]) if ml else []
    lines += [
        "今日成交明细:",
        f"  上午(买{sum(1 for t in am_trades if t[1]=='BUY')}/"
        f"卖{sum(1 for t in am_trades if t[1]=='SELL')}):",
    ]
    lines += _fmt_trade_rows(am_trades, names) or ["    (无)"]
    lines += [
        f"  下午(买{sum(1 for t in pm_trades if t[1]=='BUY')}/"
        f"卖{sum(1 for t in pm_trades if t[1]=='SELL')}):",
    ]
    lines += _fmt_trade_rows(pm_trades, names) or ["    (无)"]
    lines += ["",
              f"挂单台账终态: 买成{len(filled_b)} 卖成{len(filled_s)} "
              f"撤单{len(canceled)} 挂单中{len(pending)} 状态不明{len(unknown)}"]
    if pending:
        lines.append("  ⚠仍挂单中(需人工核对): " + ", ".join(pending))
    if unknown:
        lines.append("  ⚠状态不明(明早恢复门拦截): " + ", ".join(unknown))
    if closing_filled or closing_unfilled:
        lines.append(f"尾盘竞价: 成交{len(closing_filled)} "
                     f"未成交{len(closing_unfilled)}")
    elif closing_events:
        lines.append("尾盘结算: 已执行(无新增竞价挂单)")
    lines += ["", f"持仓 {len(pos_list)}/{cfg.positions.max_positions} 只 "
                  f"(收盘价/浮盈):"]
    for p in pos_list:
        line = _fmt_pos_with_quote(p).replace("| 现价", "| 收盘")
        lines.append(line)
    if not pos_list:
        lines.append("  (空仓)")
    lines.append("今日交易结束, 系统继续监控(不交易)。")
    return "\n".join(lines)


def maybe_send_closing_report(cfg, positions, notifier, rounds_today: int = 0):
    """15:05-15:30窗口自动发一次收盘日报(幂等)。"""
    if not _is_trading_day_now() or not _in_window(CLOSING_START, CLOSING_END):
        return False
    guard = cfg.resolve(CLOSING_GUARD_FILE)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(guard, "r", encoding="utf-8") as f:
            if f.read().strip() == today:
                return False
    except OSError:
        pass

    text = build_closing_report(cfg, positions, rounds_today)
    os.makedirs(os.path.dirname(guard), exist_ok=True)
    try:
        with open(guard, "w", encoding="utf-8") as f:
            f.write(today)
        out = cfg.resolve(f"logs/closing_report_{time.strftime('%Y%m%d')}.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        log.warning("收盘日报落盘失败: %s", e)
    log.info("收盘日报:\n%s", text)
    if notifier and notifier.enabled():
        try:
            notifier.send(f"告警-收盘日报 {time.strftime('%m-%d')}", text,
                          level="INFO")
        except Exception as e:
            log.warning("收盘日报推送失败: %s", e)
    return True


def maybe_send_lunch_report(cfg, positions, notifier, rounds_today: int = 0):
    """午盘窗口内自动发一次盘中日报(幂等)。"""
    if not cfg.monitor.enable or not cfg.monitor.lunch_report:
        return False
    if not _is_trading_day_now() or not _in_lunch_window():
        return False
    guard = cfg.resolve(GUARD_FILE)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(guard, "r", encoding="utf-8") as f:
            if f.read().strip() == today:
                return False
    except OSError:
        pass

    text = build_report(cfg, positions, rounds_today)
    os.makedirs(os.path.dirname(guard), exist_ok=True)
    try:
        with open(guard, "w", encoding="utf-8") as f:
            f.write(today)
        out = cfg.resolve(f"logs/lunch_report_{time.strftime('%Y%m%d')}.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        log.warning("日报落盘失败: %s", e)
    log.info("盘中日报:\n%s", text)
    if notifier and notifier.enabled():
        try:
            notifier.send(f"告警-盘中日报 {time.strftime('%m-%d')}", text,
                          level="INFO")
        except Exception as e:
            log.warning("日报推送失败: %s", e)
    return True
