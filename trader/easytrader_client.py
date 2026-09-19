"""EasytraderClient: 通过easytrader操作同花顺PC客户端完成模拟炒股下单。

依赖: pip install easytrader
前置: 同花顺PC客户端已安装并登录模拟炒股账户。
接口与 PaperTrader.execute_order 一致, 供 DecisionEngine 在 mode=auto 时调用。

easytrader API:
  user = easytrader.use('universal_client')
  user.connect(r'path/to/xiadan.exe')
  user.buy('162411', price=0.55, amount=100)
  user.sell('162411', price=0.55, amount=100)
  user.balance  -> 资金
  user.position -> 持仓列表
  user.cancel_entrust(entrust_no)
"""
import logging
import time

from config import AppConfig
from trader.risk_control import RiskController

log = logging.getLogger("easytrader")


class EasytraderClient:
    """PC端同花顺客户端下单 (替代PaperTrader)。"""

    def __init__(self, cfg: AppConfig, risk: RiskController):
        self.cfg = cfg
        self.risk = risk
        self._user = None
        self._connected = False

    def _connect(self):
        """连接同花顺客户端 (延迟到首次下单, 避免paper模式白连)。

        真机经验(2026-09-07实测):
        - UniversalClientTrader默认grid_strategy=Xls(Ctrl+S存临时文件),
          在本机会静默失败 -> 改用Copy(Ctrl+C读剪贴板)。
        - 皮肤化控件set_edit_text无效 -> enable_type_keys_for_editor()
          走select()+type_keys()通道(实测唯一可靠的输入方式)。
        - 网格读取(持仓/委托)会触发客户端"拷贝验证码"保护 -> 生产热路径
          不读网格, 持仓以positions.json为准; 读取仅供人工诊断。
        """
        if self._connected:
            return
        try:
            import easytrader
        except ImportError as e:
            raise RuntimeError(
                "未安装easytrader, 请执行: pip install easytrader") from e
        ec = self.cfg.easytrader
        if not ec.exe_path:
            raise RuntimeError(
                "easytrader.exe_path 未配置, 请在config.yaml中填写同花顺xiadan.exe路径")
        log.info("连接同花顺客户端: %s", ec.exe_path)
        self._user = easytrader.use(ec.client_type)
        try:
            import easytrader.grid_strategies as _gs
            self._user.grid_strategy = _gs.Copy
        except Exception:
            pass
        self._user.enable_type_keys_for_editor()
        if ec.tesseract_cmd:
            import easytrader.config as ec_cfg
            ec_cfg.global_config_path  # 占位: easytrader内部OCR配置可能变
        self._user.connect(ec.exe_path, timeout=ec.connect_timeout)
        # top_window()会解析到隐藏IE辅助窗口, 导致左侧菜单/表单全部失效
        # -> 显式定位交易主窗口 (2026-09-08实测)
        main_win = self._user.app.window(title="网上股票交易系统5.0")
        self._user._main = main_win
        self._user.top_window = lambda: main_win
        self._connected = True
        log.info("同花顺客户端连接成功")

    def _sweep_popups(self):
        """异常后清扫残留弹窗(确认框30s超时自动取消, 但可能阻塞下一次下单)。"""
        try:
            top = self._user.app.top_window()
            for w in top.descendants(class_name="Button"):
                t = w.window_text() or ""
                if w.is_visible() and any(k in t for k in ("否", "取消", "确定")):
                    w.click()
                    time.sleep(0.3)
                    return
        except Exception:
            pass

    def execute_order(self, code: str, action: str, price: float = 0.0,
                      qty: int = 0, name: str = "",
                      queue_only: bool = False,
                      closing_auction: bool = False) -> dict:
        """完整下单: 风控→下单→读回执。

        返回 {ok, filled_price, entrust_no, mode, error}。
        成功: ok=True; 失败: ok=False + error。
        queue_only: 跌停排队/尾盘竞价单(form兜底通道仅透传记台账, 不自动撤)。
        closing_auction: 尾盘顶格结算单(form兜底通道不区分键位, 仅透传记台账)。
        """
        # 1. 风控预检 (PC侧, 不占手机; queue_only结算单旁路尾盘买截止/重挂上限)
        pre = self.risk.pre_check(code, action, qty, queue_only)
        if not pre.ok:
            log.warning("风控拒绝下单: %s %s -> %s", action, code, pre.reason)
            # risk_block: 规则性拒绝(限买/时段/kill_switch), 决策层据此静默,
            # 防form兜底通道信号持续时每轮ALERT刷屏(2026-09-10审计修复)
            return {"ok": False, "error": pre.reason, "mode": "easytrader",
                    "risk_block": True}

        # 2. 连接客户端
        try:
            self._connect()
        except Exception as e:
            log.error("客户端连接失败: %s", e)
            return {"ok": False, "error": f"客户端连接失败: {e}",
                    "mode": "easytrader"}

        # 3. 下单 (price<=0走市价委托; easytrader的buy不自动分流市价)
        amount = qty or self.cfg.risk.default_qty
        try:
            if action.upper() == "BUY":
                if price and price > 0:
                    r = self._user.buy(code, price=price, amount=amount)
                else:
                    r = self._user.market_buy(code, amount=amount)
            elif action.upper() == "SELL":
                if price and price > 0:
                    r = self._user.sell(code, price=price, amount=amount)
                else:
                    r = self._user.market_sell(code, amount=amount)
            else:
                return {"ok": False, "error": f"未知action: {action}",
                        "mode": "easytrader"}
        except Exception as e:
            log.error("下单异常 %s %s: %s", action, code, e)
            self._sweep_popups()
            self.risk.record_order(code, action, ok=False)
            return {"ok": False, "error": f"下单异常: {e}", "mode": "easytrader"}

        # 4. 读回执
        entrust_no = ""
        if isinstance(r, dict):
            entrust_no = str(r.get("entrust_no", "") or r.get("entrust_nbr", "")
                            or "")
        ok = bool(entrust_no)
        log.info("下单结果: %s %s qty=%s entrust=%s ok=%s",
                 action, code, amount, entrust_no, ok)
        self.risk.record_order(code, action, ok=ok)
        return {"ok": ok, "filled_price": price, "entrust_no": entrust_no,
                "mode": "easytrader"}

    def get_position(self) -> list:
        """查询当前持仓。"""
        try:
            self._connect()
            return self._user.position or []
        except Exception as e:
            log.error("查询持仓失败: %s", e)
            return []

    def get_balance(self) -> dict:
        """查询资金。"""
        try:
            self._connect()
            return self._user.balance or {}
        except Exception as e:
            log.error("查询资金失败: %s", e)
            return {}
