"""HotkeyTrader: 行情端快捷键闪电下单(主通道) + xiadan表单兜底。

流程(execution.channel=hotkey):
1. 风控预检(kill_switch/每股每日限买/交易时段/日下单数)
2. 每日bootstrap: F12登录交易通道+关窗(当日仅一次, 热键前置条件)
3. 快捷键面板检测(顶部红/绿按钮条, 颜色像素判定): 不显示时鼠标点击
   界面唤出(用户实测: 面板消失则F1-F4无效, 点一下即恢复)
4. 同花顺行情端(hexin.exe)键盘精灵定位目标股票 -> 发送仓位快捷键
   (F1买25%@最新价/F2买25%@卖一价/F3清仓@最新价/F4清仓@买一价)
5. 下单后F6持仓验证(默认f6_verify_enable=true): 行情端浮层查当前个股
   持仓, 不弹xiadan无验证码。BUY见持仓/SELL变"无持仓"=成交确认;
   状态未变化=uncertain(未成交或按键未生效)ALERT人工确认。
   旧xiadan当日委托回查(verify_enable)默认关, 弹窗且触发"拷贝数据"
   验证码, 仅手动开启, 优先级低于F6。
6. 面板无法唤出时 -> 自动降级xiadan表单下单兜底(可能需人工输验证码)

xiadan界面定位: 仅兜底下单与(可选)回查使用, 平时绝不弹出。
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from config import AppConfig
from keepawake import check_screensaver
from models.audit import get_event_log
from trader.risk_control import RiskController

log = logging.getLogger("hotkey_trader")

# 行情端主窗标题中的6位个股代码(沪深主板60/00为主, 兼容30/68等):
# F8撤单前据此核对待撤股票, 标题格式不符则提取失败(fail-open照常撤)
_TITLE_CODE_RE = re.compile(r"(?<!\d)((?:60|00|30|68|83|87|92|43)\d{4})(?!\d)")


class HotkeyTrader:
    """行情端快捷键下单(替代EasytraderClient表单下单), 接口保持一致。"""

    def __init__(self, cfg: AppConfig, risk: RiskController, positions=None):
        self.cfg = cfg
        self.risk = risk
        self.positions = positions   # 主进程PositionStore实例(后台线程复用, 不新建)
        self.hk = cfg.hotkey
        self._hexin_app = None
        self._hexin_win = None
        self._td_user = None       # xiadan easytrader实例(回查用)
        self._td_connected = False
        self._form_client = None   # 快捷键面板失效时的xiadan表单兜底通道
        self.notifier = None       # notify.Notifier, 后台撤单/成交结果推送
        # hexin窗口操作互斥: 主轮execute_order与挂单后台监控线程/人工持仓
        # 对账(check_holding)都会set_focus+键盘精灵+发键, 交叉操作会导致
        # 热键发错股票或F5误撤, 必须串行化(2026-09-10审计修复)
        self._win_lock = threading.RLock()
        # 主轮交易意图: execute_order置位, 后台挂单监控获取_win_lock前先
        # 等该事件清零, 避免后台5-30s的goto+F6阻塞主轮信号交易(主轮优先)
        self._trading_intent = threading.Event()
        # 封单监控: 封单秒级变动>=10%立即告警; 有pending挂单必F8撤单
        from monitor.seal_watcher import SealWatcher
        self.seal_watcher = SealWatcher(
            notifier=None,
            cancel_fn=lambda code, side: self._watcher_cancel(code, side),
            pending_fn=lambda code: self._pending_side(code))
        # F6检测链路健康: 连续异常计数(非"未成交", 是截屏/模板链路不可用),
        # 达阈值自动降级xiadan只读对账(只读不写); 冷却节流防反复弹交易端
        self._f6_err_streak = 0
        self._f6_last_fallback_ts = 0.0
        self._f6_health_lock = threading.Lock()
        # F6检测质量(区别于连续异常降级): 近20次滑窗异常率>=30%每日告警一次,
        # 捕捉"时好时坏"的视觉链路劣化(连续2次异常才触发只读对账, 漏掉抖动)
        self._f6_quality = deque(maxlen=20)
        self._f6_quality_alerted = ""
        # 尾盘顶格热键未配置告警按方向每日去重(结算单可能一批多只, 不刷屏)
        self._closing_key_warned = set()
        # F8撤单前标题无法核对(fail-open)的滑窗计数: 窗口内达阈值升级CRITICAL
        # (2026-09-18采纳); 计数降回阈值下自动重新武装
        self._cancel_failopen_ts = deque()
        self._cancel_failopen_alerted = False

    def _pending_side(self, code: str) -> str:
        """封单监控回调: 台账中该股当前挂单方向(BUY/SELL), 无挂单返回''。"""
        for act in ("SELL", "BUY"):
            t = self.risk.get_attempt(code, act)
            if t and t.get("status") == "pending":
                return act
        return ""

    def _watcher_cancel(self, code: str, side: str = "SELL") -> bool:
        """封单监控线程回调: 持窗口锁F8撤该股挂单(买/卖)。

        撤单成功台账对应方向转canceled: 15:00尾盘成交复查据此跳过该股
        (已转人工处置, 不再误报"尾盘未成交")。
        """
        try:
            with self._win_lock:
                ok = self._cancel_single_by_hotkey(code, side)
            if ok:
                self.risk.set_attempt_status(code, side, "canceled")
            return ok
        except Exception as e:
            log.error("封单监控撤单异常 %s: %s", code, e)
            return False

    # ---------- 连接 ----------

    @staticmethod
    def _find_pid(image_name: str):
        """按进程名查PID (tasklist), 返回int或None。"""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}",
                 "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split('","')
                if len(parts) >= 2 and image_name.lower() in parts[0].lower():
                    return int(parts[1].strip('"'))
        except Exception as e:
            log.warning("查询进程%s失败: %s", image_name, e)
        return None

    def _connect_hexin(self):
        """连接同花顺行情端(hexin.exe)主窗口。"""
        if self._hexin_win is not None:
            return
        import pywinauto
        hk = self.hk
        if hk.hexin_exe:
            app = pywinauto.Application().connect(path=hk.hexin_exe, timeout=10)
        else:
            pid = self._find_pid("hexin.exe")
            if not pid:
                raise RuntimeError("未找到hexin.exe进程(同花顺行情端未启动)")
            app = pywinauto.Application().connect(process=pid, timeout=10)
        self._hexin_app = app
        # 主窗口: 优先标题含"同花顺"的可见顶层, 否则top_window
        win = None
        for w in app.windows(visible_only=True):
            try:
                if "同花顺" in (w.window_text() or ""):
                    win = w
                    break
            except Exception:
                pass
        if win is None:
            win = app.top_window()
        self._hexin_win = win
        log.info("行情端已连接: '%s'", win.window_text())

    def _connect_xiadan(self):
        """连接xiadan(回查委托/持仓/资金用), lazy。"""
        if self._td_connected:
            return
        # xiadan"关闭"=隐藏到托盘时, easytrader的connect->top_window()会抛
        # "No windows found"; 进程已在运行则先恢复主窗可见再让easytrader接管
        pid = self._find_pid("xiadan.exe")
        if pid and not self._restore_xiadan_window():
            log.warning("xiadan进程%d在运行但主窗恢复失败, 尝试直接连接", pid)
        import easytrader
        import easytrader.grid_strategies as gs
        user = easytrader.use(self.cfg.easytrader.client_type)
        user.grid_strategy = gs.Copy       # Xls策略在本机静默失败
        user.enable_type_keys_for_editor()
        user.connect(self.cfg.easytrader.exe_path,
                     timeout=self.cfg.easytrader.connect_timeout)
        # top_window()会解析到隐藏IE辅助窗口(Internet Explorer_Hidden),
        # 导致左侧菜单/网格全部ElementNotFound -> 显式定位交易主窗口
        main_win = user.app.window(title="网上股票交易系统5.0")
        user._main = main_win
        user.top_window = lambda: main_win
        self._td_user = user
        self._td_connected = True
        log.info("交易端(xiadan)已连接: 用于委托回查")

    # ---------- 快捷键面板(顶部按钮条) ----------

    def _panel_visible(self) -> bool:
        """检测快捷键面板(hexin顶部红/绿按钮条)是否显示。

        面板是主窗自绘无独立窗口, 前台截屏统计红绿按钮像素判定;
        面板不显示时F1-F4热键无效(用户实测, 点一下界面即恢复)。
        """
        from trader.pcwin import panel_color_count
        try:
            self._hexin_win.set_focus()
            time.sleep(0.3)
            red, green = panel_color_count(self._hexin_win.rectangle())
            ok = red + green >= self._PANEL_MIN_PIXELS
            if not ok:
                log.warning("快捷键面板未显示(红=%d 绿=%d)", red, green)
            return ok
        except Exception as e:
            log.warning("面板检测异常: %s", e)
            return False

    def _ensure_hotkey_panel(self) -> bool:
        """确保快捷键面板显示: 不可见时点击界面唤出(最多2个位置)。"""
        if self._panel_visible():
            return True
        from trader.pcwin import real_click
        try:
            r = self._hexin_win.rectangle()
            spots = [
                (r.right - 190, min(r.top + 500, r.bottom - 120)),  # 右侧图区
                (r.left + (r.right - r.left) // 2,                  # 客户区中央
                 r.top + (r.bottom - r.top) // 2),
            ]
            for x, y in spots:
                log.info("点击(%d,%d)尝试唤出快捷键面板...", x, y)
                real_click(x, y)
                if self._panel_visible():
                    log.info("快捷键面板已唤出")
                    return True
        except Exception as e:
            log.warning("面板唤出点击异常: %s", e)
        return False

    def _execute_form_fallback(self, code: str, action: str,
                               price: float, qty: int,
                               queue_only: bool = False,
                               closing_auction: bool = False) -> dict:
        """快捷键面板不可用时的兜底: xiadan表单下单。

        表单通道可能弹"拷贝验证码"(需人工输入, 约30s自动取消);
        BUY时qty=0按default_qty(100股)保守下单(快捷键的25%仓位语义
        在表单通道无法复现, 保守优先)。
        """
        try:
            from trader.easytrader_client import EasytraderClient
            if self._form_client is None:
                self._form_client = EasytraderClient(self.cfg, self.risk)
            log.warning("%s %s 走form表单兜底通道(若有验证码弹窗需人工输入)",
                        action, code)
            rep = self._form_client.execute_order(code, action, price, qty,
                                                  queue_only=queue_only,
                                                  closing_auction=closing_auction)
            rep["mode"] = "form_fallback"
            return rep
        except Exception as e:
            log.error("form兜底通道异常: %s", e)
            return {"ok": False, "error": f"form兜底通道异常: {e}",
                    "mode": "form_fallback"}

    # ---------- 每日F12登录 bootstrap ----------

    _XIADAN_TITLE = "网上股票交易系统5.0"
    _PANEL_MIN_PIXELS = 2000     # 面板可见时红绿按钮约1.9万像素, 阈值取1/10

    def _restore_xiadan_window(self) -> bool:
        """把隐藏在托盘的xiadan主窗恢复到前台可见。"""
        from trader.pcwin import find_window_hwnd, restore_window
        pid = self._find_pid("xiadan.exe")
        if not pid:
            return False
        hwnd = find_window_hwnd(pid, self._XIADAN_TITLE)
        if hwnd is None:
            return False
        if restore_window(hwnd):
            return True
        log.warning("恢复xiadan主窗失败")
        return False

    def _xiadan_main_window(self, visible_only: bool = False):
        """定位xiadan主窗口(pywinauto对象), 不存在返回None。

        visible_only=True时仅返回可见窗口(实测: xiadan点关闭是隐藏到托盘,
        进程与窗口都保留, 只有is_visible能反映"窗口已关")。
        """
        import win32gui
        from trader.pcwin import find_window_hwnd
        pid = self._find_pid("xiadan.exe")
        if not pid:
            return None
        hwnd = find_window_hwnd(pid, self._XIADAN_TITLE)
        if hwnd is None:
            return None
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return None
        try:
            import pywinauto
            app = pywinauto.Application().connect(handle=hwnd, timeout=5)
            return app.window(handle=hwnd)
        except Exception:
            return None

    def _close_xiadan(self):
        """关闭xiadan主窗口并重置easytrader连接状态(恢复热键就绪)。

        用户实测: 委托交易页面开着时F1-F4热键不生效, 必须关窗。
        实测xiadan"关闭"是隐藏到托盘(进程保留), 判定标准=主窗不可见。
        下次委托回查时_connect_xiadan会set_focus恢复窗口。
        """
        self._td_user = None
        self._td_connected = False
        win = self._xiadan_main_window(visible_only=True)
        if win is None:
            return
        try:
            win.close()
            time.sleep(2)
        except Exception as e:
            log.warning("关闭xiadan窗口异常: %s", e)
        if self._xiadan_main_window(visible_only=True) is None:
            log.info("xiadan窗口已关闭(热键就绪)")
        else:
            log.warning("xiadan主窗关闭后仍可见, 请人工确认")

    def daily_bootstrap(self, force: bool = False) -> dict:
        """每日首次: F12登录交易通道 -> 关闭委托窗口, 保证买卖热键可用。

        用户实测: 每天首次进入同花顺必须先按F12登录账户并关闭委托交易
        页面, 否则F1-F4热键不生效。状态文件按日期记录, 当日只做一次。
        """
        marker = os.path.join(self.cfg.project_root, "logs",
                              "hotkey_bootstrap.json")
        today = time.strftime("%Y-%m-%d")
        if not force:
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    if json.load(f).get("date") == today:
                        return {"ok": True, "skipped": True,
                                "detail": "今日已bootstrap"}
            except (OSError, ValueError):
                pass

        try:
            self._connect_hexin()
        except Exception as e:
            return {"ok": False, "error": f"行情端连接失败: {e}"}

        # 昨日残留的xiadan窗口先关掉(会话失效且抢占F键)
        self._close_xiadan()

        win = self._hexin_win
        try:
            win.set_focus()
            time.sleep(0.4)
            win.type_keys("{ESC}")       # 清掉可能的键盘精灵/弹层
            time.sleep(0.3)
            log.info("每日bootstrap: 发送F12登录交易通道...")
            win.type_keys("{F12}")
        except Exception as e:
            return {"ok": False, "error": f"F12发送失败: {e}"}

        # 等xiadan主窗出现(客户端自动登录)
        deadline = time.time() + 25
        xd = None
        while time.time() < deadline:
            xd = self._xiadan_main_window()
            if xd is not None:
                break
            time.sleep(1.0)
        if xd is None:
            return {"ok": False,
                    "error": "F12后25s内委托窗口未出现, 请人工确认登录状态"}
        log.info("委托窗口已出现, 等待自动登录完成...")
        time.sleep(8)                    # 实测自动登录约5-8s

        self._close_xiadan()             # 关窗恢复热键就绪

        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"date": today,
                           "time": time.strftime("%H:%M:%S")}, f)
        except OSError as e:
            log.warning("bootstrap状态写入失败: %s", e)
        log.info("每日bootstrap完成: 交易通道已登录, 热键就绪")
        return {"ok": True, "skipped": False, "detail": "F12登录+关窗完成"}

    # ---------- 行情端动作 ----------

    def _goto_stock(self, code: str):
        """键盘精灵定位股票: ESC清理 -> 敲6位代码 -> 回车 -> 等行情页加载。"""
        win = self._hexin_win
        win.set_focus()
        time.sleep(0.3)
        win.type_keys("{ESC}")            # 关掉可能残留的键盘精灵/弹窗
        time.sleep(0.3)
        win.type_keys(code)               # 行情端敲数字自动弹键盘精灵
        time.sleep(0.7)                   # 等联想列表定位
        win.type_keys("{ENTER}")          # 回车进入个股分时页
        time.sleep(self.hk.goto_settle_seconds)

    def _pick_key(self, action: str, qty: int = 0,
                  closing_auction: bool = False) -> str:
        """按信号方向与变体选键。
        BUY:  latest=F1(最新价25%) / ask1=F2(卖一价25%, 极端)
        SELL: latest=F3(最新价清仓) / bid1=F4(买一价核卖)
        变体由config.hotkey.buy_variant/sell_variant控制, 默认latest。

        closing_auction(2026-09-16用户裁定): 14:57-15:00尾盘集合竞价结算单
        改发用户自定义的"涨停价买/跌停价卖"键顶格报价——集合竞价按收盘价
        统一撮合, 顶格只决定撮合优先级而非实际成交价, 数量仍由客户端控制;
        自定义键未配置时回退常规键并WARNING一次/方向/进程(不阻断结算)。
        """
        hk = self.hk
        buy = action.upper() == "BUY"
        if closing_auction and hk.closing_limit_key_enable:
            limit_key = (hk.closing_buy_limit_key if buy
                         else hk.closing_sell_limit_key)
            if limit_key:
                log.info("尾盘集合竞价顶格报价 %s -> 键%s(按收盘价撮合)",
                         action, limit_key)
                return limit_key
            side = "BUY" if buy else "SELL"
            if side not in self._closing_key_warned:
                self._closing_key_warned.add(side)
                log.warning("尾盘竞价顶格热键未配置(closing_%s_limit_key为空),"
                            " 回退常规最新价键, 顶格报价不生效; 请在同花顺"
                            "面板绑定涨停价买/跌停价卖键后填入config",
                            side.lower())
        if buy:
            return hk.buy_ask1_key if hk.buy_variant == "ask1" \
                else hk.buy_latest_key
        return hk.sell_bid1_key if hk.sell_variant == "bid1" \
            else hk.sell_latest_key

    # ---------- 键位白名单(2026-09-18采纳) ----------

    # config中显式配置的键位字段(这些键无论是否在F1-F12内置集合都放行)
    _CONFIGURED_KEY_ATTRS = (
        "buy_latest_key", "buy_ask1_key", "sell_latest_key",
        "sell_bid1_key", "closing_buy_limit_key", "closing_sell_limit_key",
        "cancel_key", "cancel_single_key", "position_key",
    )

    @staticmethod
    def _norm_key(k) -> str:
        return str(k).strip().strip("{}").upper()

    def _allowed_key_set(self) -> set:
        """生效白名单: allowed_keys非空用之, 否则内置F1-F12; 并并入config
        中显式配置的全部键位(含用户自定义尾盘顶格键), 保证配置行为不被拦。"""
        hk = self.hk
        if getattr(hk, "allowed_keys", None):
            base = {self._norm_key(k) for k in hk.allowed_keys
                    if str(k).strip()}
        else:
            base = {f"F{i}" for i in range(1, 13)}
        for attr in self._CONFIGURED_KEY_ATTRS:
            v = getattr(hk, attr, "")
            if v:
                base.add(self._norm_key(v))
        return base

    def _key_allowed(self, key) -> bool:
        if not getattr(self.hk, "key_whitelist_enable", False):
            return True
        return self._norm_key(key) in self._allowed_key_set()

    # ---------- 委托回查 ----------

    def _entrust_ids(self) -> set:
        """当前当日委托的合同号集合(快照)。异常返回None表示回查不可用。"""
        try:
            ents = self._td_user.today_entrusts or []
            return {str(e.get("合同编号", "")) for e in ents if e.get("合同编号")}
        except Exception as e:
            log.warning("委托快照读取失败: %s", e)
            return None

    def _find_new_entrust(self, code: str, action: str, before: set):
        """在当日委托中找目标股票+方向的新委托, 返回dict或None(未找到/读取失败)。

        返回 ('ok', entrust_dict) / ('none', None) / ('error', None)
        """
        try:
            ents = self._td_user.today_entrusts or []
        except Exception as e:
            log.warning("委托回查读取失败(可能触发验证码): %s", e)
            return ("error", None)
        direction = "买入" if action.upper() == "BUY" else "卖出"
        for e in ents:
            cid = str(e.get("合同编号", ""))
            if cid in before:
                continue
            if str(e.get("证券代码", ""))[-6:] == code[-6:] \
                    and direction in str(e.get("操作", "")):
                return ("ok", e)
        return ("none", None)

    @staticmethod
    def _entrust_status(e: dict) -> str:
        """按成交数量判定 filled/partial/pending。"""
        try:
            qty = int(float(e.get("委托数量", 0) or 0))
            filled = int(float(e.get("成交数量", 0) or 0))
        except (TypeError, ValueError):
            return "pending"
        if filled <= 0:
            return "pending"
        return "filled" if filled >= qty else "partial"

    def _verify(self, code: str, action: str, before: set):
        """轮询回查, 返回 (ok, result_dict)。"""
        hk = self.hk
        deadline = time.time() + hk.verify_timeout
        while time.time() < deadline:
            state, e = self._find_new_entrust(code, action, before)
            if state == "ok":
                status = self._entrust_status(e)
                log.info("回查命中: %s %s 合同%s 状态=%s",
                         action, code, e.get("合同编号"), status)
                return True, {
                    "ok": True,
                    "entrust_no": str(e.get("合同编号", "")),
                    "filled_price": float(e.get("委托价格", 0) or 0),
                    "status": status,
                    "mode": "hotkey",
                }
            if state == "error":
                return False, {
                    "ok": False, "uncertain": True, "mode": "hotkey",
                    "error": "下单后委托回查失败(可能触发拷贝验证码), "
                             "请人工确认是否成交",
                }
            time.sleep(hk.verify_interval)
        # 超时: 委托可能已产生但xiadan未回报, F8撤该股挂单防意外成交
        self._cancel_single_by_hotkey(code, action)
        log.warning("回查超时, 已F8单只撤单 %s %s(防挂单意外成交)", action, code)
        return False, {
            "ok": False, "mode": "hotkey",
            "error": "超时未查询到新委托, 已F8撤该股挂单(快捷键可能未生效或回报延迟)",
        }

    # ---------- 秒撤(F5键盘, 不做物理坐标定位) ----------

    def _cancel_by_hotkey(self) -> bool:
        """发F5秒撤(撤销全部未成交委托)。

        用于系统级异常(熔断/设备掉线需全撤)和盘后清理。
        """
        try:
            win = self._hexin_win
            win.set_focus()
            time.sleep(0.3)
            log.info("发送秒撤快捷键 %s", self.hk.cancel_key)
            win.type_keys(self.hk.cancel_key)
            time.sleep(0.8)
            return self._confirm_cancel_dialog()
        except Exception as e:
            log.warning("F5秒撤异常: %s", e)
            return False

    def _active_code(self) -> str:
        """从行情端主窗口标题提取当前个股6位代码; 提取不到返回''。

        同花顺各版本标题格式不一(可能含"名称(代码)"/"代码-名称"等),
        提取不到属"无法核对"而非"核对不一致", 调用方fail-open处理。
        """
        try:
            title = self._hexin_win.window_text() or ""
        except Exception:
            return ""
        m = _TITLE_CODE_RE.search(title)
        return m.group(1) if m else ""

    def _record_cancel_failopen(self, code: str):
        """F8撤单前标题无法核对(fail-open仍撤)时计滑窗, 达阈值升CRITICAL。

        2026-09-18采纳(豆包): 单次"解析不出"放行合理, 但短时间连续解析不出
        说明窗口标题规则变化/视觉链路异常, 继续fail-open有误撤风险, 需告警。
        计数降回阈值以下自动重新武装。fail-open语义本身不变(本次照常撤)。
        """
        hk = self.hk
        if not getattr(hk, "cancel_fail_alert_enable", False):
            return
        now = time.time()
        win = float(getattr(hk, "cancel_fail_window_sec", 1800.0))
        dq = self._cancel_failopen_ts
        dq.append(now)
        while dq and (now - dq[0]) > win:
            dq.popleft()
        n = len(dq)
        cap = int(getattr(hk, "cancel_fail_max", 3))
        if n < cap:
            self._cancel_failopen_alerted = False
            return
        if self._cancel_failopen_alerted:
            return
        self._cancel_failopen_alerted = True
        get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir)).log(
            "anomaly", stage="cancel_failopen_burst", code=code, count=n,
            window_sec=win)
        self._notify(
            "F8核对连续无法解析, 请人工确认窗口",
            f"{win:.0f}s内已有{n}次撤单前无法从窗口标题解析代码(fail-open"
            f"仍照常撤单)。可能标题格式变化或行情窗口异常, 继续自动撤单"
            f"存在误撤风险, 请人工核对同花顺窗口/委托列表。",
            level="CRITICAL")

    def _cancel_single_by_hotkey(self, code: str, side: str = "") -> bool:
        """发F8单只撤单(撤销当前股票的买卖挂单), 返回是否可确认撤单生效。

        先定位到目标股票页面再发F8, 弹窗处理同_cancel_by_hotkey。
        用于下单超时/挂单到期等针对单只股票的撤单场景。

        F8前后核对(2026-09-15用户裁定, cancel_verify_enable开关):
        - 撤单前: 窗口标题代码与目标code明确不一致 -> 不发F8+CRITICAL
          (防键盘精灵错位撤错股票); 标题提取不到代码 -> 无法核对, 照常撤;
        - 撤单后: F6复查持仓, BUY撤后仍有持仓/SELL撤后无持仓 => 疑似撤单
          晚于成交, 或F6链路异常 => 只CRITICAL人工介入, 返回False(台账保留
          pending不自动转canceled)。绝不自动补撤/双撤循环。
        """
        code = str(code)
        side = (side or "").upper()
        try:
            self._connect_hexin()
            self._goto_stock(code)
            # ---- 撤单前: 窗口代码核对 ----
            if self.hk.cancel_verify_enable:
                active = self._active_code()
                if active and active != code:
                    el = get_event_log(
                        self.cfg.resolve(self.cfg.paths.logs_dir))
                    el.log("trade", stage="cancel_code_mismatch", code=code,
                           name="", action=side or None, active_code=active)
                    self._notify(
                        f"F8撤单前代码不一致, 已中止撤单 {code}",
                        f"键盘精灵定位后窗口标题代码为{active}, 与目标{code}"
                        f"不一致, 为防撤错股票未发F8。请人工核对行情窗口与"
                        f"挂单后手动撤单。", level="CRITICAL")
                    return False
                if not active:
                    log.warning("F8撤单前窗口标题未能解析出股票代码"
                                "(无法核对, fail-open照常撤单): %s", code)
                    self._record_cancel_failopen(code)
            # ---- 撤单竞态: 撤前F6终核(默认关, 2026-09-18采纳, 智谱A2) ----
            # 只在此开关开启时改变时序; 检测出"疑似已成交"则不发F8+CRITICAL;
            # 检测链路异常仍fail-open照常发F8(只CRITICAL), 不触碰现行F8语义。
            if (getattr(self.hk, "cancel_race_check_enable", False)
                    and side in ("BUY", "SELL")):
                no_pos0, score0, det_ok0 = self._f6_check_position(
                    ctx="cancel_race", code=code, action=side)
                el0 = get_event_log(
                    self.cfg.resolve(self.cfg.paths.logs_dir))
                if det_ok0:
                    raced = (not no_pos0) if side == "BUY" else no_pos0
                    if raced:
                        hint0 = ("F6已见持仓, 买单可能已成交"
                                 if side == "BUY"
                                 else "F6已无持仓, 卖单可能已成交")
                        el0.log("trade", stage="cancel_race_filled",
                                code=code, action=side,
                                score=round(float(score0), 3))
                        self._notify(
                            f"撤单竞态终核: 疑似已成交, 未发F8 {code}",
                            f"{side}挂单撤单前F6终核{hint0}"
                            f"(score={score0:.2f}), 为避免撤掉已成单未发F8。"
                            f"台账保留挂单中, 请人工核对成交(系统不自动补账)。",
                            level="CRITICAL")
                        return False
                else:
                    el0.log("trade", stage="cancel_race_error", code=code,
                            action=side)
                    self._notify(
                        f"撤单竞态终核F6异常, fail-open继续撤单 {code}",
                        f"{side}挂单撤单前F6终核链路异常, 仍按fail-open"
                        f"照常发F8(现行行为不变), 请人工核对成交与撤单。",
                        level="CRITICAL")
            win = self._hexin_win
            win.set_focus()
            time.sleep(0.3)
            log.info("发送单只撤单快捷键 %s: %s %s",
                     self.hk.cancel_single_key, code, side or "")
            win.type_keys(self.hk.cancel_single_key)
            time.sleep(0.8)
            confirmed = self._confirm_cancel_dialog()
            if not confirmed:
                return False
            # ---- 撤单后: F6验证委托确已消失(按方向解读持仓语义) ----
            if self.hk.cancel_verify_enable and side in ("BUY", "SELL"):
                no_pos, score, det_ok = self._f6_check_position(
                    ctx="cancel_verify", code=code, action=side)
                el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
                if not det_ok:
                    el.log("trade", stage="cancel_verify_error", code=code,
                           action=side)
                    self._notify(
                        f"F8撤单后状态无法确认 {code}",
                        f"{side}挂单已发F8但撤单后F6检测链路异常, 未自动改台账"
                        f"(保留挂单中状态), 请人工核对是否撤单成功/是否已成交。",
                        level="CRITICAL")
                    return False
                suspect = (not no_pos) if side == "BUY" else no_pos
                if suspect:
                    hint = ("F6显示仍有持仓: 买单可能在撤单前已成交"
                            if side == "BUY"
                            else "F6显示已无持仓: 卖单可能在撤单前已成交")
                    el.log("trade", stage="cancel_verify_suspect_filled",
                           code=code, action=side, no_position=no_pos,
                           score=round(float(score), 3))
                    self._notify(
                        f"F8撤单后疑似已成交 {code}",
                        f"{side}挂单F8撤单后{hint}(score={score:.2f}), "
                        f"台账保留挂单中状态, 请人工核对成交与持仓"
                        f"(系统不会自动补撤/补账)。", level="CRITICAL")
                    return False
                log.info("F8撤单后F6核对通过 %s %s(score=%.2f)",
                         side, code, score)
            return True
        except Exception as e:
            log.warning("F8单只撤单异常: %s", e)
            return False

    def _confirm_cancel_dialog(self) -> bool:
        """秒撤后若弹确认框(#32770), 优先点"是/确定"按钮, 兜底发ENTER。"""
        try:
            import win32gui
            import win32process
            pid = self._find_pid("hexin.exe")
            if not pid:
                return False
            dialogs = []

            def _handler(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                    cls = win32gui.GetClassName(hwnd)
                    if wpid == pid and cls == "#32770" and title:
                        dialogs.append((hwnd, title))

            win32gui.EnumWindows(_handler, None)
            if not dialogs:
                # 无确认弹窗: 用户已设F5按下即撤, 直接生效(正常路径)
                return True
            import pywinauto
            for hwnd, title in dialogs:
                log.info("秒撤确认弹窗: '%s', 确认", title)
                dlg = pywinauto.Application().connect(
                    handle=hwnd, timeout=3).window(handle=hwnd)
                for btn in dlg.descendants(class_name="Button"):
                    t = btn.window_text() or ""
                    if btn.is_visible() and any(
                            k in t for k in ("是", "确定", "确认")):
                        btn.click()        # 控件级点击, 非屏幕坐标
                        time.sleep(0.5)
                        return True
                dlg.type_keys("{ENTER}")   # 兜底: 默认焦点通常在"是"
                time.sleep(0.5)
                return True
        except Exception as e:
            log.warning("秒撤弹窗处理异常: %s", e)
        return False

    def _notify(self, title: str, content: str, level: str = "WARNING"):
        """手机推送(配置了notifier才发); 异常不影响主流程。"""
        log.warning("通知(%s): %s | %s", level, title, content)
        if self.notifier is not None:
            try:
                self.notifier.send(title, content, level=level)
            except Exception as e:
                log.warning("通知推送异常: %s", e)

    # ---------- 挂单后台监控(5分钟未成交F5撤单, 不阻塞扫描) ----------

    def _pending_check_once(self, code: str, name: str, signal_price: float,
                            action: str = "BUY",
                            wait_seconds: float = 0.0):
        """挂单到期复查(调用方须持_win_lock): 成交补建仓/补清仓; 未成交撤单。

        BUY延迟成交→补建仓; SELL延迟成交→补清仓; 未成交→F8/F5撤单+
        台账canceled(决策层下轮按价格条件决定是否重挂; 尾盘竞价兜底)。
        """
        from ths.quote import price_compare_text
        buy = action.upper() == "BUY"
        wait_seconds = wait_seconds or self.hk.buy_pending_wait
        el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
        self._connect_hexin()
        self._goto_stock(code)
        no_pos, score, det_ok = self._f6_check_position(
            ctx="pending_monitor", code=code, action=action)
        now = time.strftime("%H:%M:%S")
        price_txt = price_compare_text(code, signal_price)
        if not det_ok:
            # F6检测链路异常: 不补仓也不撤单(委托状态未知), 只告警人工核对
            el.log("trade", stage="pending_monitor_error", code=code,
                   name=name, error="到期复查时F6检测链路异常", time_str=now,
                   action=action)
            self._notify(
                f"挂单状态不明 {code} {name}",
                f"{action}挂单{wait_seconds:.0f}秒到期复查时F6检测异常,"
                f"未自动补仓/撤单。{price_txt}。请人工核对委托与持仓。",
                level="CRITICAL")
            return
        filled = (not no_pos) if buy else no_pos
        if filled:
            el.log("trade", stage="pending_filled", code=code,
                   name=name, time_str=now, signal_price=signal_price,
                   action=action)
            self.risk.set_attempt_status(code, action, "filled")
            try:
                store = self.positions
                if store is None:
                    from models.positions import PositionStore
                    store = PositionStore(
                        self.cfg.resolve(self.cfg.positions.file))
                if buy:
                    store.add(code, name=name,
                              note=f"挂单延迟成交补建仓 {now}")
                    self._notify(
                        f"延迟成交 {code} {name}",
                        f"买入挂单于{now}成交(确认窗口内未回报), 已补建仓。"
                        f"{price_txt}。")
                else:
                    store.remove(code)
                    self._notify(
                        f"延迟成交 {code} {name}",
                        f"卖出挂单于{now}成交, 已补清仓。{price_txt}。")
            except Exception as e:
                log.error("延迟成交持仓修正失败 %s %s: %s", action, code, e)
                self._notify(
                    f"成交但持仓修正失败 {code} {name}",
                    f"{action}挂单{now}成交, 持仓文件自动修正失败({e}), "
                    f"请人工核对positions.json",
                    level="CRITICAL")
        else:
            # 挂单未成交: 始终F8单只撤(不用F5全撤——账户上可能同时挂着
            # 跌停排队卖单queue_only, F5会误撤; F5仅系统级异常/盘后清理用)
            # 收盘集合竞价硬门禁: 工作日14:57-15:00交易所禁止撤单, F8是废单,
            # 只告警留单到15:00(与封单监控同策略, 2026-09-15合规硬约束)
            if self.seal_watcher._in_closing_auction():
                el.log("trade", stage="pending_cancel_blocked_closing",
                       code=code, name=name, time_str=now, action=action)
                self._notify(
                    f"14:57收盘集合竞价禁撤, 挂单保留 {code} {name}",
                    f"{action}挂单{wait_seconds:.0f}秒到期复查时未成交, 但已进入"
                    f"14:57-15:00收盘集合竞价(交易所禁止撤单), 未发F8, 挂单"
                    f"原样保留参与撮合。{price_txt}。请15:00后人工/次日启动"
                    f"恢复门核对成交。", level="CRITICAL")
                return
            cancelled = self._cancel_single_by_hotkey(code, action)
            if not cancelled:
                # F8前代码不一致/撤单后F6疑似成交或检测异常: 状态未知,
                # 不转canceled(CRITICAL已发), 留待人工/次日启动恢复门
                el.log("trade", stage="pending_cancel_unverified", code=code,
                       name=name, time_str=now, signal_price=signal_price,
                       action=action)
                self._notify(
                    f"挂单撤单未获确认 {code} {name}",
                    f"{action}挂单未成交但F8撤单核对未通过(代码不一致/疑似已成交"
                    f"/F6异常), 台账保留挂单中状态, 系统不会补撤, 请人工核对。",
                    level="CRITICAL")
                return
            method = "F8单只撤"
            self.risk.set_attempt_status(code, action, "canceled")
            el.log("trade", stage="pending_canceled", code=code,
                   name=name, time_str=now, signal_price=signal_price,
                   action=action)
            if buy:
                hint = ("下轮仍为多头且现价≤本次挂单价时自动补单; "
                        "尾盘竞价仍未成交将兜底买入")
            else:
                hint = ("下轮仍为空头且现价≥本次挂单价时自动重挂; "
                        "尾盘竞价仍未卖出将兜底卖出")
            self._notify(
                f"挂单{wait_seconds:.0f}秒未成交已撤 {code} {name}",
                f"{action}挂单未成交, {method}({now})。{price_txt}。{hint}。",
                level="WARNING")

    def _spawn_pending_monitor(self, code: str, name: str,
                               signal_price: float = 0.0,
                               action: str = "BUY",
                               wait_seconds: float = 0.0):
        """F6快速确认窗口内未成交: 后台线程等到wait_seconds总时长后复查。

        复查F6持仓: 已成交->补建仓/补清仓positions.json+推送; 仍未成交
        ->F8/F5撤单+台账canceled+推送; 检测异常->不补不撤只告警(绝不在
        委托状态未知时操作)。
        后台线程操作PC端hexin, 全程持_win_lock与主轮交易串行, 防止
        交叉set_focus/键盘精灵导致热键发错股票或F5误撤。
        action: BUY/SELL; wait_seconds: 挂单总等待时长(开盘窗口5分钟,
        其余2分钟); signal_price: 信号/下单时价格, 供告警与二次挂单条件。
        """
        action = action.upper()
        wait_seconds = wait_seconds or self.hk.buy_pending_wait
        # F6快速确认窗口已经过去的时长(BUY=buy_quick_confirm, SELL=f6_verify_timeout)
        elapsed_cap = (self.hk.buy_quick_confirm if action == "BUY"
                       else self.hk.f6_verify_timeout)

        def _run():
            remain = wait_seconds - elapsed_cap
            if remain > 0:
                time.sleep(remain)
            el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
            try:
                # 主轮优先: 等交易意图清零再抢窗口锁, 避免阻塞信号交易
                while self._trading_intent.is_set():
                    time.sleep(1.0)
                with self._win_lock:
                    self._pending_check_once(code, name, signal_price,
                                             action=action,
                                             wait_seconds=wait_seconds)
            except Exception as e:
                log.error("挂单监控线程异常 %s %s: %s", action, code, e)
                el.log("trade", stage="pending_monitor_error", code=code,
                       name=name, error=str(e)[:300], action=action)
                self._notify(f"挂单监控异常 {code}",
                             f"后台复查/撤单异常: {e}, 请人工核对委托状态。",
                             level="CRITICAL")

        threading.Thread(target=_run, daemon=True).start()
        log.info("已启动挂单后台监控: %s %s, %.0fs后复查(未成交F8/F5撤单)",
                 action, code, wait_seconds)

    # ---------- 排队单收盘成交复查(跌停排队卖/尾盘竞价单, 只补账不撤单) ----------

    def _spawn_closing_fill_check(self, code: str, name: str,
                                  signal_price: float = 0.0,
                                  action: str = "BUY"):
        """queue_only挂单: 后台线程在15:00后按closing_recheck_delays多次F6复查。

        只补账(成交补建仓/补清仓)绝不撤单; 复查时台账已非pending(如封单
        监控盘中F8撤单转人工)则跳过; 前几次检测链路异常只退避重试, 最后
        一次仍异常才转unknown+CRITICAL(启动恢复门次日兜底), 避免盘后瞬时
        卡顿直接误判unknown(2026-09-18采纳, 豆包)。
        """
        action = action.upper()

        def _run():
            # 退避时刻 = 今日15:00:00 + closing_recheck_delays(默认5/600/1800s)
            delays = list(getattr(self.hk, "closing_recheck_delays", None)
                          or [5])
            base = datetime.now().replace(
                hour=15, minute=0, second=0, microsecond=0)
            el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
            for i, d in enumerate(delays):
                target = base + timedelta(seconds=float(d))
                wait = (target - datetime.now()).total_seconds()
                if wait > 0:
                    time.sleep(wait)
                elif i > 0:
                    time.sleep(1.0)   # 已过退避时刻(如盘后启动), 不空转
                else:
                    time.sleep(1.0)
                try:
                    # 主轮优先: 等交易意图清零再抢窗口锁
                    while self._trading_intent.is_set():
                        time.sleep(1.0)
                    with self._win_lock:
                        result = self._queue_fill_check_once(
                            code, name, signal_price, action,
                            attempt=i, max_attempts=len(delays))
                    if result != "retry":
                        return
                except Exception as e:
                    log.error("收盘成交复查线程异常 %s %s: %s", action, code, e)
                    el.log("trade", stage="closing_check_error", code=code,
                           name=name, error=str(e)[:300], action=action)
                    self._notify(f"收盘成交复查异常 {code}",
                                 f"{action}排队单收盘后复查异常: {e}, "
                                 f"请人工核对成交与持仓。",
                                 level="CRITICAL")
                    return

        threading.Thread(target=_run, daemon=True).start()
        log.info("已排队单收盘复查: %s %s (15:00后按%s退避F6补账, 不撤单)",
                 action, code,
                 getattr(self.hk, "closing_recheck_delays", [5]))

    def _queue_fill_check_once(self, code: str, name: str,
                               signal_price: float, action: str,
                               attempt: int = 0,
                               max_attempts: int = 1) -> str:
        """15:00后排队单成交复查(调用方须持_win_lock): 成交补账/未成交告警。

        与_pending_check_once的区别: 全程不撤单(集合竞价价成交是用户
        指定的兜底路径; 跌停排队卖同理)。

        返回: "done"=已定论(成交/未成交/台账非pending/末次unknown);
        "retry"=本次检测链路异常但还有后续退避, 调用方应稍后重试。
        """
        from ths.quote import price_compare_text
        buy = action == "BUY"
        att = self.risk.get_attempt(code, action)
        if att and att.get("status") != "pending":
            log.info("收盘复查跳过 %s %s: 台账状态=%s(盘中已处置)",
                     action, code, att.get("status"))
            return "done"
        el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
        self._connect_hexin()
        self._goto_stock(code)
        no_pos, score, det_ok = self._f6_check_position(
            ctx="closing_check", code=code, action=action)
        now = time.strftime("%H:%M:%S")
        price_txt = price_compare_text(code, signal_price)
        if not det_ok:
            last = attempt >= max_attempts - 1
            if not last:
                log.warning("收盘复查F6链路异常, 退避稍后重试 %s %s"
                            "(attempt=%d/%d)", action, code,
                            attempt + 1, max_attempts)
                el.log("trade", stage="closing_check_retry", code=code,
                       name=name, action=action, attempt=attempt + 1)
                return "retry"
            self.risk.set_attempt_status(code, action, "unknown")
            el.log("trade", stage="closing_check_error", code=code,
                   name=name, error=f"收盘复查F6连续{max_attempts}次异常",
                   action=action)
            self._notify(
                f"排队单收盘状态不明 {code} {name}",
                f"{action}排队单15:00后{max_attempts}次复查F6均异常, "
                f"未自动补账也不撤单。{price_txt}。请人工核对成交与持仓"
                f"(明早启动恢复门也会拦截)。",
                level="CRITICAL")
            return "done"
        filled = (not no_pos) if buy else no_pos
        if filled:
            self.risk.set_attempt_status(code, action, "filled")
            el.log("trade", stage="closing_filled", code=code, name=name,
                   time_str=now, signal_price=signal_price, action=action)
            try:
                store = self.positions
                if store is None:
                    from models.positions import PositionStore
                    store = PositionStore(
                        self.cfg.resolve(self.cfg.positions.file))
                if buy:
                    store.add(code, name=name, note=f"尾盘竞价成交补建仓 {now}")
                    self._notify(
                        f"尾盘竞价成交 {code} {name}",
                        f"买入排队单已于集合竞价成交({now}), 已补建仓。"
                        f"{price_txt}。")
                else:
                    store.remove(code)
                    self._notify(
                        f"尾盘竞价成交 {code} {name}",
                        f"卖出排队单已于集合竞价成交({now}), 已补清仓。"
                        f"{price_txt}。")
            except Exception as e:
                log.error("收盘成交持仓修正失败 %s %s: %s", action, code, e)
                self._notify(
                    f"成交但持仓修正失败 {code} {name}",
                    f"{action}排队单{now}成交, 持仓文件修正失败({e}), "
                    f"请人工核对positions.json", level="CRITICAL")
        else:
            # 15:00撮合后仍未成交: 交易所闭市自动废单, 台账转canceled
            self.risk.set_attempt_status(code, action, "canceled")
            el.log("trade", stage="closing_unfilled", code=code, name=name,
                   time_str=now, signal_price=signal_price, action=action)
            self._notify(
                f"排队单集合竞价未成交 {code} {name}",
                f"{action}排队单截至{now}仍未成交, 闭市后委托自动失效"
                f"(台账已记canceled)。{price_txt}。请明日人工决策。",
                level="WARNING")
        try:
            self.seal_watcher.remove(code)
        except Exception:
            pass
        return "done"

    # ---------- F6持仓验证(下单后, 行情端浮层, 不弹xiadan) ----------

    def check_holding(self, code: str, name: str = "") -> dict:
        """持仓对账: F6查指定个股在券商侧是否真有持仓(行情端浮层, 无验证码)。

        人工手动买卖不会写positions.json, 本方法供调度器定期对账:
        系统记录持仓但这里查到"无持仓" => 疑似人工卖出。
        返回 {"ok": 链路是否正常, "no_pos": 是否无持仓, "score": 匹配置信度,
        "error": 错误信息}; ok=False时no_pos无意义, 调用方不得据此修正持仓。
        """
        try:
            with self._win_lock:
                self._connect_hexin()
                self._goto_stock(code)
                no_pos, score, ok = self._f6_check_position(
                    ctx="manual_holding", code=code)
            return {"ok": ok, "no_pos": bool(no_pos), "score": score,
                    "error": "" if ok else "F6检测链路异常"}
        except Exception as e:
            log.warning("check_holding异常 %s: %s", code, e)
            return {"ok": False, "no_pos": False, "score": 0.0,
                    "error": str(e)[:200]}

    def _f6_check_position(self, ctx: str = "", code: str = "",
                           action: str = "", name: str = "") -> tuple:
        """发F6查当前个股持仓, 返回 (无持仓bool, score, 检测链路ok)。

        用户键位: F6=查个股持仓(行情端浮层), F7=查当日委托(弹交易系统,
        弃用)。浮层显示"当前股票无持仓"=无持仓; 其他内容=有持仓。
        查完ESC关浮层(会退回自选列表, 下次goto自会重新进个股页)。
        ctx/code/action/name: 调用场景标识, 供F6连续异常降级只读对账告警。
        """
        from trader.pcwin import f6_no_position
        try:
            win = self._hexin_win
            win.set_focus()
            time.sleep(0.3)
            win.type_keys(self.hk.position_key)
            time.sleep(1.2)
            no_pos, score, det_ok = f6_no_position(win.rectangle())
            win.type_keys("{ESC}")       # 清浮层回列表
            time.sleep(0.8)
            self._f6_detection(det_ok, ctx, code, action, name)
            return no_pos, score, det_ok
        except Exception as e:
            log.warning("F6持仓查询异常: %s", e)
            self._f6_detection(False, ctx, code, action, name)
            return False, 0.0, False

    # ---------- F6链路健康: 连续异常自动降级xiadan只读对账 ----------

    F6_QUALITY_WINDOW = 20      # F6检测质量滑窗样本数
    F6_QUALITY_RATE = 0.30      # 滑窗内异常率>=30%告质量(每日边沿一次)

    def _f6_detection(self, ok: bool, ctx: str, code: str,
                      action: str, name: str):
        """F6检测结果健康统计: 成功清零; 连续异常达阈值触发只读对账。

        非Windows(开发/CI机)F6截屏链路本就不可用, 不计数不弹xiadan;
        触发后streak清零+cooldown节流, 防反复弹交易端阻塞热键。
        另有近20次滑窗异常率质量告警(捕捉时好时坏的链路劣化)。
        """
        hk = self.hk
        if not getattr(hk, "f6_fallback_enable", True):
            return
        try:
            from instance_lock import IS_WINDOWS
            is_win = bool(IS_WINDOWS)
        except Exception:
            is_win = False
        if ok:
            if is_win:
                self._f6_quality_sample(True)
                if self._f6_err_streak:
                    with self._f6_health_lock:
                        self._f6_err_streak = 0
            return
        if not is_win:
            return
        self._f6_quality_sample(False)
        fire, n = False, 0
        with self._f6_health_lock:
            self._f6_err_streak += 1
            n = self._f6_err_streak
            threshold = max(1, int(getattr(hk, "f6_fallback_threshold", 2)))
            cooldown = float(getattr(hk, "f6_fallback_cooldown", 1800.0))
            now = time.time()
            if n >= threshold and (now - self._f6_last_fallback_ts) >= cooldown:
                self._f6_last_fallback_ts = now
                self._f6_err_streak = 0
                fire = True
        log.warning("F6检测链路异常(连续%d次, ctx=%s %s %s)",
                    n, ctx or "-", action or "", code or "")
        if fire:
            self._spawn_readonly_reconcile(ctx, code, action, name, n)

    def _f6_quality_sample(self, ok: bool):
        """F6检测质量滑窗: 异常率超阈值每日只告一次(WARNING, 不降级不阻断)。"""
        q = self._f6_quality
        q.append(1 if ok else 0)
        if len(q) < self.F6_QUALITY_WINDOW:
            return
        rate = 1.0 - sum(q) / len(q)
        if rate < self.F6_QUALITY_RATE:
            return
        today = time.strftime("%Y%m%d")
        if self._f6_quality_alerted == today:
            return
        self._f6_quality_alerted = today
        log.critical("F6检测质量告警: 近%d次异常率%.0f%%>=%.0f%%, "
                     "视觉识别链路可能劣化(质量告警, 未触发连续异常降级)",
                     len(q), rate * 100, self.F6_QUALITY_RATE * 100)
        try:
            get_event_log(self.cfg.resolve(
                self.cfg.paths.logs_dir)).log(
                "trade", stage="f6_quality", window=len(q),
                error_rate=round(rate, 3))
        except Exception:
            pass
        self._notify(
            "F6检测质量告警",
            f"近{len(q)}次F6持仓检测异常率{rate:.0%}(阈值"
            f"{self.F6_QUALITY_RATE:.0%}), 截图/模板识别链路可能时好时坏"
            f"(未达连续异常降级条件, 系统照常运行), 请人工关注检测准确性。",
            level="WARNING")

    def _spawn_readonly_reconcile(self, ctx: str, code: str, action: str,
                                  name: str, streak: int):
        """F6连续异常: 后台跑xiadan只读对账(复用manual_audit采集, 只读不写)。"""
        def _run():
            el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
            try:
                # 主轮优先: 等交易意图清零再抢窗口锁(采集会弹xiadan)
                while self._trading_intent.is_set():
                    time.sleep(1.0)
                with self._win_lock:
                    self._run_readonly_reconcile(
                        ctx, code, action, name, streak)
            except Exception as e:
                log.error("F6降级只读对账线程异常: %s", e)
                try:
                    el.log("trade", stage="f6_fallback_error",
                           error=str(e)[:300])
                except Exception:
                    pass
                self._notify("F6异常只读对账失败",
                             f"自动降级对账执行异常: {e}, 请人工核对委托与持仓。",
                             level="CRITICAL")

        threading.Thread(target=_run, daemon=True).start()
        log.warning("F6连续%d次异常, 已调度xiadan只读对账(后台, 只读不写不撤)",
                    streak)

    def _run_readonly_reconcile(self, ctx: str, code: str, action: str,
                                name: str, streak: int):
        """只读对账实际采集(调用方持_win_lock): 差异CRITICAL, 绝不apply/撤补。"""
        from trader import manual_audit
        el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
        el.log("trade", stage="f6_fallback_reconcile", context=ctx or None,
               code=code or None, action=action or None, name=name or None,
               streak=streak)
        data = manual_audit.collect_broker_data(self.cfg)
        if not data.get("ok"):
            el.log("trade", stage="f6_fallback_error",
                   error=str(data.get("error"))[:300])
            self._notify(
                "F6异常降级: xiadan对账连接失败",
                f"F6检测连续{streak}次异常, 尝试只读对账但: {data.get('error')}。"
                f"系统未自动改仓/撤单, 请立即人工核对委托与持仓。",
                level="CRITICAL")
            return
        broker = data.get("broker_positions", {}) or {}
        sysnap = data.get("system_positions", []) or []
        sys_codes = {str(p.get("code")) for p in sysnap if p.get("code")}
        broker_codes = set(broker.keys())
        only_broker = sorted(broker_codes - sys_codes)
        only_sys = sorted(sys_codes - broker_codes)
        opens = data.get("open_entrusts", []) or []
        errors = data.get("errors", {}) or {}
        diff = bool(only_broker or only_sys or opens)
        lines = [f"触发: {ctx or 'F6检测'}连续{streak}次链路异常"
                 f"(只读对账, 系统未自动改仓/撤补)"]
        if only_broker:
            lines.append("券商有/系统无: " + ",".join(only_broker))
        if only_sys:
            lines.append("系统有/券商无: " + ",".join(only_sys))
        if opens:
            lines.append(f"未完成委托{len(opens)}笔: " + ";".join(
                f"{o.get('code')}{o.get('side')}{o.get('qty')}@{o.get('price')}"
                for o in opens[:10]))
        if errors:
            lines.append("采集告警: "
                         + ";".join(f"{k}:{v}" for k, v in errors.items()))
        if not diff and not errors:
            lines.append("券商持仓与系统一致, 无未完成委托(F6疑似误报)")
        el.log("trade", stage="f6_fallback_result", read_only=True,
               only_broker=only_broker, only_system=only_sys,
               open_entrusts=len(opens), has_diff=diff or None,
               errors=";".join(errors.keys()) or None)
        self._notify(
            "F6异常只读对账: 发现差异请核对" if diff
            else "F6异常只读对账完成(无差异)",
            "\n".join(lines), level="CRITICAL" if diff else "WARNING")

    def _verify_by_f6(self, code: str, action: str, name: str = "",
                      signal_price: float = 0.0,
                      queue_only: bool = False) -> dict:
        """下单后F6持仓验证(轮询: 成交立即返回, 不等满窗口)。

        BUY:  下单f6_first_check(5s)后首次F6查持仓; 未成交每f6_poll_interval
              秒复查(需重新goto个股页, 因F6查完ESC退回列表), 直到
              buy_quick_confirm(30s)上限: 成交->成功; 超时->pending, 后台
              线程到期复查, 仍未成交F8撤单+告警。
        SELL: 同样轮询, 上限f6_verify_timeout(12s): 变"无持仓"=清仓成功;
              超时仍有持仓: 普通单→pending后台到期撤单+告警(二次空头
              需现价≥首次挂单价才重挂); queue_only(跌停排队/尾盘竞价)
              →保留排队不撤(跌停由封单监控盯封单大减自动撤)。
        queue_only(跌停排队卖/尾盘竞价买卖): 集合竞价15:00才撮合, 不做长
              轮询, 仅首查一次, 未成交即保留挂单+挂15:00成交复查(补建/
              补清仓; 不撤单)。
        signal_price: 信号时价/挂单价, 告警与二次挂单价格条件依据。
        """
        buy = action.upper() == "BUY"
        if queue_only:
            cap = self.hk.f6_first_check
        else:
            cap = self.hk.buy_quick_confirm if buy else self.hk.f6_verify_timeout
        t0 = time.time()
        time.sleep(self.hk.f6_first_check)
        no_pos, score, ok = True, 0.0, False
        checks = 0
        while True:
            if checks > 0:
                # 上次F6查完ESC退回了自选列表, 复查前需重新进个股页
                self._goto_stock(code)
            no_pos, score, ok = self._f6_check_position(
                ctx="order_verify", code=code, action=action)
            checks += 1
            elapsed = time.time() - t0
            if ok:
                if buy and not no_pos:
                    log.info("F6验证成交 BUY %s: 第%d次查询确认持仓(%.1fs)",
                             code, checks, elapsed)
                    return {"ok": True, "entrust_no": "",
                            "filled_price": signal_price,
                            "status": "filled", "mode": "hotkey_f6",
                            "detail": f"F6持仓确认(score={score:.2f},"
                                      f"{elapsed:.0f}s)"}
                if not buy and no_pos:
                    log.info("F6验证清仓 SELL %s: 第%d次查询确认无持仓(%.1fs)",
                             code, checks, elapsed)
                    return {"ok": True, "entrust_no": "",
                            "filled_price": signal_price,
                            "status": "filled", "mode": "hotkey_f6",
                            "detail": f"F6确认已清仓(score={score:.2f},"
                                      f"{elapsed:.0f}s)"}
            if elapsed >= cap:
                break
            time.sleep(min(self.hk.f6_poll_interval, max(0.5, cap - elapsed)))

        log.info("F6持仓验证 %s %s: %ds内%d次查询 无持仓=%s score=%.2f",
                 action, code, cap, checks, no_pos, score)
        if not ok:
            return {"ok": False, "uncertain": True, "mode": "hotkey_f6",
                    "error": "F6持仓检测链路异常, 请人工确认成交状态"}
        if queue_only:
            # 跌停排队卖/尾盘竞价买卖: 竞价撮合前不可能成交, 保留挂单不撤,
            # 挂15:00后成交复查(补建/补清仓; 跌停另由封单监控盘中自动处置)
            self._spawn_closing_fill_check(code, name, signal_price, action)
            log.warning("排队单已挂出(不自动撤单) %s %s queue_only",
                        action, code)
            return {"ok": False, "queue_only": True, "mode": "hotkey_f6",
                    "error": f"{action}排队单已挂出(15:00集合竞价撮合), "
                             f"不自动撤单, 收盘后自动F6复查成交"}
        if buy:
            # 窗口内未成交: 后台监控到期复查(开盘9:25-9:30挂单等5分钟,
            # 其余时段2分钟), 主线程不阻塞
            wait = self._pending_wait_seconds()
            self._spawn_pending_monitor(code, name, signal_price,
                                        action="BUY", wait_seconds=wait)
            return {"ok": False, "pending": True, "mode": "hotkey_f6",
                    "error": f"买入{cap:.0f}s未成交, 委托挂单中, "
                             f"{wait:.0f}s后自动复查(未成交撤单+告警)"}
        # 普通卖单未成交: 后台2分钟复查, 仍未成交撤单+告警
        wait = self._pending_wait_seconds()
        self._spawn_pending_monitor(code, name, signal_price,
                                    action="SELL", wait_seconds=wait)
        return {"ok": False, "pending": True, "mode": "hotkey_f6",
                "error": f"卖出{cap:.0f}s未成交, 委托挂单中, "
                         f"{wait:.0f}s后自动复查(未成交撤单+告警)"}

    def _pending_wait_seconds(self) -> float:
        """挂单到期等待: 开盘窗口(9:25-9:30, 工作日)挂单等5分钟, 其余2分钟。

        9:25集合竞价结束→9:30连续开盘是全天最活跃时段, 首轮买单多在此
        窗口挂出, 给足5分钟等成交; 其余时段2分钟不成就撤, 释放名额给
        后续价格条件补单。
        """
        now = datetime.now()
        if now.weekday() < 5 and "09:25:00" <= now.strftime("%H:%M:%S") < "09:30:00":
            return float(getattr(self.hk, "opening_pending_wait", 300))
        return float(self.hk.buy_pending_wait)

    # ---------- 主入口 ----------

    def _price_guard_check(self, code: str, action: str, name: str,
                           signal_price: float,
                           queue_only: bool = False):
        """发键前价格守卫: 现价相对信号价偏离超阈值→告警+事件(默认不拦单)。

        2026-09-15用户裁定(第一阶段): 买现价高于信号价+0.8%/卖低于-1.5%
        只WARNING推送+写price_guard事件(滑点台账), 照常发单; enforce=true
        才由调用方升级为拒单。未超阈值写exec_quality质量事件(执行滑点采集)。
        queue_only(跌停排队卖/尾盘竞价结算单)任何情况不检查。
        取价失败/信号价缺失→无法核对, 返回None(fail-open)。
        """
        hk = self.hk
        if not hk.price_guard_enable or queue_only or signal_price <= 0:
            return None
        try:
            from ths.quote import realtime_price
            send_price = realtime_price(code)
        except Exception:
            send_price = 0.0
        if not send_price or send_price <= 0:
            return None
        deviation = (send_price - signal_price) / signal_price
        buy = action.upper() == "BUY"
        bad = (deviation >= hk.price_guard_buy_pct if buy
               else deviation <= -hk.price_guard_sell_pct)
        el = get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir))
        el.log("trade", stage="price_guard" if bad else "exec_quality",
               code=code, name=name, action=action,
               signal_price=round(float(signal_price), 3),
               send_price=round(float(send_price), 3),
               deviation=round(float(deviation), 5),
               enforce=hk.price_guard_enforce or None)
        if bad:
            pct_txt = f"{deviation * 100:+.2f}%"
            threshold = (hk.price_guard_buy_pct if buy
                         else -hk.price_guard_sell_pct) * 100
            log.warning("价格守卫: %s %s 信号%.2f→现价%.2f 偏离%s(阈值%+.1f%%, "
                        "enforce=%s)", action, code, signal_price, send_price,
                        pct_txt, threshold, hk.price_guard_enforce)
            self._notify(
                f"发键前价格偏离告警 {code} {name}",
                f"{action}信号价{signal_price:.2f}, 发键前现价{send_price:.2f}"
                f"(偏离{pct_txt})。"
                + ("已超强制阈值, 本次拦单未发键。"
                   if hk.price_guard_enforce
                   else "仅告警, 照常发单(滑点已记执行质量台账)。"),
                level="WARNING")
        return {"send_price": float(send_price),
                "deviation": float(deviation), "bad": bool(bad)}

    def execute_order(self, code: str, action: str, price: float = 0.0,
                      qty: int = 0, name: str = "",
                      queue_only: bool = False,
                      closing_auction: bool = False) -> dict:
        """快捷键下单 + F6持仓验证。price/qty忽略(仓位由快捷键决定)。

        queue_only: 跌停卖单/尾盘竞价单, 挂出后不做超时自动撤单。
        closing_auction: 14:57尾盘结算单, 改发涨停价买/跌停价卖自定义键。
        窗口驱动段(bootstrap/goto/发键/F6/form兜底)全程持_win_lock,
        与挂单后台监控线程串行, 防热键发错股票/F5误撤。
        """
        # 1. 风控预检 (规则性拒绝如每日限买带risk_block标记, 决策层静默处理;
        #    queue_only结算单旁路14:55买截止/重挂上限, 但kill_switch仍拦)
        pre = self.risk.pre_check(code, action, qty, queue_only)
        if not pre.ok:
            log.warning("风控拒绝下单: %s %s -> %s", action, code, pre.reason)
            return {"ok": False, "error": pre.reason, "mode": "hotkey",
                    "risk_block": True}
        # 置位交易意图: 后台挂单监控看到后会等待, 不抢占窗口锁
        self._trading_intent.set()
        try:
            with self._win_lock:
                return self._execute_locked(code, action, price, qty, name,
                                            queue_only, closing_auction)
        finally:
            self._trading_intent.clear()

    def _execute_locked(self, code: str, action: str, price: float,
                        qty: int, name: str,
                        queue_only: bool = False,
                        closing_auction: bool = False) -> dict:
        """窗口驱动下单全流程(调用方须持_win_lock)。"""
        # 1.5 屏保进程检测 + 每日bootstrap: F12登录交易通道+关窗 (当日仅一次)
        blockers = check_screensaver()
        if blockers:
            msg = f"屏保进程{blockers}运行中会封锁键盘模拟, 请人工退出后重试"
            log.error(msg)
            return {"ok": False, "error": msg, "mode": "hotkey"}
        boot = self.daily_bootstrap()
        if not boot.get("ok"):
            log.error("每日登录bootstrap失败: %s", boot.get("error"))
            return {"ok": False,
                    "error": f"每日登录bootstrap失败: {boot.get('error')}",
                    "mode": "hotkey"}

        # 2. 连接行情端 + 快捷键面板检测(面板不显示时F1-F4无效)
        try:
            self._connect_hexin()
        except Exception as e:
            log.error("行情端连接失败: %s", e)
            return {"ok": False, "error": f"行情端连接失败: {e}",
                    "mode": "hotkey"}
        if not self._ensure_hotkey_panel():
            log.error("快捷键面板无法唤出, %s %s 转form表单兜底", action, code)
            return self._execute_form_fallback(code, action, price, qty,
                                               queue_only, closing_auction)
        if self.hk.verify_enable:
            try:
                self._connect_xiadan()
            except Exception as e:
                log.error("交易端连接失败(回查不可用): %s", e)
                return {"ok": False,
                        "error": f"交易端连接失败, 无法回查验证: {e}",
                        "mode": "hotkey"}

        # 2.5 买入前F6预检: 人工手动买入不会写positions.json, 若该股券商侧
        #     已有持仓则拒绝重复建仓(防止人工买入后系统再次买同一只)
        if action.upper() == "BUY" and self.hk.buy_precheck_f6:
            try:
                self._goto_stock(code)
                no_pos, score, det_ok = self._f6_check_position(
                    ctx="buy_precheck", code=code, action="BUY")
                if det_ok and not no_pos:
                    msg = (f"买入前F6预检: {code} {name} 券商侧已有持仓"
                           f"(疑似人工买入), 跳过重复建仓, 请人工核对")
                    log.warning(msg)
                    # 规则性拒绝(未发键不耗名额): risk_block静默不刷屏
                    return {"ok": False, "error": msg, "mode": "hotkey",
                            "risk_block": True}
                if det_ok:
                    log.info("买入前F6预检: %s 无持仓, 继续买入(score=%.2f)",
                             code, score)
                # det_ok=False(检测链路异常): 不阻断, 交由下单后F6验证兜底
            except Exception as e:
                log.warning("买入前F6预检异常(不阻断下单): %s", e)

        # 2.6 卖出前F6预检(与买入对称, 2026-09-15用户裁定): 券商侧明确无
        #     持仓则拒卖(人工已清仓再发F3是废单); 检测链路异常fail-open
        #     放行(券商端最终校验), 不做fail-closed
        if action.upper() == "SELL" and self.hk.sell_precheck_f6:
            try:
                self._goto_stock(code)
                no_pos, score, det_ok = self._f6_check_position(
                    ctx="sell_precheck", code=code, action="SELL")
                if det_ok and no_pos:
                    msg = (f"卖出前F6预检: {code} {name} 券商侧无持仓"
                           f"(疑似人工已卖出), 跳过废单卖出, 请人工核对")
                    log.warning(msg)
                    # 规则性拒绝(未发键不耗名额): risk_block静默不刷屏
                    return {"ok": False, "error": msg, "mode": "hotkey",
                            "risk_block": True}
                if det_ok:
                    log.info("卖出前F6预检: %s 有持仓, 继续卖出(score=%.2f)",
                             code, score)
                # det_ok=False: fail-open, 券商端最终校验
            except Exception as e:
                log.warning("卖出前F6预检异常(不阻断下单): %s", e)

        # 3. 下单前委托快照
        before = set()
        if self.hk.verify_enable:
            before = self._entrust_ids() or set()

        # 4. 定位股票 + 发热键
        # 下单前记录信号时价(≈信号出现时刻现价), 未成交告警附带供人工决策
        signal_price = 0.0
        try:
            from ths.quote import realtime_price
            signal_price = realtime_price(code)
            if signal_price > 0:
                log.info("信号时价 %s = %.2f", code, signal_price)
        except Exception as e:
            log.warning("信号时价查询失败 %s: %s", code, e)
        key = self._pick_key(action, qty, closing_auction)
        # 4.1 先定位股票: 定位失败时热键尚未发出, 不消耗买入名额(下轮可重试)
        try:
            self._goto_stock(code)
        except Exception as e:
            log.error("键盘精灵定位失败(未发键, 不耗买入名额) %s: %s", code, e)
            return {"ok": False, "error": f"定位股票失败(未下单): {e}",
                    "mode": "hotkey"}
        # 4.15 发键前价格守卫(默认只告警+采集滑点, enforce才拦单;
        #      queue_only结算单一律跳过): 现价相对信号价买+0.8%/卖-1.5%
        guard = self._price_guard_check(code, action, name,
                                        signal_price, queue_only)
        if guard and guard.get("bad") and self.hk.price_guard_enforce:
            msg = (f"发键前价格守卫强制拦截 {code}: 信号价{signal_price:.2f}"
                   f"→现价{guard['send_price']:.2f}"
                   f"(偏离{guard['deviation']*100:+.2f}%)")
            log.warning(msg)
            # 未发键不耗买入名额; risk_block规则性拒单, 决策层静默
            return {"ok": False, "error": msg, "mode": "hotkey",
                    "risk_block": True}
        # 4.2 发键: 此后委托可能已产生, 记挂单尝试台账(pending);
        #     成交/撤单在验证与后台监控线程里流转状态
        try:
            _tag = ("(尾盘竞价顶格)" if closing_auction
                    else "(排队单)" if queue_only else "")
            # 发键前白名单拦截(2026-09-18采纳): 非允许键不发, 记anomaly审计
            if not self._key_allowed(key):
                log.critical("键位白名单拦截: 待发键%s不在允许集合, 未发键"
                             " %s %s(不耗买入名额)", key, code, action)
                get_event_log(
                    self.cfg.resolve(self.cfg.paths.logs_dir)).log(
                    "anomaly", stage="key_whitelist_block", code=code,
                    name=name, action=action, key=self._norm_key(key))
                return {"ok": False, "risk_block": True, "mode": "hotkey",
                        "error": f"键位{key}不在白名单, 已拦截(未发键)"}
            log.info("发送快捷键 %s: %s %s%s", key, action, code, _tag)
            get_event_log(self.cfg.resolve(self.cfg.paths.logs_dir)).log(
                "trade", stage="order_sent", code=code, name=name,
                action=action, key=key.strip("{}"), mode="hotkey",
                queue_only=queue_only or None,
                closing_auction=closing_auction or None,
                signal_price=signal_price or None)
            self._hexin_win.type_keys(key, pause=0.02)
            time.sleep(0.6)              # 等下单请求发出(压缩自1.0s)
            # 下单时价: 发键后再取一次现价, 比signal_price更接近实际成交价
            # (F1市价/F2卖一价委托, 成交价≈发键瞬间价), 供二次挂单价格条件比较;
            # 守卫在发键前已成功取价则先用作初值(更贴近发键瞬间)
            order_price = guard.get("send_price") if guard else signal_price
            if not order_price:
                order_price = signal_price
            try:
                from ths.quote import realtime_price
                _p = realtime_price(code)
                if _p > 0:
                    order_price = _p
            except Exception:
                pass
            self.risk.record_attempt(code, action, order_price, "pending")
        except Exception as e:
            log.error("快捷键发送异常 %s %s: %s", action, code, e)
            self.risk.record_order(code, action, ok=False)
            return {"ok": False, "error": f"快捷键发送异常: {e}",
                    "mode": "hotkey"}

        # 5. 下单后验证(优先级: F6持仓 > xiadan回查 > 跳过)
        if self.hk.f6_verify_enable:
            result = self._verify_by_f6(code, action, name, signal_price,
                                        queue_only=queue_only)
            # 台账状态流转: 成交filled / 挂单pending / 失败unknown
            if result.get("ok"):
                self.risk.record_order(code, action, ok=True,
                                       price=order_price)
            elif result.get("pending") or result.get("queue_only"):
                self.risk.record_order(code, action, ok=False, pending=True,
                                       price=order_price)
            else:
                self.risk.record_order(code, action, ok=False)
                self.risk.set_attempt_status(code, action, "unknown")
            if result.get("pending") or result.get("queue_only"):
                log.info("挂单监控中 %s %s: %s", action, code,
                         result.get("error"))
            elif not result.get("ok"):
                log.error("F6验证未通过 %s %s: %s", action, code,
                          result.get("error"))
            return result
        if not self.hk.verify_enable:
            self.risk.record_order(code, action, ok=True, price=order_price)
            return {"ok": True, "entrust_no": "",
                    "filled_price": signal_price,
                    "status": "unverified", "mode": "hotkey"}
        ok, result = self._verify(code, action, before)
        self.risk.record_order(code, action, ok=ok, price=order_price)
        # 关闭xiadan窗口恢复热键就绪(下次回查easytrader自动重连)
        self._close_xiadan()
        if not ok:
            log.error("下单验证失败 %s %s: %s", action, code, result.get("error"))
        return result

    # ---------- 低频查询(人工诊断用; 网格读取可能触发验证码) ----------

    def get_position(self) -> list:
        try:
            self._connect_xiadan()
            return self._td_user.position or []
        except Exception as e:
            log.error("查询持仓失败: %s", e)
            return []

    def get_balance(self) -> dict:
        try:
            self._connect_xiadan()
            return self._td_user.balance or {}
        except Exception as e:
            log.error("查询资金失败: %s", e)
            return {}
