"""跨进程单实例锁: 防止同一台机器误开两个自动交易进程。

背景: 2026-09-10曾因多开auto_round, 两个进程对同一手机重复发键下单、
对positions.json读-改-写互相覆盖持仓。加锁后第二个实例启动即拒绝。

实现(零第三方依赖, 进程退出/崩溃由OS自动释放锁):
- Windows: 命名互斥量 CreateMutexW (Local\\, 同会话有效, 单用户交易机足够)
- macOS/Linux: fcntl.flock 排他锁(logs/app.lock, 文件只作锁载体与PID诊断)

只锁真实发键/真实写持仓的入口(auto_round / pipeline_round);
web监控台/scan扫描/watchdog/人工对账不获取此锁, 仍可与auto_round同跑。

跨平台约定见 .trae/documents/CROSS_PLATFORM_DEV.md: 平台专有API延迟导入。
"""
import logging
import os
import sys
import time

log = logging.getLogger("instance_lock")

IS_WINDOWS = sys.platform == "win32"
ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """用法:
        with SingleInstance(lock_file="logs/app.lock") as got:
            if not got: sys.exit(1)
            ...
    或显式 acquire()/release(); 锁随进程死亡由OS自动释放。
    """

    def __init__(self, lock_file: str = "logs/app.lock",
                 name: str = "AwakeningTrader"):
        self.lock_file = lock_file
        self.name = name
        self._fh = None
        self._mutex = None

    def acquire(self) -> bool:
        try:
            if IS_WINDOWS:
                return self._acquire_win()
            return self._acquire_posix()
        except OSError as e:
            log.error("单实例锁获取异常: %s", e)
            return False

    def release(self):
        try:
            if IS_WINDOWS:
                self._release_win()
            else:
                self._release_posix()
        except OSError:
            pass

    # ---------- Windows: 命名互斥量 ----------

    def _acquire_win(self) -> bool:
        import ctypes  # 延迟导入; import ctypes在Mac可行但windll不存在
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
        if not handle:
            log.error("CreateMutexW失败, last_error=%s",
                      ctypes.get_last_error())
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._mutex = (kernel32, handle)
        return True

    def _release_win(self):
        if not self._mutex:
            return
        kernel32, handle = self._mutex
        kernel32.CloseHandle(handle)
        self._mutex = None

    # ---------- POSIX: flock 文件锁 ----------

    def _acquire_posix(self) -> bool:
        import fcntl
        d = os.path.dirname(os.path.abspath(self.lock_file))
        os.makedirs(d, exist_ok=True)
        fh = open(self.lock_file, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
        self._fh = fh
        return True

    def _release_posix(self):
        import fcntl
        if not self._fh:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def acquire_or_exit(lock_file: str, name: str = "AwakeningTrader",
                    kind: str = "自动交易"):
    """入口守卫: 获取不到单实例锁则打印明确提示并退出进程。

    返回持有的SingleInstance(调用方须保活到进程结束, 别让它被GC)。
    """
    lock = SingleInstance(lock_file, name=name)
    if lock.acquire():
        log.info("单实例锁已获取(%s)", kind)
        return lock
    print("\n" + "=" * 60)
    print("⚠️  检测到另一个 Awakening 交易进程正在运行, 本实例拒绝启动。")
    if not IS_WINDOWS:
        print(f"   锁文件: {os.path.abspath(lock_file)} (内含持有方PID)")
        print("   若确认无进程在跑(如上次异常退出残留显示), 锁会由OS自动释放;")
        print("   也可删除该锁文件后重试。")
    else:
        print("   请检查任务栏/任务管理器中已有的交易进程, 双开会导致")
        print("   重复发键下单与positions.json持仓覆盖。")
    print("=" * 60)
    sys.exit(1)
