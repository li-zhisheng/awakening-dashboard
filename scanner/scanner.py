"""扫描编排。

两种扫描模式:
- scan_loop (推荐): 从当前页任意股票开始, 随'>'按钮遍历, 起点代码第二次出现即一轮结束
- scan(codes): 按 stocks.txt 列表顺序扫描 (代码仅作预期校验, 实际以页面读到的为准)

管线 (切换与识别共用同一批截图, 不做多余截图):
  tap ">" -> 等动画期 -> 轮询截图读码(15ms/次):
    代码未变 -> 继续等
    代码变新 -> 0.3s后第二帧确认代码一致 -> 该稳定帧直接做信号识别
"""
import logging
import random
import time
from typing import List, Optional

from adb.device import AdbError, Device, DeviceLostError
from config import AppConfig
from detector.signal import SignalDetector
from models.result import (Signal, StockResult, StockStatus, build_summary)
from scanner.first_signal import FirstSignalRecorder
from ths.navigator import Navigator
from ths.page_detector import PageDetector

log = logging.getLogger("scanner")


class StockScanner:
    def __init__(self, cfg: AppConfig, device: Device, control, shot,
                 detector: SignalDetector, navigator: Navigator,
                 page_detector: PageDetector, code_reader):
        self.cfg = cfg
        self.device = device
        self.control = control
        self.shot = shot
        self.detector = detector
        self.navigator = navigator
        self.page_detector = page_detector
        self.code_reader = code_reader
        self.observers = []   # 每只结果回调 fn(idx, StockResult): 审计时间线/心跳
        self.on_stuck = None  # 切页卡死告警回调 fn(cur_code, err, consec_fail)
        self.first_signal = FirstSignalRecorder(cfg)  # 当日首次多/空快照
        # 设备断连告警回调 fn(reason:str)->None (无人值守重连超时/开始等待)
        self.on_device_lost = None

    # ---------- 对外入口 ----------

    def scan_current(self):
        """测试模式: 只检测当前页面股票, 不切换。"""
        r = self._capture_and_detect("", None, "")
        return [r], build_summary([r], r.elapsed_time)

    def scan_loop(self, max_stocks: int, position_check=None,
                  recheck_interval: float = 90.0, on_signal=None,
                  should_break=None):
        """循环模式: 从当前页股票开始, 起点第二次出现即一轮结束。

        position_check: 持仓巡检回调, 签名 fn(cur_code)->None;
        巡检(goto随机访问)会丢失自选循环上下文, 回调内需自行恢复到cur_code;
        主轮中距上次巡检超过 recheck_interval 秒时, 在下一只切换前抢占执行。

        on_signal: 主轮信号回调, 签名 fn(StockResult)->None;
        检测到多/空信号时同步调用 (auto模式触发决策+交易, 扫描自然暂停)。

        should_break: fn()->str(原因)/None; 每只切换前检查, 返回原因即中断
        本轮(用于14:57尾盘集合竞价结算, 保证结算有完整3分钟窗口)。
        中断原因可读 self.last_break_reason。
        """
        scan_start = time.time()
        results: List[StockResult] = []
        last_pos_check = time.time()
        consec_fail = 0     # 连续切页失败计数(熔断用)
        stuck_alerted = False  # 本轮卡死告警只发一次(恢复成功即复位)
        self.last_break_reason = ""

        r = self._with_reconnect(lambda: self._capture_and_detect("", None, ""))
        results.append(r)
        start_code = r.stock_code if r.stock_code != "unknown" else ""
        seen = {start_code} if start_code else set()
        # 首帧识别失败(unknown): 用UI dump兜底取页面代码加入seen, 否则回环
        # 判定(start_code非空)永远不生效, 每轮会多扫一圈直到seen重复才结束
        if not start_code:
            try:
                xml = self.control.dump_ui()
                actual, _ = self.navigator.extract_info(xml)
                if actual:
                    seen.add(actual)
                    log.info("首帧unknown, UI dump兜底起点代码: %s", actual)
            except Exception:
                pass
        cur_code = start_code
        log.info("循环扫描起点: %s, 一轮上限 %d 只", start_code or "未知", max_stocks)
        self._log_result(1, r)

        # 首帧也可能触发信号 (auto模式)
        if on_signal and r.status == StockStatus.OK and r.signal in (Signal.LONG, Signal.SHORT):
            on_signal(r)

        while len(results) < max_stocks:
            # 外部中断点(14:57尾盘结算): 在任何切换/插扫之前生效,
            # 保证goto重扫+挂竞价单赶在14:57-15:00窗口内
            if should_break:
                try:
                    reason = should_break()
                except Exception as e:
                    log.warning("should_break回调异常: %s", e)
                    reason = None
                if reason:
                    self.last_break_reason = str(reason)
                    log.warning("主轮扫描被外部中断(已扫%d只): %s",
                                len(results), reason)
                    break

            # 持仓插扫: 持仓股优先级高于主轮, 在下一只切换前抢占执行
            if position_check and time.time() - last_pos_check >= recheck_interval:
                log.info("主轮插入持仓巡检 (距上次%.0fs)",
                         time.time() - last_pos_check)
                position_check(cur_code)
                last_pos_check = time.time()

            code, img, switch_time, err = self._with_reconnect(
                lambda: self._advance(cur_code))

            # 切页三级处置(重试/重建扫描页)后仍失败: 卡死告警 + 记录UNKNOWN +
            # 连续失败熔断(防页面真死时整轮空转), 不无限等待
            if not code and err:
                consec_fail += 1
                r = self._with_reconnect(
                    lambda: self._detect_on_frame("", None, None, err, ""))
                results.append(r)
                self._log_result(len(results), r)
                if not stuck_alerted:
                    stuck_alerted = True
                    log.error("扫描卡死: %s (本轮连续%d只切页失败)", err,
                              consec_fail)
                    if self.on_stuck:
                        try:
                            self.on_stuck(cur_code, err, consec_fail)
                        except Exception as e:
                            log.warning("卡死告警回调异常: %s", e)
                if consec_fail >= max(1, self.cfg.monitor.stuck_abort):
                    # R5: stuck_policy=skip 时不中止整轮, 跳过当前卡死股续扫
                    # (默认 abort 维持原熔断语义, config 可回滚)
                    if getattr(self.cfg.monitor, "stuck_policy",
                               "abort") == "skip":
                        log.error("连续%d只切页失败, stuck_policy=skip, "
                                  "跳过该股续扫(已扫%d只)",
                                  consec_fail, len(results))
                    else:
                        log.error("连续%d只切页失败, 中止本轮扫描(已扫%d只)",
                                  consec_fail, len(results))
                        break
                continue
            consec_fail = 0
            stuck_alerted = False

            # 回到起点 = 一轮结束, 该帧属于下一轮, 不计入本次结果
            if code and start_code and code == start_code:
                log.info("已回到起点 %s, 一轮扫描结束 (本轮%d只)", start_code, len(results))
                break

            r = self._with_reconnect(
                lambda: self._detect_on_frame(code, img, switch_time, err, ""))
            results.append(r)
            self._log_result(len(results), r)

            # 首帧识别失败(起点unknown): 以第一个有效识别码补设本轮起点,
            # 否则回环判定(L117 start_code非空)永远不生效, 每轮会多扫
            # 一圈直到"seen重复"才结束(重复计数/信号可能重复触发)
            if (not start_code and r.stock_code
                    and r.stock_code != "unknown"):
                start_code = r.stock_code
                seen.add(start_code)
                log.info("以首个有效识别码 %s 补设本轮起点", start_code)

            # 信号触发: auto模式同步执行决策+交易 (扫描自然暂停, PC端交易3-5s)
            if on_signal and r.status == StockStatus.OK and r.signal in (Signal.LONG, Signal.SHORT):
                on_signal(r)

            if code and code in seen:
                log.warning("股票 %s 在一轮内重复出现, 判定循环结束 (共%d只)",
                            code, len(results))
                break
            if code:
                seen.add(code)
            cur_code = code or cur_code

        return results, build_summary(results, time.time() - scan_start)

    def scan(self, codes: List[str]):
        """文件顺序模式: 当前页作为第一只, 之后逐一切换, 实际代码以页面为准。"""
        scan_start = time.time()
        results: List[StockResult] = []

        r = self._with_reconnect(
            lambda: self._capture_and_detect(codes[0], None, ""))
        results.append(r)
        cur_code = r.stock_code if r.stock_code != "unknown" else ""
        self._log_result(1, r)

        for idx in range(1, len(codes)):
            code, img, switch_time, err = self._with_reconnect(
                lambda: self._advance(cur_code))
            r = self._with_reconnect(
                lambda: self._detect_on_frame(code, img, switch_time, err, codes[idx]))
            results.append(r)
            cur_code = r.stock_code if r.stock_code != "unknown" else cur_code
            self._log_result(idx + 1, r)

        return results, build_summary(results, time.time() - scan_start)

    def scan_one_goto(self, code: str) -> StockResult:
        """goto随机访问单只并识别 (持仓巡检用, 每只约6s)。"""
        goto_err = ""
        try:
            self._with_reconnect(lambda: self.navigator.goto(code))
        except AdbError as e:
            goto_err = f"goto({code})失败: {e}"
        return self._with_reconnect(
            lambda: self._capture_and_detect(code, None, goto_err))

    # ---------- 内部流程 ----------

    def _with_reconnect(self, fn):
        """执行 fn; 设备断开时暂停等待重连后重试, 不产生重复记录。"""
        while True:
            try:
                return fn()
            except DeviceLostError as e:
                log.error("设备问题: %s", e)
                self._wait_reconnect()

    def _capture_and_detect(self, expected: str, switch_time: Optional[float],
                            switch_error: str) -> StockResult:
        """截图一帧并完成读码+识别 (test_current / 每轮首只使用)。"""
        img = self.shot.capture()
        capture_time = time.time()
        return self._detect_on_frame("", img, switch_time or capture_time,
                                     switch_error, expected, capture_time=capture_time)

    def _advance(self, old_code: str):
        """点击'>'并轮询直到新股票代码出现且两帧一致。

        卡死处置(单次等待上限 monitor.scan_timeout 秒, 默认5s, 不无限等待):
          第1次超时 -> 原样重试1次
          第2次超时 -> 重建扫描页上下文(enter_watchlist)后再试
          第3次超时 -> 返回失败, 由scan_loop告警+记录+熔断
        返回 (code, 稳定帧图像, 切换完成时间, 错误信息)。
        确认切换所用的帧直接交给识别, 不额外截图。
        """
        sw = self.cfg.switch
        max_retries = max(1, sw.max_switch_retries)
        scan_timeout = max(2.0, self.cfg.monitor.scan_timeout)
        for attempt in range(1, max_retries + 1):
            if attempt == 2:
                log.warning("切页%.0fs超时, 第1次重试", scan_timeout)
            elif attempt == 3:
                self._rebuild_scan_page(old_code)
            time.sleep(random.uniform(sw.jitter_min, sw.jitter_max))  # 轻微随机化
            self.navigator.tap_next()
            time.sleep(sw.post_tap_delay)  # 跳过切换动画期

            deadline = time.time() + scan_timeout
            pending = None  # 读到过新代码但帧间未确认: (code, img)
            while time.time() < deadline:
                try:
                    img = self.shot.capture()
                except AdbError:
                    time.sleep(sw.poll_interval)
                    continue
                code, _ = self.code_reader.read(img)
                if code and code != old_code:
                    # 新代码出现, 第二帧确认 (防过渡动画帧)
                    time.sleep(sw.confirm_gap)
                    try:
                        img2 = self.shot.capture()
                    except AdbError:
                        time.sleep(sw.poll_interval)
                        continue
                    code2, _ = self.code_reader.read(img2)
                    if code2 == code:
                        return code, img2, time.time(), ""
                    if code2 and code2 != old_code:
                        pending = (code2, img2)  # 仍在新页, 继续等稳定
                time.sleep(sw.poll_interval)

            # 本轮超时: 若曾读到新代码, 用一次 UI dump 兜底确认
            if pending:
                code, img = pending
                try:
                    xml = self.control.dump_ui()
                    actual, _ = self.navigator.extract_info(xml)
                    if actual and actual != old_code:
                        return actual, img, time.time(), "切换经UI dump兜底确认"
                except AdbError:
                    pass
            log.warning("第%d次切换后未能确认新股票页面", attempt)

        return "", None, None, "切换失败: 重试+重建扫描页后仍未确认新页面"

    def _rebuild_scan_page(self, cur_code: str):
        """卡死处置第2级: 重新进入自选扫描页, 恢复'>'循环上下文后再试。"""
        log.warning("连续切页失败, 重建扫描页上下文(enter_watchlist)后重试")
        try:
            self._with_reconnect(
                lambda: self.navigator.enter_watchlist(cur_code or ""))
            self._stability_event("scan_page_rebuilt", code=cur_code or "")
        except (AdbError, DeviceLostError) as e:
            log.error("重建扫描页失败: %s", e)
            self._stability_event("scan_page_rebuild_failed",
                                  code=cur_code or "", error=str(e)[:200])

    def _detect_on_frame(self, code: str, img, switch_time: Optional[float],
                         switch_error: str, expected: str,
                         capture_time: Optional[float] = None) -> StockResult:
        """在已截取的帧上完成读码兜底+信号识别, 组装 StockResult。"""
        r = StockResult()
        r.start_time = switch_time or time.time()
        r.switch_complete_time = switch_time or r.start_time
        r.capture_time = capture_time or time.time()
        notes = [switch_error] if switch_error else []
        xml = None
        try:
            self.device.ensure_connected()
            if img is None:
                raise AdbError("无可用页面帧")

            if code:
                r.stock_code = code
            else:
                # 帧上无代码: 本帧读码重试 + UI dump 兜底
                read_code, cr_notes = self.code_reader.read(img)
                if read_code:
                    r.stock_code = read_code
                else:
                    notes.append(f"数字读码失败({cr_notes}), 回退UI dump")
                    try:
                        xml = self.control.dump_ui()
                    except AdbError:
                        xml = ""
                    actual, name = self.navigator.extract_info(xml)
                    if actual:
                        r.stock_code = actual
                    elif name:
                        notes.append(f"页面名称[{name}]不在股票池映射中")
                    else:
                        notes.append("无法读取股票代码")

            if expected and r.stock_code not in ("", "unknown") \
                    and r.stock_code != expected:
                notes.append(f"代码不匹配: 预期{expected}, 实际{r.stock_code}")

            sig, info = self.detector.detect(r.stock_code or "unknown", img, xml=xml)
            if r.stock_code in ("", "unknown"):
                # 页面异常: 连代码都读不出, 无论视觉结果如何都必须记未知
                # (UNKNOWN 不允许自动降级为 NONE)
                sig = Signal.UNKNOWN
                info.state = "page_error"
                notes.append("页面异常: 无法识别股票代码, 强制记未知")
            r.signal = sig
            r.detection = info
            r.status = (StockStatus.UNKNOWN if switch_error
                        else StockStatus.OK
                        if sig in (Signal.LONG, Signal.SHORT, Signal.NONE)
                        else StockStatus.UNKNOWN)
        except DeviceLostError as e:
            r.status = StockStatus.DEVICE_LOST
            r.signal = Signal.UNKNOWN
            notes.append(str(e))
        except AdbError as e:
            r.status = StockStatus.UNKNOWN
            r.signal = Signal.UNKNOWN
            notes.append(str(e))
        except Exception as e:  # 兜底: 单只失败不允许崩溃整个扫描
            r.status = StockStatus.UNKNOWN
            r.signal = Signal.UNKNOWN
            notes.append(f"{type(e).__name__}: {e}")
        finally:
            if not r.stock_code:
                r.stock_code = "unknown"
            r.error = "; ".join(x for x in notes if x)
            r.detect_complete_time = time.time()
            r.finalize()
            self.first_signal.record(r)   # 首次多/空快照(内部自兜底)
        return r

    def _log_result(self, idx: int, r: StockResult):
        log.info("[%d] %s -> %s (%s) %.2fs %s",
                 idx, r.stock_code, r.signal.label, r.status.value,
                 r.elapsed_time, r.error or "")
        # 观察者: 审计时间线(每只扫描时间戳)/心跳touch等, 单个异常不影响扫描
        for fn in self.observers:
            try:
                fn(idx, r)
            except Exception as e:
                log.warning("扫描结果观察者异常: %s", e)

    # 无人值守(无控制台/auto_round后台运行)等设备重连上限: 超时不再空转,
    # 抛DeviceLostError让本轮失败退出, 由看门狗/人工介入(2026-09-10审计修复:
    # 原EOFError分支while True+sleep永久阻塞, 设备掉线后系统静默挂死)
    RECONNECT_UNATTENDED_TIMEOUT = 600

    def _notify_lost(self, msg: str):
        log.error(msg)
        try:
            if self.on_device_lost:
                self.on_device_lost(msg)
        except Exception as e:
            log.warning("设备断连告警回调异常: %s", e)

    def _stability_event(self, etype: str, **fields):
        """稳定性计数事件(ADB重连/页面重建等): 落events JSONL供盘中
        稳定性统计(异常次数/恢复次数), 纯记录不影响任何流程。"""
        try:
            from models.audit import get_event_log
            logs_dir = self.cfg.resolve(self.cfg.paths.logs_dir)
            get_event_log(logs_dir).log("anomaly", type=etype, **fields)
        except Exception as e:
            log.debug("稳定性事件写入失败: %s", e)

    def _wait_reconnect(self):
        log.error("Android 设备已断开, 扫描暂停")
        lost_at = time.time()
        self._stability_event("device_lost")
        while True:
            try:
                input("请重新连接设备并确认USB调试授权, 然后按回车继续...")
            except EOFError:
                # 无控制台(后台/重定向stdout): 有界轮询, 绝不永久阻塞
                self._notify_lost(
                    "Android设备已断开(无人值守模式), 开始等待重连, "
                    f"{self.RECONNECT_UNATTENDED_TIMEOUT}s超时将终止本轮")
                deadline = time.time() + self.RECONNECT_UNATTENDED_TIMEOUT
                while time.time() < deadline:
                    time.sleep(15)
                    if self.device.wait_until_connected(timeout=15):
                        log.info("设备已重新连接, 继续扫描")
                        self._stability_event(
                            "device_recovered",
                            downtime_s=round(time.time() - lost_at, 1))
                        return
                self._notify_lost(
                    f"Android设备断开{self.RECONNECT_UNATTENDED_TIMEOUT}s"
                    "未恢复, 终止本轮扫描, 请人工检查")
                self._stability_event(
                    "device_reconnect_timeout",
                    downtime_s=round(time.time() - lost_at, 1))
                raise DeviceLostError(
                    f"设备断开后{self.RECONNECT_UNATTENDED_TIMEOUT}s内未重连")
            if self.device.wait_until_connected(timeout=15):
                log.info("设备已重新连接, 继续扫描")
                self._stability_event(
                    "device_recovered",
                    downtime_s=round(time.time() - lost_at, 1))
                return
            print("仍未检测到设备, 请检查连接后重试")
