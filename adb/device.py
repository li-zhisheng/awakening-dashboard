"""ADB 底层封装: 命令执行、设备管理。"""
import os
import re
import subprocess
import time
from typing import List

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AdbError(Exception):
    """单次 ADB 命令失败。"""


class DeviceLostError(AdbError):
    """设备级故障: 未连接 / 已断开 / 未授权。"""


def run_adb(args: List[str], timeout: float = 10, binary: bool = False):
    cmd = ["adb"] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        raise AdbError("adb 命令未找到, 请确认 platform-tools 已加入 PATH")
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 命令超时: {' '.join(args)}")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        msg = f"adb 命令失败: {' '.join(args)} -> {stderr}"
        lowered = (stderr + " " + msg).lower()
        if any(k in lowered for k in ("device not found", "device offline",
                                      "no devices", "unauthorized", "closed")):
            raise DeviceLostError(msg)
        raise AdbError(msg)
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def list_devices() -> List[str]:
    out = run_adb(["devices"], timeout=5)
    serials = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line.endswith("\tdevice"):
            serials.append(line.split("\t")[0])
    return serials


class Device:
    def __init__(self, serial: str = ""):
        serials = list_devices()
        if not serials:
            raise DeviceLostError(
                "未检测到 Android 设备, 请检查: 1)USB连接 2)已开启USB调试 "
                "3)已在手机上授权此电脑"
            )
        if serial:
            if serial not in serials:
                raise DeviceLostError(f"指定设备 {serial} 未连接")
            self.serial = serial
        else:
            self.serial = serials[0]

    def adb_args(self) -> List[str]:
        return ["-s", self.serial]

    def is_connected(self) -> bool:
        try:
            return self.serial in list_devices()
        except AdbError:
            return False

    def ensure_connected(self):
        if not self.is_connected():
            raise DeviceLostError(f"设备 {self.serial} 已断开")

    def wait_until_connected(self, timeout: float = 60, interval: float = 1.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_connected():
                return True
            time.sleep(interval)
        return False

    def shell(self, command: str, timeout: float = 10) -> str:
        return run_adb(self.adb_args() + ["shell", command], timeout=timeout)

    def screen_size(self) -> tuple:
        out = self.shell("wm size", timeout=5)
        matches = re.findall(r"(\d+)x(\d+)", out)
        if matches:
            w, h = matches[-1]  # Override size 优先于 Physical size
            return int(w), int(h)
        raise AdbError(f"无法解析屏幕分辨率: {out!r}")
