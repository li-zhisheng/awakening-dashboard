"""页面变化检测: 截图签名(标题栏+K线区域哈希)轮询, 不依赖固定 sleep。"""
import hashlib
import time

import cv2
import numpy as np

from adb.device import AdbError
from adb.screenshot import Screenshotter, crop_region
from config import AppConfig


def _region_hash(img: np.ndarray) -> str:
    if img is None or img.size == 0:
        return "empty"
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    small = cv2.resize(gray, (32, 32))
    return hashlib.md5(small.tobytes()).hexdigest()


class PageSignature:
    __slots__ = ("kline_hash",)

    def __init__(self, kline_hash: str):
        self.kline_hash = kline_hash

    def key(self) -> str:
        return self.kline_hash


class PageDetector:
    def __init__(self, shot: Screenshotter, cfg: AppConfig):
        self.shot = shot
        self.cfg = cfg

    def signature(self) -> PageSignature:
        """页面签名只用K线区域哈希。

        实测同花顺标题区(ViewFlipper)有跑马灯动画、右侧有机器人动画,
        均会导致签名永不稳定; K线图表区域静止后哈希稳定, 且不同股票必然不同。
        """
        img = self.shot.capture()
        return self.signature_from(img)

    def signature_from(self, img) -> PageSignature:
        kline = crop_region(img, self.cfg.regions.kline_region)
        return PageSignature(_region_hash(kline))

    def wait_switch_done(self, old: PageSignature, timeout: float) -> bool:
        """切换后轮询: 连续两次签名(均!=old且彼此相等)即认为页面已切换且稳定。

        相比"先等变化再等稳定"两阶段轮询, 少一次截图; 稳定判定依然基于
        K线区域哈希, 不依赖固定 sleep (post_tap_delay 仅用于跳过动画期)。
        """
        deadline = time.time() + timeout
        prev: PageSignature = None
        while time.time() < deadline:
            try:
                sig = self.signature()
            except AdbError:
                time.sleep(self.cfg.switch.poll_interval)
                continue
            if sig.key() != old.key():
                if prev is not None and sig.key() == prev.key():
                    return True
                prev = sig
            time.sleep(self.cfg.switch.poll_interval)
        return False
