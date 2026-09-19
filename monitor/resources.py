"""运行资源守卫(零第三方依赖): auto_round每轮轮头检查磁盘余量/日志体量/
进程内存, 只观测告警, 不改变任何交易行为。

告警策略(状态边沿触发, 防刷屏):
- 磁盘剩余<critical_gb(默认2GB, 与preflight阻断阈值一致): CRITICAL手机推送
  ——原子写持仓/截图会失败, 属交易安全事件;
- 磁盘<warn_gb(5GB) / logs目录超warn_mb / 进程峰值内存超rss_warn_mb: WARNING;
- 同级别持续异常只推一次, 全部恢复正常后推一条恢复提醒。
- 任何探测失败只跳过该项, 绝不抛异常影响主轮。

内存口径: POSIX用标准库resource(RUSAGE_SELF峰值RSS);
Windows用ctypes调psapi.GetProcessMemoryInfo, 均延迟导入(Mac离线可测其余项)。
"""
import logging
import os
import shutil
import sys

log = logging.getLogger("resources")

IS_WINDOWS = os.name == "nt"


def process_rss_mb():
    """当前进程(峰值)常驻内存MB; 探测失败返回None。"""
    try:
        if IS_WINDOWS:
            import ctypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.windll.psapi
            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return counters.PeakWorkingSetSize / 1024 / 1024
            return None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS单位字节, Linux单位KB
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        return peak_bytes / 1024 / 1024
    except Exception as e:
        log.debug("进程内存探测失败: %s", e)
        return None


def dir_size_mb(path: str):
    """目录总大小MB(普通文件累加, 子项异常跳过); 不存在返回0。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
    except OSError:
        return 0.0
    return total / 1024 / 1024


class ResourceGuard:
    OK, WARN, CRITICAL, DISABLED = "ok", "warn", "critical", "disabled"

    def __init__(self, cfg, notifier=None):
        self.cfg = cfg
        self.notifier = notifier
        self._level = self.OK   # 上次状态(边沿触发去重)

    def snapshot(self) -> dict:
        """采集磁盘余量(项目盘)、logs目录体量、进程峰值内存。"""
        m = getattr(self.cfg, "monitor", None)
        logs_dir = self.cfg.resolve(self.cfg.paths.logs_dir)
        check_dir = logs_dir if os.path.isdir(logs_dir) else (
            self.cfg.project_root or ".")
        disk_free_gb = disk_total_gb = None
        try:
            du = shutil.disk_usage(check_dir)
            disk_free_gb = du.free / 1024 ** 3
            disk_total_gb = du.total / 1024 ** 3
        except OSError as e:
            log.debug("磁盘探测失败: %s", e)
        rss = process_rss_mb()
        return {
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "disk_free_pct": (round(disk_free_gb / disk_total_gb * 100, 1)
                              if disk_free_gb is not None
                              and disk_total_gb else None),
            "logs_mb": round(dir_size_mb(logs_dir), 1),
            "rss_mb": round(rss, 1) if rss is not None else None,
        }

    def _classify(self, snap: dict):
        """返回 (level, [问题描述...], {指标:值})。"""
        m = getattr(self.cfg, "monitor", None)
        crit_gb = float(getattr(m, "disk_critical_gb", 2.0))
        warn_gb = float(getattr(m, "disk_warn_gb", 5.0))
        logs_warn_mb = float(getattr(m, "logs_size_warn_mb", 500.0))
        rss_warn_mb = float(getattr(m, "rss_warn_mb", 800.0))
        criticals, warns = [], []
        free = snap.get("disk_free_gb")
        if free is not None:
            if free < crit_gb:
                criticals.append(
                    f"磁盘仅剩{free:.1f}GB(<{crit_gb:g}GB), "
                    f"持仓原子写/截图随时可能失败")
            elif free < warn_gb:
                warns.append(f"磁盘剩余{free:.1f}GB(<{warn_gb:g}GB)")
        if snap.get("logs_mb", 0) > logs_warn_mb:
            warns.append(f"logs目录已{snap['logs_mb']:.0f}MB"
                         f"(阈值{logs_warn_mb:g}MB)")
        rss = snap.get("rss_mb")
        if rss is not None and rss > rss_warn_mb:
            warns.append(f"进程峰值内存{rss:.0f}MB(阈值{rss_warn_mb:g}MB,"
                         f"疑似泄漏)")
        level = (self.CRITICAL if criticals else
                 self.WARN if warns else self.OK)
        return level, criticals + warns

    def check(self) -> str:
        """每轮轮头调用。返回 ok/warn/critical/disabled。"""
        m = getattr(self.cfg, "monitor", None)
        if not getattr(m, "enable", True) or not getattr(
                m, "resource_check_enable", True):
            return self.DISABLED
        try:
            snap = self.snapshot()
            level, problems = self._classify(snap)
        except Exception as e:
            log.warning("资源检查异常: %s", e)
            return self.OK
        if level == self.OK:
            if self._level != self.OK:
                log.info("运行资源恢复正常(磁盘%sGB可用, logs%.0fMB)",
                         snap.get("disk_free_gb"), snap.get("logs_mb", 0))
                self._notify("WARNING", "交易告警·运行资源已恢复",
                             f"磁盘{snap.get('disk_free_gb'):.1f}GB可用, "
                             f"logs目录{snap.get('logs_mb', 0):.0f}MB, "
                             f"资源告警解除。")
            self._level = self.OK
            return level
        # 同级别持续异常只推一次(级别升级时追加推送)
        if level != self._level:
            rank = {self.WARN: 1, self.CRITICAL: 2}
            if self._level == self.OK or rank[level] > rank.get(
                    self._level, 0):
                detail = "; ".join(problems)
                hint = ("请立即清理logs/screenshots旧文件或更换磁盘, "
                        "清理前勿重启交易进程" if level == self.CRITICAL
                        else "可运行清理或检查日志/截图TTL配置")
                title = ("交易告警·磁盘空间紧急不足" if level == self.CRITICAL
                         else "交易告警·运行资源预警")
                self._notify(
                    "CRITICAL" if level == self.CRITICAL else "WARNING",
                    title, f"{detail}。当前快照: 磁盘"
                    f"{snap.get('disk_free_gb'):.1f}GB可用"
                    f"({snap.get('disk_free_pct')}%), "
                    f"logs {snap.get('logs_mb', 0):.0f}MB, "
                    f"内存峰值{snap.get('rss_mb')}MB。{hint}。")
                log.log(logging.CRITICAL if level == self.CRITICAL
                        else logging.WARNING, "运行资源%s: %s",
                        "严重不足" if level == self.CRITICAL else "预警",
                        detail)
        self._level = level
        return level

    def _notify(self, level: str, title: str, content: str):
        if not self.notifier:
            return
        try:
            self.notifier.send(title, content, level=level)
        except Exception as e:
            log.warning("资源告警推送失败: %s", e)
