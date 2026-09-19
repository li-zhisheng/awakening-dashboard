"""涨跌停封单监控: 后台轮询行情盘口, 监控开板/封单秒级异动。

规则(用户2026-09-12重新定义):
- 封单量较上一轮(10s)环比变动 >=10% 且封单基数 >=50手 -> 立即告警:
  骤减 CRITICAL(可能开板), 暴增 WARNING(封板加强); 同票纯告警180s冷却防刷屏
- 异动时若该股存在挂单(台账pending, 买/卖都算) -> 必须F8撤单, 每票只撤一次,
  撤单事件不受冷却限制; 涨停场景系统本就不排队买, 兜底的是尾盘queue_only单
- 开板(对手盘重新出现/价格离开涨跌停) -> 告警并摘除监控(一次性)
- 涨停侧任何事件都只告警, 绝不自动补单买入(人工判断)

纯PC侧HTTP行情, 不占手机通道。

收盘集合竞价硬门禁(2026-09-15用户裁定): 14:57-15:00(沪深一致)交易所规则
不允许撤单, F8会成废单; 该时段一切封单异动只告警, 挂单原样保留到15:00
收盘对账, 绝不发F8。
"""
import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("seal_watch")

SEAL_JUMP_RATIO = 0.10     # 封单量秒级变动10%即告警
MIN_SEAL_VOL = 50.0        # 封单基数低于50手不判变动(低流动性噪声保护)
POLL_INTERVAL = 10.0       # 轮询间隔(秒)
ALERT_COOLDOWN = 180.0     # 同票纯异动告警冷却(秒); 撤单事件不受此限
CLOSING_AUCTION_START = "14:57:00"   # 收盘集合竞价开始, 此刻起严禁F8撤单
CLOSING_AUCTION_END = "15:00:00"


class SealWatcher:
    def __init__(self, notifier=None, cancel_fn=None,
                 pending_fn=None, interval: float = POLL_INTERVAL):
        """
        notifier: notify.Notifier(可None, 只记日志)
        cancel_fn: callable(code, side)->bool, 异动撤单(F8), side=BUY/SELL
        pending_fn: callable(code)->""/"BUY"/"SELL", 查台账当前挂单方向
        """
        self.notifier = notifier
        self.cancel_fn = cancel_fn
        self.pending_fn = pending_fn
        self.interval = interval
        self._lock = threading.Lock()
        # code -> {name, last_vol, last_alert_ts, cancelled}
        self._up = {}
        self._down = {}
        self._stop = threading.Event()
        self._thread = None

    def watch_up(self, code: str, name: str = "", seal_vol: float = 0.0):
        """注册涨停多头监控(幂等; 已注册不重置基线)。"""
        with self._lock:
            if code not in self._up:
                self._up[code] = self._new_item(name, seal_vol)
                log.info("封单监控注册[涨停多头] %s %s 封单%.0f手",
                         code, name, seal_vol)
        self._ensure_thread()

    def watch_down(self, code: str, name: str = "", seal_vol: float = 0.0):
        """注册跌停持仓卖单监控(幂等)。"""
        with self._lock:
            if code not in self._down:
                self._down[code] = self._new_item(name, seal_vol)
                log.info("封单监控注册[跌停卖单] %s %s 封单%.0f手",
                         code, name, seal_vol)
        self._ensure_thread()

    @staticmethod
    def _new_item(name: str, seal_vol: float) -> dict:
        return {"name": name, "last_vol": float(seal_vol or 0),
                "last_alert_ts": 0.0, "cancelled": False,
                "close_blocked": False, "close_block_alerted": False}

    @staticmethod
    def _in_closing_auction() -> bool:
        """14:57:00-15:00:00收盘集合竞价(工作日): 交易所禁止撤单。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.strftime("%H:%M:%S")
        return CLOSING_AUCTION_START <= hhmm < CLOSING_AUCTION_END

    def watching_down(self, code: str) -> bool:
        with self._lock:
            return code in self._down

    def remove(self, code: str):
        with self._lock:
            self._up.pop(code, None)
            self._down.pop(code, None)

    def stop(self):
        self._stop.set()

    # ---------- 内部 ----------

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="seal-watcher")
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:
                log.warning("封单监控轮询异常: %s", e)
            self._stop.wait(self.interval)

    def _poll(self):
        from ths.quote import realtime_quote
        with self._lock:
            ups = list(self._up.items())
            downs = list(self._down.items())
        for code, item in ups:
            q = realtime_quote(code, 5.0)
            if not q:
                continue
            # 开板: 卖一重新出现 或 最新价离开涨停 -> 一次性告警摘除
            if (q.get("ask1_vol", 0) > 0
                    or q["price"] < q["limit_up"] - 0.005):
                side0 = self._pending_side(code)
                cancelled = (self._cancel(code, item, side0)
                             if side0 else False)
                if side0 and item.get("close_blocked"):
                    extra = (f"检测到{side0}挂单, 但收盘集合竞价禁撤, "
                             f"留单至15:00收盘对账")
                else:
                    extra = (f"检测到{side0}挂单, 已F8撤单" if cancelled
                             else f"检测到{side0}挂单, 撤单待核实" if side0 else "")
                self._fire_up(code, item["name"], "已开板", q["price"],
                              q.get("seal_vol", 0), critical=True,
                              extra=extra)
                with self._lock:
                    self._up.pop(code, None)
                continue
            self._check_jump(code, item, q, side="up")

        for code, item in downs:
            q = realtime_quote(code, 5.0)
            if not q:
                continue
            if (q.get("bid1_vol", 0) > 0
                    or q["price"] > q["limit_down"] + 0.005):
                side0 = self._pending_side(code)
                cancelled = (self._cancel(code, item, side0)
                             if side0 else False)
                if side0 and item.get("close_blocked"):
                    extra = ("收盘集合竞价禁撤, 排队卖单留至15:00收盘对账, "
                             "请人工关注")
                else:
                    extra = ("排队卖单已F8撤销, 请立即人工处理" if cancelled
                             else "撤单待核实, 请立即人工处理" if side0 else "")
                self._fire_down(code, item["name"], "已开板", q["price"],
                                q.get("seal_vol", 0), extra=extra)
                with self._lock:
                    self._down.pop(code, None)
                continue
            self._check_jump(code, item, q, side="down")

    def _check_jump(self, code: str, item: dict, q: dict, side: str):
        """封单量环比变动>=10%: 立即告警; 有挂单必撤。"""
        cur = float(q.get("seal_vol") or 0)
        base = item["last_vol"]
        if base < MIN_SEAL_VOL:
            item["last_vol"] = max(base, cur)
            return
        ratio = abs(cur - base) / base if base > 0 else 0
        if ratio < SEAL_JUMP_RATIO:
            return
        shrinking = cur < base
        kind = (f"封单骤减{ratio * 100:.0f}%({base:.0f}手→{cur:.0f}手)"
                if shrinking else
                f"封单暴增{ratio * 100:.0f}%({base:.0f}手→{cur:.0f}手)")
        item["last_vol"] = cur
        now = time.time()
        cooled = now - item["last_alert_ts"] >= ALERT_COOLDOWN
        pending_side = self._pending_side(code)
        # 有挂单必撤(每票一次), 撤单事件不受冷却限制; 无挂单走冷却
        if pending_side and not item["cancelled"]:
            cancelled = self._cancel(code, item, pending_side)
            blocked = item.get("close_blocked")
            if side == "up":
                if blocked:
                    extra = (f"检测到{pending_side}挂单, 但收盘集合竞价禁撤, "
                             f"留单至15:00收盘对账")
                else:
                    extra = (f"检测到{pending_side}挂单, "
                             f"{'已F8撤单' if cancelled else '撤单待核实'}")
                self._fire_up(code, item["name"], kind, q["price"], cur,
                              critical=True, extra=extra)
            else:
                if blocked:
                    extra = ("收盘集合竞价禁撤, 排队卖单留至15:00收盘对账, "
                             "请人工关注")
                else:
                    extra = (f"排队卖单{'已F8撤销' if cancelled else '撤单待核实'}, "
                             f"请立即人工处理")
                self._fire_down(code, item["name"], kind, q["price"], cur,
                                extra=extra)
            item["last_alert_ts"] = now
        elif cooled:
            if side == "up":
                self._fire_up(code, item["name"], kind, q["price"], cur,
                              critical=shrinking)
            else:
                # 跌停封单骤减=可能开板(最高优先); 暴增=封板加强
                self._fire_down(code, item["name"], kind, q["price"], cur,
                                critical=shrinking)
            item["last_alert_ts"] = now

    def _pending_side(self, code: str) -> str:
        if not self.pending_fn:
            return ""
        try:
            return self.pending_fn(code) or ""
        except Exception as e:
            log.warning("挂单状态查询失败 %s: %s", code, e)
            return ""

    def _cancel(self, code: str, item: dict, side: str) -> bool:
        """F8撤单; 每票只撤一次(item.cancelled)。

        收盘集合竞价14:57-15:00硬门禁: 交易所禁止撤单, F8是废单,
        只告警并留单到15:00收盘对账(每票门禁告警一次)。
        """
        if not side or item["cancelled"] or not self.cancel_fn:
            return False
        if self._in_closing_auction():
            item["close_blocked"] = True
            if not item.get("close_block_alerted"):
                item["close_block_alerted"] = True
                self._send(
                    f"收盘竞价禁撤·留单 {code} {item['name']}",
                    f"14:57-15:00收盘集合竞价交易所禁止撤单, 检测到{side}挂单"
                    f"但系统已硬拦截F8(强发即废单); 挂单原样保留至15:00收盘, "
                    f"请在收盘对账中核实该单成交情况。", True)
            log.warning("收盘集合竞价禁撤: %s %s挂单保留至15:00", code, side)
            return False
        item["cancelled"] = True
        try:
            return bool(self.cancel_fn(code, side))
        except Exception as e:
            log.error("封单异动撤单异常 %s: %s", code, e)
            return False

    def _send(self, title: str, content: str, critical: bool):
        level = "CRITICAL" if critical else "WARNING"
        log.log(logging.CRITICAL if critical else logging.WARNING,
                "%s | %s", title, content)
        if self.notifier:
            try:
                self.notifier.send(title, content, level=level)
            except Exception as e:
                log.warning("封单告警推送失败: %s", e)

    def _fire_up(self, code: str, name: str, kind: str, price: float,
                 seal_vol: float, critical: bool = True, extra: str = ""):
        """涨停侧: 只告警, 绝不自动补单(人工判断)。"""
        tail = extra or "系统未挂买单(涨停不排队), 请人工判断是否补单上车"
        self._send(
            f"涨停异动 {code} {name}",
            f"该股涨停封板后{kind}, 现价{price:.2f}, 封单{seal_vol:.0f}手。"
            f"{tail}。", critical)

    def _fire_down(self, code: str, name: str, kind: str, price: float,
                   seal_vol: float, critical: bool = True, extra: str = ""):
        """跌停侧: 告警人工卖出(挂单已由_check_jump撤掉)。"""
        tail = extra or "请立即人工以更好价格卖出"
        self._send(
            f"跌停异动 {code} {name}",
            f"该股跌停封单{kind}, 现价{price:.2f}, 封单{seal_vol:.0f}手。"
            f"{tail}。", critical)
