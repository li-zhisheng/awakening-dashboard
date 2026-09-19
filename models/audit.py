"""运行审计事件日志: 单一JSONL文件按日轮转, 回测/对账/追责的统一数据源。

文件: logs/events_YYYYMMDD.jsonl, 每行一个JSON事件:
  {"time": "...", "ts": 169..., "kind": "...", ...字段}

事件种类(kind):
  scan      每只股票扫描时间线(时间戳+信号+置信度, 回测主数据)
  signal    信号生命周期: 当天信号状态变化 none->LONG/LONG->SHORT等
  trade     交易状态机: order_request/order_result/position_add/
            position_remove/pending_filled/pending_canceled/order_failed/
            decision(含"满仓不开新仓"等抑制原因, 可解释每笔没买)
  coverage  每轮扫描覆盖率(实扫成功数/计划数), <coverage_min记异常
  anomaly   运行异常(扫描卡死/轮中止等)

设计: 追加写+进程内锁, 单文件无索引; 当天量级约几千行, 直接grep/读入
即可分析。SignalTracker启动时重放当天signal事件恢复状态, 重启不丢生命周期。
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("audit")


def _today() -> str:
    return time.strftime("%Y%m%d")


class EventLog:
    """线程安全的按日JSONL事件日志。"""

    def __init__(self, logs_dir: str, enabled: bool = True):
        self.logs_dir = logs_dir
        self.enabled = enabled
        self._lock = threading.Lock()
        self._date = _today()
        self._path = self._file_for(self._date)

    def _file_for(self, date: str) -> str:
        return os.path.join(self.logs_dir, f"events_{date}.jsonl")

    @property
    def path(self) -> str:
        return self._path

    def log(self, kind: str, **fields) -> dict:
        """追加一条事件, 返回写入的dict。失败只记日志, 不抛异常。"""
        evt = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "ts": round(time.time(), 3), "kind": kind}
        evt.update(fields)
        if not self.enabled:
            return evt
        try:
            with self._lock:
                if _today() != self._date:          # 跨天轮转
                    self._date = _today()
                    self._path = self._file_for(self._date)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("事件写入失败(%s): %s", kind, e)
        return evt

    def read_day(self, date: str = "") -> list:
        """读回某天全部事件(默认今天), 文件缺失返回空list。"""
        path = self._file_for(date or _today())
        if not os.path.isfile(path):
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError as e:
            log.warning("事件读取失败: %s", e)
        return out

    def cursor(self) -> dict:
        """返回当前各事件文件的字节偏移游标 {events_YYYYMMDD.jsonl: size}。

        供持仓滚动快照记录(2026-09-18第二版点评采纳, 智谱A5): 损坏重建时
        从字节偏移而非时间戳重放, 杜绝同秒事件漏放。写入均为整行append,
        文件长度恒在换行边界上。在事件锁内测长度, 与append串行。
        """
        out = {}
        try:
            import glob
            with self._lock:
                for path in sorted(glob.glob(
                        os.path.join(self.logs_dir, "events_*.jsonl"))):
                    try:
                        out[os.path.basename(path)] = os.path.getsize(path)
                    except OSError:
                        pass
        except Exception as e:
            log.warning("事件游标采集失败: %s", e)
        return out


_shared: EventLog = None
_shared_lock = threading.Lock()


def get_event_log(logs_dir: str) -> EventLog:
    """进程级共享事件日志(单一写入实例, 避免多实例append交错)。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = EventLog(logs_dir)
        return _shared


class SignalTracker:
    """信号生命周期: 每只股票当天状态变化 -> signal事件。

    状态序列举例: (首次检测)LONG -> NONE -> SHORT(卖出依据) -> NONE。
    启动时重放当天signal事件恢复状态, 进程重启不产生重复"首次"事件。
    """

    def __init__(self, event_log: EventLog):
        self.el = event_log
        self._state = {}          # code -> 最新信号值
        self._load_today()

    def _load_today(self):
        for evt in self.el.read_day():
            if evt.get("kind") == "signal" and evt.get("code"):
                self._state[evt["code"]] = evt.get("to")

    def update(self, code: str, signal_value: str, name: str = "",
               confidence: float = 0.0, source: str = "",
               scan_ts: float = 0.0) -> bool:
        """更新一只股票信号状态, 状态变化时写事件, 返回是否有变化。"""
        if not code or code == "unknown":
            return False
        prev = self._state.get(code)
        if prev == signal_value:
            return False
        self._state[code] = signal_value
        self.el.log("signal", code=code, name=name,
                    **{"from": prev or "NEW", "to": signal_value},
                    confidence=round(confidence or 0.0, 3),
                    source=source, scan_ts=round(scan_ts or time.time(), 3))
        return True

    def state_of(self, code: str):
        return self._state.get(code)


def write_alert_file(alerts_dir: str, code: str, signal: str,
                     reason: str, source: str = ""):
    """只写本地alerts.jsonl(不推手机), 供覆盖率异常等低级别记录复用。"""
    try:
        os.makedirs(alerts_dir, exist_ok=True)
        path = os.path.join(alerts_dir, "alerts.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "code": code, "signal": signal,
                "reason": reason, "source": source,
            }, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("告警写入失败: %s", e)
