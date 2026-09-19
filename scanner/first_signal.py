"""首次信号快照: 每只股票当日首次判为多/空时, 记录检测时间+瞬时价+涨幅。

数据落盘 logs/first_signal_YYYYMMDD.jsonl, 一行一事件:
  {"ts":"2026-09-12 10:03:21","code":"600487","name":"亨通光电",
   "dir":"多","price":5.67,"prev_close":5.60,"pct":1.25}

- 去重: (code,dir)为键, 当日进程重启不重复记录(seen从当日jsonl重建)
- 价格: PC侧腾讯实时行情, 与检测时刻差约1s, 不占用手机通道
- 任何异常只记warning, 绝不影响扫描主流程
"""
import json
import logging
import os
import time

from models.result import Signal, StockStatus

log = logging.getLogger("first_signal")


class FirstSignalRecorder:
    """挂到 StockScanner._detect_on_frame 末尾, 每个结果过一遍。"""

    def __init__(self, cfg, logs_dir: str = ""):
        self.cfg = cfg
        self._logs_dir = logs_dir or cfg.resolve(cfg.paths.logs_dir)
        self._day = ""
        self._seen = set()   # (code, dir)

    def _roll_day(self):
        """跨日重置seen; seen从当日已有文件重建(重启安全)。"""
        day = time.strftime("%Y%m%d")
        if day == self._day:
            return
        self._day = day
        self._seen = set()
        f = self._file()
        if os.path.isfile(f):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            self._seen.add((d.get("code"), d.get("dir")))
                        except ValueError:
                            continue
            except OSError as e:
                log.warning("首次信号快照读取失败: %s", e)

    def _file(self) -> str:
        return os.path.join(self._logs_dir, f"first_signal_{self._day}.jsonl")

    def record(self, result):
        """首次出现多/空时记一行快照; 其余情况静默返回。"""
        try:
            if result.status != StockStatus.OK:
                return
            if result.signal not in (Signal.LONG, Signal.SHORT):
                return
            code = result.stock_code
            if not code or code == "unknown":
                return
            self._roll_day()
            direction = result.signal.label          # 多 / 空
            if (code, direction) in self._seen:
                return
            from ths.quote import realtime_quote
            q = realtime_quote(code, 5.0) or {}
            price = float(q.get("price") or 0)
            prev = float(q.get("prev_close") or 0)
            pct = round((price / prev - 1) * 100, 2) \
                if price > 0 and prev > 0 else 0.0
            row = {
                "ts": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(result.capture_time)),
                "ts_epoch": round(result.capture_time, 3),
                "code": code,
                "name": q.get("name", ""),
                "dir": direction,
                "price": round(price, 3) if price else 0.0,
                "prev_close": round(prev, 3) if prev else 0.0,
                "pct": pct,
            }
            os.makedirs(self._logs_dir, exist_ok=True)
            with open(self._file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._seen.add((code, direction))
            log.info("首次信号快照: %s %s %s 价%.3f 昨收%.3f 涨幅%.2f%%",
                     code, row["name"], direction, price, prev, pct)
        except Exception as e:   # 记录失败绝不影响扫描
            log.warning("首次信号快照记录失败(%s): %s",
                        getattr(result, "stock_code", "?"), e)
