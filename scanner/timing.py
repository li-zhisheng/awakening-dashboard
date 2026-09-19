"""耗时统计与结果落盘。"""
import csv
import os
import time
from typing import List

from models.result import StockResult, ScanSummary

CSV_FIELDS = [
    "stock_code", "start_time", "switch_complete_time", "capture_time",
    "detect_complete_time", "elapsed_time", "signal", "status",
    "error", "detection_source", "detection_state", "detection_confidence",
]


def _iso(t: float) -> str:
    if not t:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def write_results_csv(path: str, results: List[StockResult]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for r in results:
            w.writerow([
                r.stock_code,
                _iso(r.start_time),
                _iso(r.switch_complete_time),
                _iso(r.capture_time),
                _iso(r.detect_complete_time),
                r.elapsed_time,
                r.signal.label,
                r.status.value,
                r.error,
                r.detection.source,
                r.detection.state,
                round(r.detection.confidence, 3),
            ])


def format_summary(summary: ScanSummary) -> str:
    lines = [
        "================ 扫描统计 ================",
        f"总股票数: {summary.total_stocks} | 成功: {summary.success_count} "
        f"| UNKNOWN: {summary.unknown_count}",
        f"信号分布: 多 {summary.long_count} | 空 {summary.short_count} "
        f"| 无 {summary.none_count} | 未知 {summary.unknown_count}",
        f"总耗时: {summary.total_elapsed:.1f}s | 平均: {summary.average_elapsed:.2f}s "
        f"| 最快: {summary.min_elapsed:.2f}s | 最慢: {summary.max_elapsed:.2f}s",
        f"P50: {summary.p50:.2f}s | P90: {summary.p90:.2f}s | P95: {summary.p95:.2f}s",
        "==========================================",
    ]
    return "\n".join(lines)
