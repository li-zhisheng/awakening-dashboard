"""本机时钟 vs 行情源时间戳守卫 (D类, 只告警不阻断, 2026-09-15)。

行情快照携带交易所时刻 quote_ts(腾讯成交时刻f30 / 新浪f30日期+f31时间)
与本机接收时刻 local_ts:
- 偏差大(默认|Δ|>30s): 本机时钟可能漂移, 而14:55买截止/14:57禁撤等
  硬门禁依赖本机时间, 提示校时;
- 陈旧大(默认Δ>120s, 仅行情滞后方向): 源站缓存/代理异常, 行情可能不新。
边沿去重(异常开始告一次, 恢复告一次), 仅WARNING, 不影响任何交易判定。
"""
import logging
import time

log = logging.getLogger("clock_guard")


class ClockGuard:
    def __init__(self, skew_warn_sec: float = 30.0,
                 stale_warn_sec: float = 120.0, enable: bool = True):
        self.skew_warn_sec = float(skew_warn_sec)
        self.stale_warn_sec = float(stale_warn_sec)
        self.enable = enable
        self._active = {"skew": False, "stale": False}

    @staticmethod
    def in_window(ts: float = 0.0) -> bool:
        """仅工作日 09:10-15:05 检测(盘后行情时刻天然陈旧, 告了无意义)。"""
        lt = time.localtime(ts or time.time())
        if lt.tm_wday >= 5:
            return False
        return "09:10:00" <= time.strftime("%H:%M:%S", lt) <= "15:05:00"

    def check(self, snap: dict) -> str:
        """检测一次快照, 返回当前异常态 ''/'skew'/'stale'。异常只打日志。"""
        if not self.enable or not snap:
            return ""
        try:
            qts = float(snap.get("quote_ts") or 0)
            lts = float(snap.get("local_ts") or 0)
        except (TypeError, ValueError):
            return ""
        if qts <= 0 or lts <= 0 or not self.in_window(lts):
            return ""
        skew = lts - qts
        kind = "stale" if skew > self.stale_warn_sec else (
            "skew" if abs(skew) > self.skew_warn_sec else "")
        for k in ("skew", "stale"):
            active = kind == k
            if active and not self._active[k]:
                self._alert(k, snap, skew)
            elif not active and self._active[k]:
                log.info("时钟守卫解除: %s %s 时间戳恢复正常",
                         snap.get("source", "?"), snap.get("name", ""))
            self._active[k] = active
        return kind

    def _alert(self, kind: str, snap: dict, skew: float):
        src = snap.get("source", "?")
        name = snap.get("name", "")
        if kind == "stale":
            log.warning("行情陈旧告警: %s %s 行情时刻滞后本机%.0fs"
                        "(>%.0fs), 可能源站缓存/代理, 行情决策注意时效性",
                        src, name, skew, self.stale_warn_sec)
        else:
            log.warning("时钟偏差告警: %s %s 行情时刻与本机差%.0fs"
                        "(|偏差|>%.0fs), 本机时钟可能漂移; "
                        "14:55买截止/14:57禁撤等硬门禁依赖本机时间, 请校时",
                        src, name, skew, self.skew_warn_sec)

    def check_once(self, code: str = "sh000001") -> str:
        """实时拉一次指数行情做时钟/时效检测(2026-09-18采纳, preflight节点用)。

        返回当前异常态 ''/'skew'/'stale'; 行情失败返回''(节点判为跳过不阻断)。
        """
        try:
            from ths.quote import realtime_quote
            q = realtime_quote(code, 5.0)
        except Exception:
            return ""
        if not q:
            return ""
        return self.check({
            "quote_ts": q.get("quote_ts"),
            "local_ts": time.time(),
            "source": q.get("source", "preflight"),
            "name": q.get("name", code),
        })
