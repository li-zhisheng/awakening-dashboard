"""UI 控制: 点击、UI Hierarchy 获取、XML 工具函数。"""
import re
import time
from typing import Optional, Tuple

from .device import AdbError, Device


class UIController:
    def __init__(self, device: Device):
        self.device = device

    def tap(self, x: int, y: int):
        self.device.shell(f"input tap {int(x)} {int(y)}", timeout=10)

    def input_text(self, text: str):
        """输入文本 (仅ASCII安全, 股票代码为纯数字)。"""
        self.device.shell(f"input text {text}", timeout=10)

    def back(self):
        self.device.shell("input keyevent 4", timeout=10)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400):
        """滑动手势 (用于下拉刷新等)。"""
        self.device.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}",
            timeout=10)

    def dump_ui(self, retries: int = 2, timeout: float = 20) -> str:
        """获取当前界面 UI Hierarchy XML, 失败抛出 AdbError。"""
        last_err = ""
        for _ in range(retries + 1):
            try:
                out = self.device.shell(
                    "uiautomator dump /sdcard/aw_uidump.xml", timeout=timeout
                )
                if "dumped" in (out or "").lower():
                    xml = self.device.shell("cat /sdcard/aw_uidump.xml", timeout=timeout)
                    if xml and xml.lstrip().startswith("<"):
                        return xml
                last_err = f"dump 输出异常: {(out or '')[:120]!r}"
            except AdbError as e:
                last_err = str(e)
            time.sleep(0.6)
        raise AdbError(f"uiautomator dump 失败: {last_err}")


def parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    """解析 '[x1,y1][x2,y2]' 格式的 bounds。"""
    nums = re.findall(r"-?\d+", bounds or "")
    if len(nums) == 4:
        x1, y1, x2, y2 = map(int, nums)
        return x1, y1, x2, y2
    return None


def center_of(bounds: str) -> Optional[Tuple[int, int]]:
    b = parse_bounds(bounds)
    if not b:
        return None
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
