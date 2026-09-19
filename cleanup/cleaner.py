"""垃圾清理: TTL策略定期清理截图与日志 (无数据库)。

设计决策: 不引入数据库。
- 截图是调试产物, 过期即删; 轮次CSV/JSON极小(每轮约4KB), 保留90天无压力。
- 状态文件(positions.json/trade_state.json/hotlist_local.json/alerts.jsonl/
  ths_cookie.txt/kill_switch.flag)永不清理。

规则(白名单glob, 相对项目根):
- screenshots/*.png      -> screenshot_ttl_hours (默认48h)
- logs/scan_*.log        -> log_ttl_hours (默认14天)
- logs/diag_*.*          -> debug_ttl_hours (默认7天)
- logs/ui_*.xml          -> debug_ttl_hours
- logs/*.png             -> debug_ttl_hours (杂项调试截图, 无状态文件为png)
- logs/results_*.csv     -> archive_ttl_hours (轮次档案, 默认90天)
- logs/summary_*.json    -> archive_ttl_hours
- logs/auto_round_*.csv  -> archive_ttl_hours
- logs/events_*.jsonl    -> event_ttl_hours (审计事件: 信号/交易/覆盖率/异常,
                           策略复盘与追责核心数据, 单独保留1年而非90天)
- logs/lunch_report_*.txt -> archive_ttl_hours (盘中日报存档)

用法:
    cleaner = Cleaner(cfg)           # cfg: AppConfig
    stats = cleaner.run()            # 立即清理, 返回统计
    stats = cleaner.run(dry_run=True)  # 只统计不删除
    Cleaner.daily_guard(cfg)         # auto_round启动时调用, 每日最多执行一次
"""
import logging
import os
import time

from config import AppConfig

log = logging.getLogger("cleanup")

# (glob模式, TTL配置字段名) — 白名单, 杜绝误删
RULES = [
    ("screenshots/*.png", "screenshot_ttl_hours"),
    ("logs/scan_*.log", "log_ttl_hours"),
    ("logs/scan_*.log.*", "log_ttl_hours"),   # RotatingFileHandler轮转份(.1/.2/.3)
    ("logs/pipeline_*.log", "log_ttl_hours"),
    ("logs/pipeline_*.log.*", "log_ttl_hours"),
    ("logs/diag_*.*", "debug_ttl_hours"),
    ("logs/ui_*.xml", "debug_ttl_hours"),
    ("logs/*.png", "debug_ttl_hours"),
    ("logs/results_*.csv", "archive_ttl_hours"),
    ("logs/summary_*.json", "archive_ttl_hours"),
    ("logs/auto_round_*.csv", "archive_ttl_hours"),
    ("logs/events_*.jsonl", "event_ttl_hours"),       # 审计事件保留1年
    ("logs/lunch_report_*.txt", "archive_ttl_hours"),  # 盘中日报存档
]

GUARD_FILE = "logs/last_cleanup.date"


class Cleaner:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.root = cfg.project_root

    def _iter_files(self, pattern: str):
        """按glob列出文件 (仅项目根内, 单层目录)。"""
        import glob
        full = os.path.join(self.root, pattern)
        for path in glob.glob(full):
            if os.path.isfile(path):
                yield path

    def run(self, dry_run: bool = False) -> dict:
        """执行清理, 返回 {规则: (删除数, 释放字节)}。

        同一文件命中多条规则时只按首条规则处理一次(去重)。
        """
        stats: dict = {}
        seen: set = set()
        now = time.time()
        for pattern, ttl_field in RULES:
            ttl_hours = getattr(self.cfg.cleanup, ttl_field)
            cutoff = now - ttl_hours * 3600
            deleted, freed = 0, 0
            for path in self._iter_files(pattern):
                if path in seen:
                    continue
                try:
                    if os.path.getmtime(path) <= cutoff:
                        size = os.path.getsize(path)
                        if not dry_run:
                            os.remove(path)
                        seen.add(path)
                        deleted += 1
                        freed += size
                except OSError as e:
                    log.warning("清理失败 %s: %s", path, e)
            stats[pattern] = (deleted, freed)
        return stats

    @staticmethod
    def format_stats(stats: dict) -> str:
        parts = []
        total_n = total_b = 0
        for pattern, (n, b) in stats.items():
            total_n += n
            total_b += b
            if n:
                parts.append(f"{pattern}x{n}({b/1e6:.1f}MB)")
        head = f"共删{total_n}个文件 释放{total_b/1e6:.1f}MB"
        return head + (" [" + ", ".join(parts) + "]" if parts else "")


def daily_guard(cfg: AppConfig, logger=None) -> str:
    """每日一次的自动清理守卫: 当天已执行过则跳过。返回摘要字符串。"""
    if not cfg.cleanup.enable:
        return "cleanup禁用, 跳过"
    guard = os.path.join(cfg.project_root, GUARD_FILE)
    today = time.strftime("%Y-%m-%d")
    try:
        with open(guard, "r", encoding="utf-8") as f:
            if f.read().strip() == today:
                return "今日已清理, 跳过"
    except OSError:
        pass
    stats = Cleaner(cfg).run()
    summary = Cleaner.format_stats(stats)
    os.makedirs(os.path.dirname(guard), exist_ok=True)
    with open(guard, "w", encoding="utf-8") as f:
        f.write(today)
    if logger:
        logger.info("每日垃圾清理: %s", summary)
    return summary
