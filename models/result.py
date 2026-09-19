"""数据模型: 信号枚举、单只股票结果、扫描汇总统计。"""
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        """中文显示名: 多/空/无/未知。无与未知后续处理逻辑一致, 但状态独立。"""
        return _SIGNAL_LABELS[self.value]


_SIGNAL_LABELS = {
    "LONG": "多",
    "SHORT": "空",
    "NONE": "无",
    "UNKNOWN": "未知",
}


class StockStatus(str, Enum):
    OK = "OK"
    UNKNOWN = "UNKNOWN"
    DEVICE_LOST = "DEVICE_LOST"


@dataclass
class DetectionInfo:
    """单次识别的过程信息, 用于调试与统计。"""
    source: str = "none"          # ui_hierarchy / vision / screenshot / none
    state: str = "unavailable"    # found / not_found / unavailable
    confidence: float = 0.0
    bbox: Optional[List[int]] = None  # [x, y, w, h] 全屏坐标
    error: str = ""
    notes: str = ""


@dataclass
class StockResult:
    stock_code: str = ""
    start_time: float = 0.0
    switch_complete_time: float = 0.0
    capture_time: float = 0.0
    detect_complete_time: float = 0.0
    signal: Signal = Signal.UNKNOWN
    elapsed_time: float = 0.0
    status: StockStatus = StockStatus.UNKNOWN
    error: str = ""
    detection: DetectionInfo = field(default_factory=DetectionInfo)

    def finalize(self):
        if self.detect_complete_time and self.start_time:
            self.elapsed_time = round(self.detect_complete_time - self.start_time, 3)


@dataclass
class ScanSummary:
    total_stocks: int = 0
    success_count: int = 0
    unknown_count: int = 0
    long_count: int = 0
    short_count: int = 0
    none_count: int = 0
    total_elapsed: float = 0.0
    average_elapsed: float = 0.0
    min_elapsed: float = 0.0
    max_elapsed: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    p95: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_stocks": self.total_stocks,
            "success_count": self.success_count,
            "unknown_count": self.unknown_count,
            "long_count": self.long_count,
            "short_count": self.short_count,
            "none_count": self.none_count,
            "total_elapsed": round(self.total_elapsed, 3),
            "average_elapsed": round(self.average_elapsed, 3),
            "min_elapsed": round(self.min_elapsed, 3),
            "max_elapsed": round(self.max_elapsed, 3),
            "p50": round(self.p50, 3),
            "p90": round(self.p90, 3),
            "p95": round(self.p95, 3),
        }


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def build_summary(results: List[StockResult], total_elapsed: float) -> ScanSummary:
    ok = [r for r in results if r.status == StockStatus.OK]
    elapsed = [r.elapsed_time for r in results if r.elapsed_time > 0]
    summary = ScanSummary(
        total_stocks=len(results),
        success_count=len(ok),
        unknown_count=sum(1 for r in results if r.status == StockStatus.UNKNOWN),
        long_count=sum(1 for r in results if r.signal == Signal.LONG),
        short_count=sum(1 for r in results if r.signal == Signal.SHORT),
        none_count=sum(1 for r in results if r.signal == Signal.NONE),
        total_elapsed=total_elapsed,
    )
    if elapsed:
        summary.average_elapsed = sum(elapsed) / len(elapsed)
        summary.min_elapsed = min(elapsed)
        summary.max_elapsed = max(elapsed)
        summary.p50 = percentile(elapsed, 50)
        summary.p90 = percentile(elapsed, 90)
        summary.p95 = percentile(elapsed, 95)
    return summary
