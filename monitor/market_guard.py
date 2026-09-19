"""大盘系统性风控: 监控指数涨跌幅, 防系统性下跌日个股信号集体失效。

规则(配置risk段, 2026-09-12新增, 2026-09-15按用户裁定改分级):
  指数跌幅 <= market_warn_pct(-3%)  -> WARNING告警, 当日一次
  指数跌幅 <= market_crash_pct(-4%) -> 写"买侧熔断"标志(logs/kill_buy.flag):
                                       只禁止新买入; 卖出/撤单照常(风控止损
                                       永不停), CRITICAL告警
买侧熔断自动恢复:
  - 触发时记录指数与熔断低点价, 后续轮次指数从低点回升
    >=market_crash_recovery_pct(默认1%) -> 自动删flag解除买入限制并通知;
    同日再跌穿-4%可重新触发;
  - 次日自动清除残留flag(不跨天)。
人工全停: kill_switch.flag 仍是买卖全拦的最高优先级开关, 只能人工删除。
熔断后扫描照常(只记录信号); 行情走腾讯(指数sh000001), 失败降级跳过本轮,
绝不因行情故障误熔断。
"""
import logging
import time
from datetime import datetime

log = logging.getLogger("market_guard")


class MarketGuard:
    def __init__(self, cfg, risk, notifier=None):
        self.cfg = cfg
        self.risk = risk
        self.notifier = notifier
        self._warned_date = ""
        self._halt_date = ""     # 买侧熔断触发日(内存; 启动时从flag恢复)
        self._halt_idx = ""      # 触发指数代码(恢复判定跟踪同一指数)
        self._halt_low = 0.0     # 熔断瞬间指数点位(回升基准)
        self._recover_resume_ts = 0.0  # 回升首次达阈时刻(冷却防抖, 0=未进入观察)

    def _notify(self, level: str, title: str, content: str):
        log.warning("大盘风控[%s] %s | %s", level, title, content)
        if self.notifier:
            try:
                self.notifier.send(title, content, level=level)
            except Exception as e:
                log.warning("大盘风控告警推送失败: %s", e)

    def _quote_index(self, idx: str):
        """查单个指数行情快照, 失败返回None。"""
        try:
            from ths.quote import realtime_quote
            return realtime_quote(idx, 5.0)
        except Exception as e:
            log.warning("指数行情查询异常 %s: %s", idx, e)
            return None

    def _sync_halt_flag(self, today: str) -> dict:
        """与买侧熔断标志文件同步状态, 返回flag payload(无则{})。

        - 非今日flag: 自动清除(买侧熔断不跨天);
        - 今日flag且内存为空(进程重启): 恢复跟踪指数与低点。
        """
        if not self.risk:
            return {}
        flag = self.risk.read_buy_halt()
        exists = self.risk.buy_halted()
        if not exists:
            return {}
        if not flag:
            log.warning("买侧熔断标志存在但解析失败, 保持熔断等人工核查")
            self._halt_date = today
            return {}
        if flag.get("date") and flag["date"] != today:
            self.risk.clear_buy_halt()
            log.info("跨天清除昨日买侧熔断标志(原触发日%s)", flag.get("date"))
            return {}
        if self._halt_date != today:
            self._halt_date = today
            self._halt_idx = str(flag.get("index") or "")
            self._halt_low = float(flag.get("low") or 0.0)
        return flag

    def _try_recover(self, rc, today: str) -> str:
        """熔断中: 指数从低点回升达阈值则自动解除。返回recovered/recovering/halted。

        buy_halt_recover_cooldown>0时(2026-09-18采纳), 首次达阈不立即解除而
        进入观察: 期间再跌穿阈值则取消解除继续熔断, 持续站稳cooldown秒才解除。
        """
        threshold = float(getattr(rc, "market_crash_recovery_pct", 0.0) or 0.0)
        cooldown = float(getattr(rc, "buy_halt_recover_cooldown", 0.0) or 0.0)
        if threshold <= 0 or not self._halt_idx:
            return "halted"
        q = self._quote_index(self._halt_idx)
        if not q:
            return "halted"
        price = float(q.get("price") or 0)
        if self._halt_low <= 0 or price <= 0:
            return "halted"
        ratio = (price - self._halt_low) / self._halt_low
        if ratio < threshold:
            if self._recover_resume_ts:
                log.info("回升观察期内指数再跌穿阈值, 取消解除继续熔断")
                self._recover_resume_ts = 0.0
            return "halted"
        # 冷却防抖: 首次达阈进入观察, 未站稳只等
        if cooldown > 0:
            if not self._recover_resume_ts:
                self._recover_resume_ts = time.time()
                self._notify(
                    "WARNING",
                    f"【回升观察】大盘{q.get('name') or self._halt_idx}",
                    f"指数自熔断低点{self._halt_low:.2f}回升{ratio*100:.2f}%"
                    f"(现价{price:.2f})达解除阈值{threshold*100:.1f}%, "
                    f"进入{cooldown:.0f}s观察: 期间再跌穿则继续熔断, "
                    f"持续站稳才解除买入限制。")
                return "recovering"
            if time.time() - self._recover_resume_ts < cooldown:
                return "recovering"
        low0, idx0 = self._halt_low, self._halt_idx
        if self.risk:
            self.risk.clear_buy_halt()
        self._halt_date = ""
        self._halt_idx = ""
        self._halt_low = 0.0
        self._recover_resume_ts = 0.0
        self._warned_date = ""   # 解除后同日再走弱允许重新预警
        self._notify(
            "WARNING",
            f"【恢复】大盘买侧熔断解除 {q.get('name') or idx0}",
            f"指数自熔断低点{low0:.2f}回升{ratio * 100:.2f}%"
            f"(现价{price:.2f}, 阈值{threshold * 100:.1f}%), 买入限制自动"
            f"解除, 恢复正常交易(卖出/撤单在熔断期间也从未停止)。")
        log.info("买侧熔断自动解除: %s 自%.2f回升%.2f%%", idx0, low0, ratio * 100)
        return "recovered"

    def check(self) -> str:
        """每轮轮头调用。

        返回 ok/warn/halt/halted/recovered/disabled/noquote/weekend。
        监控主指数(market_index)和副指数(market_index_2, 如创业板指),
        任一指数跌幅触及阈值即告警/熔断(防持仓股所在板块系统性下跌未被
        上证指数反映)。
        """
        rc = self.cfg.risk
        if not getattr(rc, "market_guard_enable", True):
            return "disabled"
        today = time.strftime("%Y-%m-%d")
        # 先与flag文件同步(跨天清除/重启恢复), 周末也要清昨日残留
        self._sync_halt_flag(today)
        if datetime.now().weekday() >= 5:
            return "weekend"
        # 熔断中: 只做回升解除判定, 不再重复告警
        if self._halt_date == today:
            return self._try_recover(rc, today)

        indices = [rc.market_index]
        idx2 = getattr(rc, "market_index_2", "")
        if idx2:
            indices.append(idx2)

        # 取所有指数中跌幅最大者作为判定依据(任一指数熔断即触发)
        worst = None
        for idx in indices:
            q = self._quote_index(idx)
            if not q:
                continue
            pct = float(q.get("pct") or 0)
            if worst is None or pct < worst["pct"]:
                worst = {"pct": pct, "name": q.get("name") or idx,
                         "price": q.get("price") or 0, "idx": idx}
        if worst is None:
            return "noquote"

        pct = worst["pct"]
        name = worst["name"]
        price = worst["price"]
        idx_code = worst["idx"]

        if pct <= rc.market_crash_pct:
            self._halt_date = today
            self._halt_idx = idx_code
            self._halt_low = float(price or 0)
            reason = (f"大盘{name}跌幅{pct:.2f}%触及买侧熔断线"
                      f"{rc.market_crash_pct:.1f}%(现价{price:.2f})")
            tripped = (self.risk.trip_buy_halt(reason, idx_code, price, today)
                       if self.risk else False)
            if not tripped and self.risk and self.risk.buy_halted():
                tripped = True   # flag此前已存在(幂等, 内存已同步)
            done = ("已写入买侧熔断标志: 仅禁止新买入, 卖出与F8撤单照常"
                    "(风控止损不受影响), 扫描继续" if tripped
                    else "已触发但标志写入失败, 见错误日志")
            recover_hint = ""
            if getattr(rc, "market_crash_recovery_pct", 0.0):
                recover_hint = (f"指数自熔断低点{price:.2f}回升"
                                f"{rc.market_crash_recovery_pct * 100:.1f}%"
                                f"将自动解除; ")
            self._notify(
                "CRITICAL",
                f"【紧急】大盘买侧熔断 {name}{pct:.2f}%",
                f"{reason}。{done}。{recover_hint}次日自动清除; "
                f"人工全停请用logs/kill_switch.flag(买卖全拦)。")
            return "halt"

        if pct <= rc.market_warn_pct and self._warned_date != today:
            self._warned_date = today
            self._notify(
                "WARNING",
                f"【警告】大盘走弱 {name}{pct:.2f}%",
                f"大盘{name}跌幅{pct:.2f}%跌破预警线"
                f"{rc.market_warn_pct:.1f}%(现价{price:.2f})。个股信号在系统性"
                f"下跌中可靠性下降, 请关注; 继续跌至{rc.market_crash_pct:.1f}%"
                f"将触发买侧熔断(只禁买入, 卖出照常)。")
            return "warn"
        return "ok"
