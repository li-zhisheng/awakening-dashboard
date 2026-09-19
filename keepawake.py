"""PC端环境守卫: 防休眠/防熄屏 + 屏保进程检测。

- SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)
  阻止Windows休眠与熄屏(同时抑制系统屏保触发), 长时间扫描/交易会话用。
- 第三方屏保进程(如ScreenSaverPlayer.exe)会封锁pywinauto模拟输入
  ("No active desktop"异常), 交易前检测到即明确报错提示人工退出。

跨平台: macOS 上 start/stop/check_screensaver 优雅跳过(不调用 Windows API)。
"""
import logging
import sys

log = logging.getLogger("keepawake")

IS_WINDOWS = sys.platform == "win32"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_started = False


def start():
    """开启防休眠/防熄屏(调用线程有效, 程序生命周期内保持)。"""
    global _started
    if not IS_WINDOWS:
        log.info("非 Windows 平台, 跳过防休眠")
        return
    import ctypes
    ok = ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    if ok:
        _started = True
        log.info("防休眠/防熄屏已开启")
    else:
        log.warning("SetThreadExecutionState调用失败, 无法阻止休眠")


def stop():
    """恢复系统默认电源行为。"""
    if not IS_WINDOWS:
        return
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    log.info("防休眠已恢复系统默认")


def check_screensaver(blockers=("ScreenSaverPlayer.exe",)) -> list:
    """检测封锁模拟输入的屏保进程, 返回命中的进程名列表(空=干净)。"""
    if not IS_WINDOWS:
        return []
    import subprocess
    found = []
    for name in blockers:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}",
                 "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            if name.lower() in (out or "").lower():
                found.append(name)
        except Exception:
            continue
    return found
