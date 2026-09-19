"""截图: exec-out 二进制截图为主, 文件中转为兜底; 区域裁剪工具。"""
import os
import tempfile
import time

import cv2
import numpy as np

from .device import AdbError, Device, run_adb


def crop_region(img: np.ndarray, region, pad: int = 0):
    """按 [x1,y1,x2,y2] 裁剪, pad 向外扩边, 自动钳制到图像边界。"""
    if img is None or img.size == 0:
        return None
    h, w = img.shape[:2]
    x1, y1, x2, y2 = region
    x1 = max(0, int(x1) - pad)
    y1 = max(0, int(y1) - pad)
    x2 = min(w, int(x2) + pad)
    y2 = min(h, int(y2) + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return img[y1:y2, x1:x2].copy()


def region_contains(region, x: int, y: int) -> bool:
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def decode_png(data: bytes):
    if not data:
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return img


class Screenshotter:
    def __init__(self, device: Device, max_retries: int = 3):
        self.device = device
        self.max_retries = max(1, max_retries)

    def capture(self) -> np.ndarray:
        """返回 BGR 图像, 失败抛出 AdbError。"""
        last_err = ""
        for _ in range(self.max_retries):
            try:
                data = run_adb(
                    self.device.adb_args() + ["exec-out", "screencap", "-p"],
                    binary=True,
                    timeout=20,
                )
                img = decode_png(data)
                if img is not None:
                    return img
                last_err = "exec-out PNG 解码失败, 尝试文件方式"
                img = self._capture_via_file()
                if img is not None:
                    return img
            except AdbError as e:
                last_err = str(e)
            time.sleep(0.4)
        raise AdbError(f"截图失败(重试{self.max_retries}次): {last_err}")

    def _capture_via_file(self):
        self.device.shell("screencap -p /sdcard/aw_shot.png", timeout=15)
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            run_adb(self.device.adb_args() + ["pull", "/sdcard/aw_shot.png", tmp],
                    timeout=20)
            return cv2.imread(tmp, cv2.IMREAD_COLOR)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
