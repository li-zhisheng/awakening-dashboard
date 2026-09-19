"""AutoRound 调度器: 轮驱动的完整决策闭环。

一轮流程:
  1) 热榜API拉取(失败兜底本地文件) + 与上轮diff
  2) 主轮启动: 优先续扫(页面仍在自选'>'循环页则免重进, 省~25s);
     重建轮先持仓巡检(goto随机访问)再经自选列表恢复循环上下文
  3) 主轮'>'顺序扫描(持仓股按recheck_interval插扫覆盖)
  4) 轮末: 云同步自选 + 热榜快照落盘
续扫设计: 云同步挪到轮末, 扫描停在起点后才同步, App不跳页时列表变更
对手机端行情页零打断; App被强制跳页则探测失败自动回退完整重进。
连续续扫超过RESUME_MAX_ROUNDS轮强制重建一次, 确保新增自选股进入
'>'循环列表。
"""
import logging
import time
from datetime import datetime

from adb.device import AdbError, DeviceLostError
from config import AppConfig
from decision.decision import Action, ActionType, DecisionEngine
from models.audit import SignalTracker, get_event_log, write_alert_file
from models.positions import PositionStore
from models.result import Signal, StockStatus
from scanner.scanner import StockScanner
from ths import fetcher
from ths.navigator import Navigator
from ths.watchlist import (CloudWatchlist, WatchlistAuthError,
                          WatchlistDeleteGuardError, WatchlistError)

log = logging.getLogger("scheduler")

RESUME_MAX_ROUNDS = 3   # 连续续扫上限, 超过强制重建'>'循环上下文(纳入新自选)
CLOSING_SETTLE_START = "14:57:00"   # 尾盘集合竞价开始(深市/沪市主板均14:57-15:00)
CLOSING_SETTLE_END = "15:00:00"


class AutoRoundScheduler:
    def __init__(self, cfg: AppConfig, scanner: StockScanner,
                 navigator: Navigator, decision: DecisionEngine,
                 positions: PositionStore, heartbeat=None):
        self.cfg = cfg
        self.scanner = scanner
        self.navigator = navigator
        self.decision = decision
        self.positions = positions
        self.hb = heartbeat          # monitor.heartbeat.Heartbeat, 可为None
        self._rounds_since_reenter = 0   # 距上次完整重建循环上下文的轮数
        self._round_no = 0
        self._last_pos_check_ts = 0.0    # 上次持仓巡检(含插扫/轮间)的时刻
        self._closing_done_date = ""     # 尾盘竞价结算已执行日期(每日一次)
        # 浮亏只读告警(2026-09-18采纳): {code: 已告警档集合}, 跨日清空
        self._loss_alert_date = ""
        self._loss_alerted = {}
        # 尾盘结算幂等键(settle:YYYYMMDD:code:side), 防窗口内重试重复挂单
        self._settle_keys = set()
        # 审计: 事件日志(扫描时间线/信号生命周期/覆盖率) + 卡死告警回调
        self.el = get_event_log(cfg.resolve(cfg.paths.logs_dir))
        self.signals = SignalTracker(self.el)
        scanner.observers.append(self._on_result)
        scanner.on_stuck = self._stuck_alert

    # ---------- 对外 ----------

    def run_round(self) -> tuple:
        """执行一轮, 返回 (results, summary)。"""
        round_start = time.time()
        results = []
        self._round_no += 1
        held_at_start = self.positions.codes()
        if self.hb:
            self.hb.set_round(self._round_no)
        # R1: 轮次号注入决策层, 交易事件(order_request/result/failed)携带round
        if hasattr(self.decision, "set_round"):
            self.decision.set_round(self._round_no)

        # 1) 热榜 + 交易标的过滤(主板/非ST/非次新; 前置过滤, 不可交易股不进扫描)
        if self.hb:
            self.hb.set_phase("热榜拉取")
        hot = fetcher.fetch(self.cfg.hot_list.top_n,
                            fallback_file=self.cfg.resolve(
                                self.cfg.hot_list.fallback_file))
        name_map = {s.code: s.name for s in hot}
        self.decision.name_map.update(name_map)
        from ths.universe_filter import filter_hot_stocks
        trade_codes, _, excluded = filter_hot_stocks(
            hot, self.cfg, held_codes=held_at_start)
        if excluded:
            log.info("标的过滤剔除%d只: %s", len(excluded),
                     ", ".join(f"{c}{n}({r})" for c, n, r in excluded[:12])
                     + ("..." if len(excluded) > 12 else ""))
        hot_codes = trade_codes
        log.info("热榜Top%d 过滤后可交易%d只: %s%s", len(hot), len(hot_codes),
                 hot_codes[:5], "..." if len(hot_codes) > 5 else "")
        # 本轮计划扫描清单(=云同步目标): 过滤后热榜∪轮初持仓, 覆盖率以此为准
        planned = list(dict.fromkeys(list(hot_codes) + held_at_start))

        # 2) 主轮启动: 优先续扫; 重建轮先巡检持仓再恢复上下文
        if self.hb:
            self.hb.set_phase("进入自选页")
        resumed = self._try_resume()
        if not resumed:
            # 轮头巡检(重建轮): goto随机访问; 续扫轮持仓由90s插扫覆盖
            results.extend(self._scan_positions("position_loop"))
            try:
                ok = self.scanner._with_reconnect(
                    lambda: self.navigator.enter_watchlist(""))
                if not ok:
                    log.error("进入自选页失败, 主轮尝试从当前页开始")
            except (AdbError, DeviceLostError) as e:
                log.error("进入自选页异常, 主轮从当前页开始: %s", e)
        self._rounds_since_reenter = (
            self._rounds_since_reenter + 1 if resumed else 1)

        # 3) 主轮: 从当前页沿'>'顺序扫(持仓股按间隔插扫)
        if self.hb:
            self.hb.set_phase("主轮扫描")

        def position_check(cur_code):
            self._scan_positions("interleave")
            # goto巡检丢失了自选循环上下文, 恢复到巡检前股票再续扫
            try:
                self.scanner._with_reconnect(
                    lambda: self.navigator.enter_watchlist(cur_code))
            except (AdbError, DeviceLostError) as e:
                log.error("巡检后恢复自选页失败: %s", e)

        main_results, summary = self.scanner.scan_loop(
            max_stocks=max(20, self.cfg.hot_list.top_n + 10),
            position_check=position_check if self.positions.codes() else None,
            recheck_interval=self.cfg.positions.recheck_interval,
            on_signal=self._on_signal,
            should_break=self._closing_deadline_reached,
        )
        results.extend(main_results)

        # 3.5) 扫描覆盖率审计: 实际成功扫描 / 本轮计划清单, <100%记异常
        #      (14:57为尾盘结算主动中断, 属预期行为, 不审计覆盖率)
        if not self.scanner.last_break_reason:
            self._audit_coverage(results, planned)

        # 4) 轮末: 云同步 + 热榜快照落盘
        #    14:57尾盘中断时跳过(省20s+不占用竞价窗口; 次轮/次日启动会补同步)
        if self.scanner.last_break_reason:
            log.warning("本轮因[%s]中断, 跳过云同步/快照, 让路尾盘结算",
                        self.scanner.last_break_reason)
            summary["interrupted_reason"] = self.scanner.last_break_reason
            if self.hb:
                self.hb.set_phase("尾盘竞价结算")
            return results, summary
        if self.hb:
            self.hb.set_phase("云同步")
        if self.cfg.watchlist.sync_mode == "cloud" and hot_codes:
            self._sync_watchlist(hot_codes)
        try:
            fetcher.save_local(hot, self.cfg.resolve(self.cfg.hot_list.fallback_file))
        except OSError as e:
            log.error("热榜快照保存失败: %s", e)

        elapsed = time.time() - round_start
        log.info("本轮完成: %d只(含巡检) 总耗时%.1fs 启动方式=%s",
                 len(results), elapsed, "续扫" if resumed else "重建")
        return results, summary

    def _closing_deadline_reached(self):
        """14:57尾盘结算截止点(工作日): 主轮扫描必须在此中断让路。

        与maybe_closing_settle共用同一时间窗常量; 返回原因字符串/None。
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return None
        if now.strftime("%H:%M:%S") >= CLOSING_SETTLE_START:
            return f"已到尾盘集合竞价{CLOSING_SETTLE_START[:5]}"
        return None

    # ---------- 内部 ----------

    def _try_resume(self) -> bool:
        """主轮续扫探测: 页面仍在自选'>'循环上下文则免重进(省~25s)。

        条件: 未超连续续扫上限 且 当前页日K选中且al_rightbutton存在。
        探测失败(App被同步跳页/设备异常等)自动回退完整重进, 无损。
        """
        if not (0 < self._rounds_since_reenter < RESUME_MAX_ROUNDS):
            return False
        try:
            if self.scanner._with_reconnect(self.navigator.is_in_watchlist_cycle):
                log.info("主轮续扫: 仍在自选'>'循环页, 免重进(连续第%d轮)",
                         self._rounds_since_reenter + 1)
                return True
            log.info("续扫探测未通过(页面非自选循环), 走完整重进")
        except (AdbError, DeviceLostError) as e:
            log.warning("续扫探测异常: %s", e)
        return False

    def _on_signal(self, r):
        """主轮信号回调(auto模式): 同步执行决策+交易, 扫描自然暂停。"""
        try:
            if self.hb:
                self.hb.set_phase("交易中", trade=r.stock_code)
            action = self.decision.decide(r)
            self.decision.execute(action)
        except Exception as e:
            log.error("信号回调异常 %s: %s", r.stock_code, e)
        finally:
            if self.hb:
                self.hb.touch()
                self.hb.set_phase("主轮扫描")

    def _on_result(self, idx: int, r):
        """扫描结果观察者: 每只扫描时间戳(时间线) + 信号生命周期 + 心跳touch。"""
        if self.hb:
            self.hb.touch()
        if not self.cfg.monitor.enable:
            return
        code = r.stock_code
        if not code or code == "unknown":
            return
        name = self.decision.name_map.get(code, "")
        self.el.log("scan", code=code, name=name,
                    signal=r.signal.value, status=r.status.value,
                    confidence=round(r.detection.confidence, 3),
                    elapsed=round(r.elapsed_time, 3),
                    capture_time=r.capture_time, source=r.detection.source,
                    round=self._round_no, error=r.error[:200])
        self.signals.update(code, r.signal.value, name=name,
                            confidence=r.detection.confidence,
                            source=r.detection.source,
                            scan_ts=r.capture_time)

    def _audit_coverage(self, results: list, planned: list):
        """覆盖率审计: 实扫成功数/计划数; <coverage_min(默认1.0)记异常。

        成功口径: status=OK且读出有效代码(UNKNOWN/UNKNOWN代码不计入成功,
        也不计入缺失——无法归因到具体股票; 缺失清单只列计划内未扫到的)。
        """
        if not planned or not self.cfg.monitor.enable:
            return
        scanned = {r.stock_code for r in results
                   if r.status == StockStatus.OK
                   and r.stock_code not in ("", "unknown")}
        missing = [c for c in planned if c not in scanned]
        coverage = (len(planned) - len(missing)) / len(planned)
        self.el.log("coverage", round=self._round_no, planned=len(planned),
                    scanned=len(scanned & set(planned)),
                    coverage=round(coverage, 4), missing=missing[:20],
                    missing_count=len(missing))
        if coverage < self.cfg.monitor.coverage_min:
            msg = (f"扫描覆盖率异常: {coverage:.0%} "
                   f"({len(planned) - len(missing)}/{len(planned)}), "
                   f"缺失{len(missing)}只: {missing[:10]}")
            log.warning(msg)
            write_alert_file(self.cfg.resolve(self.cfg.paths.logs_dir),
                             "COVERAGE", "SCAN", msg,
                             f"round_{self._round_no}")

    def _stuck_alert(self, cur_code: str, err: str, consec_fail: int):
        """扫描卡死告警(切页三级处置后仍失败): 手机推送+本地告警+审计事件。"""
        msg = (f"扫描卡死: {err} (连续{consec_fail}只切页失败, "
               f"单只等待上限{self.cfg.monitor.scan_timeout:.0f}s), "
               f"请检查手机/USB连接")
        log.error("%s", msg)
        self.el.log("anomaly", type="scan_stuck", cur_code=cur_code,
                    error=err, consec_fail=consec_fail,
                    round=self._round_no)
        try:
            self.decision.execute(
                Action(ActionType.ALERT, cur_code or "SCAN", "-",
                       msg, "scan_guard"))
        except Exception as e:
            log.error("卡死告警执行失败: %s", e)

    def _wl_alert(self, reason: str):
        """云同步异常告警(写alerts.jsonl + 响铃)。"""
        log.warning(reason)
        try:
            self.decision.execute(
                Action(ActionType.ALERT, "WATCHLIST", "-", reason, "cloud_sync"))
        except Exception as e:  # 告警本身失败不影响主流程
            log.error("云同步告警写入失败: %s", e)

    def _sync_watchlist(self, hot_codes: list):
        """把App"我的自选"整表同步为 热榜∪持仓, 手机端经云同步自动刷新。"""
        path = self.cfg.resolve(self.cfg.watchlist.cookie_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
        except OSError:
            self._wl_alert(f"云自选同步跳过: 未找到Cookie文件 {path}, "
                           f"请用浏览器登录10jqka.com.cn后复制Cookie写入该文件")
            return
        if not cookie:
            self._wl_alert(f"云自选同步跳过: Cookie文件为空 ({path})")
            return

        # 持仓股并入目标列表(跌出热榜也不删, 保证主轮+巡检双重覆盖)
        held = self.positions.codes()
        desired = list(dict.fromkeys(list(hot_codes) + held))
        try:
            wl = CloudWatchlist(
                cookie, timeout=self.cfg.watchlist.request_timeout,
                delete_guard_enable=self.cfg.watchlist.delete_guard_enable,
                delete_guard_max_pct=self.cfg.watchlist.delete_guard_max_pct,
                event_log=self.el)
            # 两段式二次确认参数(2026-09-18采纳, 默认关)
            wc = self.cfg.watchlist
            wl.delete_guard_confirm_enable = wc.delete_guard_confirm_enable
            wl.delete_guard_hard_pct = wc.delete_guard_hard_pct
            wl.delete_guard_confirm_wait = wc.delete_guard_confirm_wait
            wl.delete_guard_confirm_max_diff = wc.delete_guard_confirm_max_diff
            rep = wl.sync(desired)
        except WatchlistAuthError as e:
            self._wl_alert(f"同花顺Cookie失效, 云自选同步未执行(本轮按手机现有列表扫描), "
                           f"请重新登录复制Cookie: {e}")
            return
        except WatchlistDeleteGuardError as e:
            # 裁定8: 待删除占比>30%中止整表替换, CRITICAL人工确认
            # (本轮继续按手机现有列表扫描, 云端一只不删)
            log.critical("云自选删除护栏触发: %s", e)
            self.el.log("anomaly", stage="watchlist_delete_guard",
                        error=str(e)[:300], desired_n=len(desired))
            self._wl_alert(f"【CRITICAL】云自选删除护栏触发, 已中止同步且"
                           f"云端自选零删除(本轮按手机现有列表扫描), "
                           f"请人工核对Cookie账号/本地标的列表后再同步: {e}")
            return
        except WatchlistError as e:
            self._wl_alert(f"云自选同步接口异常, 本轮按手机现有列表扫描: {e}")
            return
        except Exception as e:
            log.error("云自选同步异常: %s", e)
            return

        failed = rep.get("failed_add", []) + rep.get("failed_del", [])
        log.info("云自选同步完成: 新增%d只%s 删除%d只%s 保留%d只",
                 len(rep["added"]), rep["added"][:8],
                 len(rep["removed"]), rep["removed"][:8], rep["kept"])
        if failed:
            self._wl_alert(f"云自选同步部分失败: {failed}, 请人工核对App自选列表")

    def idle_position_check(self):
        """轮间等待期持仓巡检(goto逐只, 压缩两轮之间的持仓盲区)。

        去重: 若距上次巡检(含主轮插扫)不足recheck_interval秒则跳过,
        避免主轮刚插扫完又在轮间重复巡检(浪费手机通道+多耗30-60s)。
        """
        codes = self.positions.codes()
        if not codes:
            return
        # 去重: 距上次巡检太近则跳过(主轮插扫刚跑过就不重复)
        min_gap = self.cfg.positions.recheck_interval
        elapsed = time.time() - self._last_pos_check_ts
        if self._last_pos_check_ts > 0 and elapsed < min_gap:
            log.debug("[轮间巡检] 跳过: 距上次巡检仅%.0fs(阈值%.0fs)",
                      elapsed, min_gap)
            return
        log.info("[轮间巡检] 持仓%d只: %s", len(codes), codes)
        self._scan_positions("idle")

    def _scan_positions(self, source: str):
        """goto逐只巡检持仓股, 结果实时进决策层。"""
        codes = self.positions.codes()
        if not codes:
            log.info("无持仓, 跳过持仓巡检")
            return []
        # 浮亏只读告警(HTTP现价, 不走手机通道; 2026-09-18采纳)
        self._loss_alert_check()
        log.info("持仓巡检: %d 只 %s", len(codes), codes)
        if self.hb:
            self.hb.set_phase("持仓巡检", codes=len(codes))
        results = []
        for code in codes:
            r = self.scanner.scan_one_goto(code)
            results.append(r)
            self.scanner._log_result(0, r)
            action = self.decision.decide(r)
            self.decision.execute(action)
        self._last_pos_check_ts = time.time()
        return results

    def _loss_alert_check(self):
        """持仓浮亏只读告警(2026-09-18采纳, 混元/豆包)。

        用建仓成本entry_price与HTTP现价比对(不走手机通道): 浮亏≤-10%告
        WARNING, ≤-15%告CRITICAL。只告警不自动止损(用户明确否决自动止损),
        每股每档当日只告一次(-10%先告, 继续跌到-15%再告)。
        """
        pc = self.cfg.positions
        if not pc.loss_alert_enable:
            return
        today = time.strftime("%Y%m%d")
        if self._loss_alert_date != today:
            self._loss_alert_date = today
            self._loss_alerted = {}
        from ths.quote import realtime_price
        for code in self.positions.codes():
            pos = self.positions.get(code)
            if pos is None or pos.entry_price <= 0:
                continue
            try:
                price = realtime_price(code)
            except Exception:
                continue
            if price <= 0:
                continue
            pnl = (price - pos.entry_price) / pos.entry_price
            if pnl <= pc.loss_critical_pct:
                tier = "CRITICAL"
            elif pnl <= pc.loss_warn_pct:
                tier = "WARNING"
            else:
                continue
            fired = self._loss_alerted.setdefault(code, set())
            if tier in fired:
                continue
            fired.add(tier)
            name = pos.name or self.decision.name_map.get(code, "")
            self.el.log("anomaly", stage="position_loss_alert", code=code,
                        name=name, entry_price=pos.entry_price, price=price,
                        pnl=round(pnl, 4), level=tier)
            self.decision.execute(Action(
                ActionType.ALERT, code, "",
                f"持仓浮亏{pnl*100:.1f}%(成本{pos.entry_price:.2f}→现价"
                f"{price:.2f}), 已破{'15%' if tier == 'CRITICAL' else '10%'}"
                f"只读告警线; 系统不会自动止损, 请人工决策是否卖出",
                "loss_alert"))

    # ---------- 尾盘集合竞价结算(14:57, 每日一次) ----------

    def maybe_closing_settle(self) -> bool:
        """时间窗口守卫: 工作日14:57-15:00且当日未执行时触发尾盘结算。

        主循环每轮轮头与轮间idle_sleep片(≤20s)都会调用, 保证不被
        5分钟轮间隔错过。返回是否执行了结算。
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.strftime("%H:%M:%S")
        if not (CLOSING_SETTLE_START <= hhmm < CLOSING_SETTLE_END):
            return False
        today = now.strftime("%Y-%m-%d")
        if self._closing_done_date == today:
            return False
        log.info("触发尾盘集合竞价结算(14:57)")
        if self.hb:
            self.hb.set_phase("尾盘竞价结算")
        try:
            summary = self._closing_settle()
        except Exception as e:
            # 异常不打已完成标记: 窗口内下一个20s tick还会重试
            log.error("尾盘竞价结算异常: %s", e)
            try:
                self.decision.execute(Action(
                    ActionType.ALERT, "CLOSING", "-",
                    f"尾盘竞价结算异常: {e}, 请立即人工核对未成交挂单",
                    "closing_auction"))
            except Exception:
                pass
            return False
        self._closing_done_date = today
        self.el.log("system", event="closing_settle_done",
                    summary=summary, round=self._round_no)
        return True

    def _closing_settle(self) -> dict:
        """对当日撤单(canceled)挂单做尾盘集合竞价兜底。

        卖出: 仍持仓+SELL canceled, 扫描确认信号为 空/无 → 强制queue_only
              卖(空是最高优先级; 信号转"多"说明反弹, 继续持有不卖;
              UNKNOWN绝不自动卖, 转告警)。
        买入: 未持仓+BUY canceled+剩余仓位槽位, 扫描仍为"多"且未涨停封板
              → queue_only买(信号消失不买; 涨停封板买不进, 告警由封单
              监控负责); 提交数≤max_positions-当前持仓, 防竞价同时成交超限。
        queue_only单15:00撮合, 不自动撤, 成交后由hotkey收盘复查线程补账。
        """
        risk = self.decision._risk()
        if risk is None:
            log.info("无风控台账, 跳过尾盘结算(paper测试态)")
            return {"sells": [], "buys": [], "skip": ["no_risk"]}
        attempts = risk.all_attempts()
        # 容错: 测试桩SimpleNamespace可能无该属性(真实实例在__init__创建)
        if not hasattr(self, "_settle_keys"):
            self._settle_keys = set()
        held = set(self.positions.codes())
        sell_codes = [c for c, a in attempts.items()
                      if a.get("SELL", {}).get("status") == "canceled"
                      and c in held]
        buy_codes = [c for c, a in attempts.items()
                     if a.get("BUY", {}).get("status") == "canceled"
                     and c not in held]
        slots = self.decision.max_positions - len(held)
        log.info("尾盘结算候选: 卖%d只%s 买%d只%s(剩余槽位%d)",
                 len(sell_codes), sell_codes, len(buy_codes), buy_codes, slots)

        done_sells, done_buys, skips = [], [], []

        # 1) 卖出兜底(持仓的空头未成交单)
        for code in sell_codes:
            name = self.decision.name_map.get(code, "")
            r = self.scanner.scan_one_goto(code)
            self.scanner._log_result(0, r)
            if r.status != StockStatus.OK:
                skips.append(f"{code}卖(状态{r.status.value})")
                self.decision.execute(Action(
                    ActionType.ALERT, code, r.signal.value,
                    f"尾盘结算卖出前扫描状态异常({r.status.value}), "
                    f"未自动挂竞价卖单, 请立即人工卖出", "closing_auction"))
                continue
            if r.signal == Signal.LONG:
                skips.append(f"{code}卖(信号转多,续持)")
                log.info("尾盘结算跳过卖出 %s: 信号已转多, 继续持有", code)
                continue
            if r.signal == Signal.UNKNOWN:
                skips.append(f"{code}卖(UNKNOWN)")
                self.decision.execute(Action(
                    ActionType.ALERT, code, "UNKNOWN",
                    "尾盘结算卖出前识别为UNKNOWN, 不自动卖出, 请人工决策",
                    "closing_auction"))
                continue
            # 停牌: 竞价也无法成交, 不挂废单, 告警等复牌
            try:
                from ths.quote import realtime_quote
                snap = realtime_quote(code, 5.0)
            except Exception:
                snap = None
            if snap and snap.get("halted"):
                skips.append(f"{code}卖(停牌)")
                self.decision.execute(Action(
                    ActionType.ALERT, code, "SHORT",
                    "尾盘结算: 持仓股停牌中, 竞价卖单无法成交, 未挂单; "
                    "复牌后系统将立即重判卖出", "closing_auction"))
                continue
            # SHORT 或 NONE: 空头兜底强制挂竞价卖(不撤单)
            # 幂等键: 窗口内重试不重复挂同一结算单(settle:YYYYMMDD:code:side)
            skey = f"settle:{time.strftime('%Y%m%d')}:{code}:SELL"
            if skey in self._settle_keys:
                skips.append(f"{code}卖(已挂结算单,去重)")
                continue
            act = Action(ActionType.SELL, code, r.signal.value,
                         "尾盘集合竞价兜底卖出(当日空头挂单未成交)",
                         "closing_auction", signal_ts=r.capture_time,
                         queue_only=True, closing_auction=True)
            self.decision.execute(act)
            self._settle_keys.add(skey)
            done_sells.append(f"{code}{name}")

        # 2) 买入兜底(未成交多头单, 仅在仍有空余槽位时)
        for code in buy_codes:
            if slots <= 0:
                skips.append(f"{code}买(槽位用尽)")
                break
            name = self.decision.name_map.get(code, "")
            r = self.scanner.scan_one_goto(code)
            self.scanner._log_result(0, r)
            if r.status != StockStatus.OK:
                skips.append(f"{code}买(状态{r.status.value})")
                continue
            if r.signal != Signal.LONG:
                skips.append(f"{code}买(信号已消失:{r.signal.value})")
                log.info("尾盘结算跳过买入 %s: 信号已非多(%s)", code,
                         r.signal.value)
                continue
            # 涨停封板买不进(封单监控负责开板告警人工补), 不挂竞价单
            try:
                from ths.quote import realtime_quote
                snap = realtime_quote(code, 5.0)
            except Exception:
                snap = None
            if snap and snap.get("halted"):
                skips.append(f"{code}买(停牌)")
                continue
            if snap and snap.get("at_limit_up"):
                skips.append(f"{code}买(涨停封板)")
                self.decision.execute(Action(
                    ActionType.ALERT, code, "LONG",
                    f"尾盘结算: 仍涨停封板(现价{snap.get('price')}), "
                    f"竞价买单无法成交, 未挂单; 封单监控持续到收盘",
                    "closing_auction"))
                continue
            act = Action(ActionType.BUY, code, r.signal.value,
                         "尾盘集合竞价兜底买入(当日多头挂单未成交且信号仍在)",
                         "closing_auction", signal_ts=r.capture_time,
                         queue_only=True, closing_auction=True)
            bkey = f"settle:{time.strftime('%Y%m%d')}:{code}:BUY"
            if bkey in self._settle_keys:
                skips.append(f"{code}买(已挂结算单,去重)")
                continue
            self.decision.execute(act)
            self._settle_keys.add(bkey)
            done_buys.append(f"{code}{name}")
            slots -= 1

        summary = {"sells": done_sells, "buys": done_buys, "skips": skips}
        log.info("尾盘竞价结算完成: 卖%d 买%d 跳过%d (%s)",
                 len(done_sells), len(done_buys), len(skips), summary)
        # 汇总推送一条(尾盘兜底是关键资金动作, 无论有无挂单都留痕;
        # 全空时只发INFO级别且notifier去重不会刷屏)
        if done_sells or done_buys or skips:
            lines = []
            if done_sells:
                lines.append("竞价卖出: " + ",".join(done_sells))
            if done_buys:
                lines.append("竞价买入: " + ",".join(done_buys))
            if skips:
                lines.append("跳过: " + ",".join(skips[:10]))
            lines.append("挂单15:00撮合, 不自动撤; 成交后系统自动补账并推送")
            self.decision.execute(Action(
                ActionType.ALERT, "CLOSING", "-",
                "尾盘集合竞价结算: " + "; ".join(lines), "closing_auction"))
        return summary
