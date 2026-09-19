"""PC窗口工具: 进程/窗口定位、托盘窗口恢复、真实鼠标点击。

hotkey_trader 与 easytrader_client 共用。
关键经验(2026-09-08/09实测):
- xiadan"关闭"=隐藏到托盘, 进程无任何可见窗口时 pywinauto 的
  connect(process=)/top_window() 都会抛 "No windows for that process
  could be found"; 必须用 win32gui EnumWindows(可枚举隐藏窗口)找hwnd
  再 ShowWindow(SW_RESTORE)恢复。
- pywinauto鼠标事件在部分环境抛"No active desktop", 直接用
  win32api SetCursorPos+mouse_event发真实点击。
"""
import logging
import os
import subprocess
import time

log = logging.getLogger("pcwin")


def find_pid(exe_name: str):
    """按进程名找PID, 多个返回最小(最早启动), 不存在返回None。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    pids = []
    for line in (out or "").strip().splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == exe_name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return min(pids) if pids else None


def find_window_hwnd(pid: int, title: str):
    """枚举(含隐藏)窗口按PID+精确标题找hwnd, 找不到返回None。"""
    try:
        import win32gui
        import win32process

        found = []

        def _handler(hwnd, _):
            if win32gui.IsWindow(hwnd):
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                if wpid == pid and win32gui.GetWindowText(hwnd) == title:
                    found.append(hwnd)

        win32gui.EnumWindows(_handler, None)
        return found[0] if found else None
    except Exception as e:
        log.warning("枚举窗口失败(pid=%s title=%r): %s", pid, title, e)
        return None


def restore_window(hwnd) -> bool:
    """把(托盘)隐藏窗口恢复到前台可见。已在前台则直接返回True。"""
    try:
        import win32con
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False
        if not win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(1.0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass                       # 前台锁定时失败无碍, 窗口已可见
        return True
    except Exception as e:
        log.warning("恢复窗口失败: %s", e)
        return False


def real_click(x: int, y: int, settle: float = 0.6):
    """真实鼠标单击(移动+按下+抬起), 绕过pywinauto桌面检查。"""
    import win32api
    import win32con
    SetCursorPos = win32api.SetCursorPos
    SetCursorPos((int(x), int(y)))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if settle:
        time.sleep(settle)


def dpi_scale() -> float:
    """物理像素/逻辑像素比(本进程不感知DPI时>1, 如125%缩放=1.25)。

    ImageGrab截屏用物理像素, win32窗口坐标是逻辑像素, 换算靠它。
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        dc = user32.GetDC(0)
        try:
            phys = gdi32.GetDeviceCaps(dc, 118)    # DESKTOPHORZRES
            logical = gdi32.GetDeviceCaps(dc, 8)   # HORZRES
            return (phys / logical) if logical else 1.0
        finally:
            user32.ReleaseDC(0, dc)
    except Exception:
        return 1.0


def panel_color_count(rect) -> tuple:
    """统计窗口顶部快捷键面板行的红/绿按钮像素数。

    rect为win32逻辑坐标(l, t, r, b), 接受pywinauto RECT对象或4元组;
    内部换算为物理像素截屏。
    面板可见时红+绿约1.9万像素(2026-09-08实测), 不可用时接近0。
    """
    import numpy as np
    s = dpi_scale()
    if hasattr(rect, "left"):            # pywinauto RECT不可迭代, 只能属性访问
        l, t, r_, b_ = rect.left, rect.top, rect.right, rect.bottom
    else:
        l, t, r_, b_ = rect
    x0, y0 = (l + (r_ - l) * 0.12) * s, (t + 2) * s
    x1, y1 = (l + (r_ - l) * 0.60) * s, (t + 36) * s
    img = grab_screen(x0, y0, x1, y1)
    if img is None:
        return 0, 0
    arr = np.asarray(img)
    rr = arr[:, :, 0].astype(int)
    gg = arr[:, :, 1].astype(int)
    bb = arr[:, :, 2].astype(int)
    red = int(((rr > 150) & (rr - gg > 60) & (rr - bb > 40)).sum())
    green = int(((gg > 130) & (gg - rr > 50) & (gg - bb > 50)).sum())
    return red, green


def grab_screen(x0: int, y0: int, x1: int, y1: int):
    """截屏指定区域, 返回PIL Image; 失败返回None。"""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(int(x0), int(y0), int(x1), int(y1)))
    except Exception as e:
        log.warning("截屏失败(%s,%s,%s,%s): %s", x0, y0, x1, y1, e)
        return None


# F6持仓浮层采样区(窗口相对比例): 浮层固定出现在窗口顶部中央
# 2026-09-08实测: 浮层物理位置约为窗口宽0.55-0.72 / 高0.08-0.31,
# 含边距取(0.52, 0.06, 0.75, 0.33); DPI感知与否均自洽(比例定位)
_F6_REGION = (0.52, 0.06, 0.75, 0.33)   # (x0_ratio, y0_ratio, x1_ratio, y1_ratio)


def f6_no_position(rect, min_score: float = 0.70) -> tuple:
    """检测F6持仓浮层是否显示"当前股票无持仓"。

    rect为窗口逻辑坐标(pywinauto RECT或4元组); 模板为黑字白底
    "当前股票无持仓"(物理像素, 2026-09-08实测裁剪)。
    返回 (无持仓bool, 匹配得分float, 检测链路ok bool)。
    截屏失败/模板缺失/区域过小 => (False, 0.0, False)——调用方绝不能把
    链路异常解读成"有持仓"或"无持仓"(2026-09-10审计修复: 原2元组把
    截屏异常与"有持仓"混同, 会导致BUY误判成交/预检误拒)。
    正常检测但未匹配到无持仓文案(即有持仓/浮层未显该文案)
    => (False, score, True); 匹配 => (True, score, True)。
    """
    import cv2
    import numpy as np
    if hasattr(rect, "left"):
        l, t, r_, b_ = rect.left, rect.top, rect.right, rect.bottom
    else:
        l, t, r_, b_ = rect
    s = dpi_scale()
    rx0, ry0, rx1, ry1 = _F6_REGION
    x0 = (l + (r_ - l) * rx0) * s
    y0 = (t + (b_ - t) * ry0) * s
    x1 = (l + (r_ - l) * rx1) * s
    y1 = (t + (b_ - t) * ry1) * s
    img = grab_screen(x0, y0, x1, y1)
    if img is None:
        return False, 0.0, False
    tpl_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))),
        "detector", "templates", "pc_f6", "no_position.png")
    tpl = cv2.imread(tpl_path)
    if tpl is None:
        log.warning("F6模板缺失: %s", tpl_path)
        return False, 0.0, False
    region = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    tpl_g = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    if region.shape[0] < tpl_g.shape[0] or region.shape[1] < tpl_g.shape[1]:
        return False, 0.0, False
    res = cv2.matchTemplate(region, tpl_g, cv2.TM_CCOEFF_NORMED)
    score = float(res.max())
    return score >= min_score, score, True
