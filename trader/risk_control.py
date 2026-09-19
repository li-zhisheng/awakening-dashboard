"""交易风控: 下单前检查 + 状态记录。

检查项:
1. kill_switch (文件存在即人工全停: 买卖全拦, 只能人工删flag)
2. 同股每日买入限次 (每股每日最多买入per_stock_daily_buys次; 卖出不限——
   清仓后无仓可卖, 用户明确要求卖出不做约束)
   2a. 买侧熔断 kill_buy.flag (大盘-4%自动: 只拦BUY, SELL/撤单永不拦截,
       指数回升自动解除)——仅BUY分支检查
3. 交易时段 (工作日 9:30-11:30 / 13:00-15:00)
4. 当日下单数 (≤max_orders_per_day, 仅计买入)
5. 数量上限 (≤max_qty_per_order)

状态记录: trade_state.json (当日下单计数 + 每股当日买入次数)。
买入名额在"真实发出委托"后才消耗(屏保/连接失败等未发单的瞬时故障不
消耗, 下一轮可重试); 发单后无论成交与否都消耗(防信号反复触发重试风暴)。
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

from config import RiskConfig

log = logging.getLogger("risk")


@dataclass
class RiskResult:
    ok: bool
    reason: str = ""


class RiskController:
    """风控器: pre_check 在下单前调用 (不占手机), record_order 在下单后调用。"""

    def __init__(self, cfg: RiskConfig, project_root: str = "",
                 positions_file: str = ""):
        self.cfg = cfg
        self._root = project_root
        self._positions_file = positions_file
        self._state = {"date": "", "order_count": 0, "daily_buys": {},
                       "attempts": {}, "requeues": {}, "cids": {},
                       "sell_count": 0}
        self._load_state()

    def _resolve(self, path: str) -> str:
        if os.path.isabs(path) or not self._root:
            return path
        return os.path.join(self._root, path)

    def _load_state(self):
        path = self._resolve(self.cfg.state_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            pass
        # 日期变更则重置(旧版cooldowns时间戳键一并废弃)
        today = time.strftime("%Y-%m-%d")
        if self._state.get("date") != today:
            self._state = {"date": today, "order_count": 0, "daily_buys": {},
                           "attempts": {}, "requeues": {}, "cids": {},
                           "sell_count": 0}
            self._save_state()
        self._state.setdefault("daily_buys", {})
        self._state.setdefault("attempts", {})
        self._state.setdefault("requeues", {})   # {code: {"BUY":n,"SELL":n}}
        self._state.setdefault("cids", {})       # {code: {"BUY":seq,"SELL":seq}}
        self._state.setdefault("sell_count", 0)  # 当日卖出笔数(只告警不硬拦)

    def _event_log(self):
        """惰性获取与state_file同目录(logs)的共享事件日志(2026-09-18采纳)。"""
        try:
            from models.audit import get_event_log
            logs_dir = os.path.dirname(
                self._resolve(self.cfg.state_file)) or "."
            return get_event_log(logs_dir)
        except Exception:
            return None

    def _save_state(self):
        path = self._resolve(self.cfg.state_file)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 原子写: 进程被杀/断电瞬间直接dump可能截断文件, 下次启动
            # 计数清零→每日限买失效(tmp+replace杜绝, 2026-09-10审计修复)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            log.error("风控状态保存失败: %s", e)

    def trip_kill_switch(self, reason: str) -> bool:
        """写kill_switch熔断标记(内容含时间+原因); 已存在不覆盖。

        人工全停开关: 触发后pre_check拒绝一切下单(含卖出/撤单), 扫描照常;
        只能人工核查后删除flag恢复(程序绝不自动解除)。
        """
        path = self._resolve(self.cfg.kill_switch_file)
        if os.path.isfile(path):
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")
        except OSError as e:
            log.error("熔断开关写入失败: %s", e)
            return False
        log.critical("kill_switch已触发: %s", reason)
        return True

    # ---------- 买侧熔断(大盘-4%自动; 只禁买入, 卖出/撤单永不拦截) ----------

    def trip_buy_halt(self, reason: str, index: str = "",
                      low: float = 0.0, date: str = "") -> bool:
        """写买侧熔断标志(JSON单行, 含触发指数与熔断低点价); 已存在不覆盖。

        与kill_switch的区别: 只拦截BUY预检; SELL/F8撤单照常(风控止损不能停)。
        低点价供MarketGuard判断"回升自动解除"; date供次日自动清除。
        """
        path = self._resolve(self.cfg.buy_halt_file)
        if os.path.isfile(path):
            return False
        payload = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "reason": reason, "index": index,
                   "low": round(float(low or 0), 4),
                   "date": date or time.strftime("%Y-%m-%d")}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            log.error("买侧熔断标志写入失败: %s", e)
            return False
        log.critical("买侧熔断已触发(只禁买入, 卖出/撤单照常): %s", reason)
        return True

    def read_buy_halt(self) -> dict:
        """读买侧熔断标志, 返回payload dict; 不存在/解析失败返回{}。"""
        path = self._resolve(self.cfg.buy_halt_file)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def buy_halted(self) -> bool:
        """买侧熔断标志是否存在(只回答事实, 日期判断由调用方做)。"""
        return os.path.isfile(self._resolve(self.cfg.buy_halt_file))

    def clear_buy_halt(self) -> bool:
        """解除买侧熔断(指数回升自动恢复/次日清除); 不存在返回False。"""
        path = self._resolve(self.cfg.buy_halt_file)
        try:
            os.remove(path)
            log.warning("买侧熔断已解除, 买入恢复")
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            log.error("买侧熔断解除失败: %s", e)
            return False

    # ---------- 预检 (不占手机) ----------

    def pre_check(self, code: str, action: str, qty: int = 0,
                  queue_only: bool = False) -> RiskResult:
        """下单前风控检查。全部通过才允许下单。

        queue_only: 跌停排队卖/14:57收盘集合竞价结算兜底单, 旁路尾盘买截止
        与单票重挂上限(用户裁定: 结算单必须照常挂出), 其余门禁不变。
        """
        if not self.cfg.enable:
            return RiskResult(ok=True)
        action = action.upper()

        # 1. kill_switch
        ks = self._resolve(self.cfg.kill_switch_file)
        if os.path.isfile(ks):
            return RiskResult(False, f"kill_switch已触发 ({ks}存在)")

        # 1a. 单票每日重挂次数上限(2026-09-15用户裁定, 默认3次, 买卖分别
        #     计数, 首次挂单不计; 0=关闭): 上笔canceled/unknown后的再次
        #     主动挂单达上限即拒(规则性拒单, 决策层静默)。queue_only旁路。
        max_rq = int(self.cfg.max_requeue_per_day or 0)
        if not queue_only and max_rq > 0:
            rq = self.requeue_count(code, action)
            if rq >= max_rq:
                self._audit_requeue_cap(code, action, rq)
                return RiskResult(
                    False, f"该股今日{action}已重挂{rq}次, 达单票每日重挂"
                           f"上限{max_rq}次(首次挂单不计; 收盘结算单不受限)")

        # 2. 同股每日买入限次: 只统计已成交次数(挂单后撤单不占名额,
        #    二次挂单由决策层价格条件把关: 买≤首次挂单价);
        #    T+1卖出校验见下
        if action == "BUY":
            # 2a. 买侧熔断(大盘-4%自动): 只禁买入; SELL/F8撤单永不被拦,
            #     指数回升由MarketGuard自动解除(人工全停仍走上面的kill_switch)
            if self.buy_halted():
                return RiskResult(
                    False, f"大盘买侧熔断中 ({self._resolve(self.cfg.buy_halt_file)}"
                           f"存在): 只禁止新买入, 卖出/撤单照常, 指数回升自动解除")
            n = self.filled_buy_count(code)
            if n >= self.cfg.per_stock_daily_buys:
                return RiskResult(
                    False, f"该股今日已成交买入{n}次, 每股每日限买"
                           f"{self.cfg.per_stock_daily_buys}次")
            # 2b. 尾盘主动买入截止(2026-09-15用户裁定=14:55): 该时刻后不发
            #     新主动买单(含二次挂买); 卖单/止损/F8撤单/14:57收盘集合
            #     竞价queue_only结算单全部不受影响。空串/开关关=不启用。
            if (not queue_only and self.cfg.buy_deadline_enable
                    and self.cfg.buy_deadline):
                now = datetime.now()
                if (now.weekday() < 5
                        and now.strftime("%H:%M:%S") >= self.cfg.buy_deadline):
                    return RiskResult(
                        False, f"已过尾盘主动买入截止{self.cfg.buy_deadline}"
                               f"(卖单/止损/收盘集合竞价结算单不受影响)")
        elif action == "SELL":
            # T+1: 当日买入的股票次日才能卖出
            if self._positions_file and self._is_t0_position(code):
                return RiskResult(
                    False, f"T+1约束: {code}当日买入, 次日才能卖出"
                           f"(持仓entry_time为今日)")
            # 卖出日单量超阈只WARNING不硬拦(2026-09-18采纳, DeepSeek B6):
            # 空头必卖, 超量也不能阻塞卖出链路; 每股去重防刷屏。
            sell_n = int(self._state.get("sell_count", 0))
            if sell_n >= self.cfg.max_orders_per_day:
                self._audit_sell_budget(code, sell_n)

        # 3. 交易时段
        if not self._is_trade_time():
            return RiskResult(False, f"非交易时段 (仅工作日 {self.cfg.sessions})")

        # 4. 当日下单数: 仅限制买入(卖出是风控止损动作, 不占名额)
        if (action.upper() == "BUY"
                and self._state.get("order_count", 0) >= self.cfg.max_orders_per_day):
            return RiskResult(False,
                              f"当日下单数已达上限 {self.cfg.max_orders_per_day}")

        # 5. 数量上限
        if qty and qty > self.cfg.max_qty_per_order:
            return RiskResult(False,
                              f"数量{qty}超过单笔上限{self.cfg.max_qty_per_order}")

        return RiskResult(ok=True)

    def _is_trade_time(self) -> bool:
        """是否在交易时段内 (工作日 9:30-11:30 / 13:00-15:00)。"""
        now = datetime.now()
        if now.weekday() >= 5:  # 周六周日
            return False
        hhmm = now.strftime("%H:%M:%S")
        for start, end in self.cfg.sessions:
            if start <= hhmm <= end:
                return True
        return False

    def _is_t0_position(self, code: str) -> bool:
        """检查持仓股是否为今日买入(T+1约束: 当日买入次日才能卖)。

        读positions.json的entry_time字段, 日期==今天则T0不能卖。
        读取异常时返回False(不阻断, 让券商端做最终校验)。
        """
        path = self._positions_file
        if not path:
            return False
        if not os.path.isabs(path) and self._root:
            path = os.path.join(self._root, path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                positions = json.load(f)
        except (OSError, ValueError) as e:
            # 2026-09-18采纳: T+1读取异常写anomaly事件(fail-open语义不变)
            try:
                el = self._event_log()
                if el:
                    el.log("anomaly", stage="t1_check_error", code=code,
                            error=str(e)[:200])
            except Exception:
                pass
            return False
        today = time.strftime("%Y-%m-%d")
        for p in positions if isinstance(positions, list) else \
                positions.get("positions", []) if isinstance(positions, dict) else []:
            if p.get("code") == code:
                entry = str(p.get("entry_time", ""))
                if entry[:10] == today:
                    return True
                return False
        return False

    # ---------- 当日挂单尝试台账(二次挂单价格条件依据) ----------

    def _next_cid(self, code: str, action: str) -> str:
        """分配下一个client_order_id(每方向独立自增seq, 当日持久化)。

        格式 YYYYMMDD-code-ACTION-seq; 作为委托幂等键只增字段,
        重挂/撤单后再发是一笔新委托→新cid; 状态流转沿用原cid。
        """
        seqmap = self._state.setdefault("cids", {}).setdefault(code, {})
        seq = int(seqmap.get(action, 0)) + 1
        seqmap[action] = seq
        return f"{time.strftime('%Y%m%d')}-{code}-{action}-{seq}"

    def record_attempt(self, code: str, action: str, price: float,
                       status: str = "pending"):
        """记录一次挂单尝试。status: pending/filled/canceled/unknown。

        台账随trade_state.json持久化, 进程重启后二次挂单价格条件仍生效。
        语义(2026-09-15收紧):
        - 首挂价/首挂时间只在首次记录时写入, 重挂不覆盖(决策层"买≤首挂价/
          卖≥首挂价"依据);
        - 每次新发委托(pending)分配新cid; 上笔canceled/unknown后再挂pending
          计一次重挂(买/卖分别计数, pre_check按max_requeue_per_day拦截);
        - canceled/unknown→filled等"同一笔委托的状态补正"请走
          set_attempt_status; 本方法是"新发委托"的构造入口。
        """
        if not self.cfg.enable or not code:
            return
        action = action.upper()
        per = self._state.setdefault("attempts", {}).setdefault(code, {})
        att = per.get(action)
        if not att:
            att = {"price": round(float(price or 0), 3),
                   "ts": round(time.time(), 3), "status": status}
            per[action] = att
            if status == "pending":
                att["cid"] = self._next_cid(code, action)
        else:
            old_status = att.get("status", "")
            if status == "pending":
                if old_status in ("canceled", "unknown"):
                    rq = self._state.setdefault(
                        "requeues", {}).setdefault(code, {})
                    rq[action] = int(rq.get(action, 0)) + 1
                    log.warning("%s %s 第%d次重挂(上限%s)", code, action,
                                rq[action], self.cfg.max_requeue_per_day)
                elif old_status != "pending":
                    log.warning("%s %s 上笔台账状态=%s即再发新委托, "
                                "请核查是否重复发单", code, action, old_status)
                att["cid"] = self._next_cid(code, action)
            elif old_status not in ("pending", status):
                # 非同态、非pending新发: 同态重复(如filled->filled,
                # PaperTrader双记账)应幂等静默; 跨态覆写提示走
                # set_attempt_status(其filled终态会被硬拒)
                log.warning("%s %s 台账状态%s被record_attempt直接覆写为%s"
                            "(状态补正应走set_attempt_status), 请核查",
                            code, action, old_status, status)
            # 首挂价/首挂时间保留, 仅刷新状态(重挂价不覆盖first price)
            att["status"] = status
        if action == "BUY" and status == "filled":
            buys = self._state.setdefault("daily_buys", {})
            # 成交计数与台账保持一致(取较大值, 不回退)
            buys[code] = max(int(buys.get(code, 0)),
                             self.filled_buy_count(code))
        self._save_state()

    # 台账合法状态流转: pending可向任何结论态; canceled/unknown允许后续
    # 补正为filled(延迟成交/收盘复查/启动恢复门); filled是终态不可回退
    # (回退会凭空释放每股每日限买名额, 只能人工核查)。
    _LEGAL_TRANSITIONS = {
        "pending": {"pending", "filled", "canceled", "unknown"},
        "canceled": {"canceled", "filled"},
        "unknown": {"unknown", "filled", "canceled"},
        "filled": {"filled"},
    }

    def set_attempt_status(self, code: str, action: str, status: str) -> bool:
        """更新已有尝试状态(不存在则忽略)。非法流转拒绝并CRITICAL, 返回是否受理。"""
        if not self.cfg.enable or not code:
            return False
        action = action.upper()
        att = self._state.get("attempts", {}).get(code, {}).get(action)
        if not att:
            return False
        old_status = att.get("status", "")
        if status == old_status:
            return True
        allowed = self._LEGAL_TRANSITIONS.get(old_status, set())
        if status not in allowed:
            log.critical("非法台账状态流转被拒绝: %s %s %s->%s"
                         "(保留%s态, 请人工核查成交/撤单)",
                         code, action, old_status, status, old_status)
            return False
        if old_status == "unknown":
            # 离开unknown: 清除悬置升级标记(若再次unknown可重新计时)
            att.pop("unknown_critical_marked", None)
            att.pop("unknown_report_marked", None)
        att["status"] = status
        if action == "BUY" and status == "filled":
            buys = self._state.setdefault("daily_buys", {})
            buys[code] = max(int(buys.get(code, 0)),
                             self.filled_buy_count(code))
        self._save_state()
        return True

    def requeue_count(self, code: str, action: str) -> int:
        """当日该股该方向已重挂次数(首次挂单不计; 台账requeues)。"""
        action = action.upper()
        return int(self._state.get("requeues", {})
                   .get(code, {}).get(action, 0))

    def get_attempt(self, code: str, action: str):
        """返回 {price, ts, status} 或 None。"""
        action = action.upper()
        return self._state.get("attempts", {}).get(code, {}).get(action)

    def all_attempts(self) -> dict:
        """当日全部挂单尝试台账 {code: {"BUY":{...},"SELL":{...}}}(尾盘结算用)。"""
        return self._state.get("attempts", {})

    def filled_buy_count(self, code: str) -> int:
        """当日该股已成交买入次数(台账优先; 旧版daily_buys计数兜底)。"""
        att = self._state.get("attempts", {}).get(code, {}).get("BUY")
        if att:
            return 1 if att.get("status") == "filled" else 0
        return int(self._state.get("daily_buys", {}).get(code, 0))

    # ---------- unknown悬置超时升级(2026-09-18采纳) ----------

    def scan_unknown_stuck(self, now_ts: float = 0.0) -> list:
        """扫描unknown挂单的悬置时长, 超时升级, 返回本次新触发的事件列表。

        按attempt的ts计龄: 超unknown_stuck_critical_sec升CRITICAL,
        超unknown_stuck_report_sec再做日报标记。同一档标记一次(标记随attempt
        持久化, 离开unknown时由set_attempt_status清除)。
        返回 [{code, action, age, level, stage}, ...], 供调度器告警。
        """
        if not self.cfg.enable:
            return []
        now = float(now_ts or time.time())
        crit = float(self.cfg.unknown_stuck_critical_sec)
        rep = float(self.cfg.unknown_stuck_report_sec)
        out = []
        for code, per in self._state.get("attempts", {}).items():
            if not isinstance(per, dict):
                continue
            for action, att in per.items():
                if not isinstance(att, dict) \
                        or att.get("status") != "unknown":
                    continue
                try:
                    age = now - float(att.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if age >= rep and not att.get("unknown_report_marked"):
                    att["unknown_report_marked"] = True
                    out.append({"code": code, "action": action, "age": int(age),
                                "level": "WARNING",
                                "stage": "unknown_stuck_report"})
                if age >= crit and not att.get("unknown_critical_marked"):
                    att["unknown_critical_marked"] = True
                    out.append({"code": code, "action": action, "age": int(age),
                                "level": "CRITICAL",
                                "stage": "unknown_stuck_critical"})
        if out:
            self._save_state()
        return out

    def _audit_requeue_cap(self, code: str, action: str, rq: int):
        """重挂达限拒绝时写专门审计事件(2026-09-18采纳, 智谱A6), 每股当日一次。"""
        try:
            alerted = self._state.setdefault("requeue_cap_alerted", {})
            key = f"{code}:{action}"
            if alerted.get(key) == self._state.get("date"):
                return
            alerted[key] = self._state.get("date")
            el = self._event_log()
            if el:
                el.log("trade", stage="requeue_cap_reject", code=code,
                       action=action, requeues=rq,
                       cap=self.cfg.max_requeue_per_day)
        except Exception as e:
            log.warning("重挂达限审计写入失败: %s", e)

    def _audit_sell_budget(self, code: str, sell_n: int):
        """卖出超日单量只WARNING事件(2026-09-18采纳, DeepSeek B6), 每股当日一次。"""
        try:
            alerted = self._state.setdefault("sell_budget_alerted", {})
            if alerted.get(code) == self._state.get("date"):
                return
            alerted[code] = self._state.get("date")
            el = self._event_log()
            if el:
                el.log("trade", stage="sell_budget_warning", code=code,
                       action="SELL", sell_count=sell_n,
                       budget=self.cfg.max_orders_per_day)
            log.warning("卖出笔数%s已达日单量%s(只告警不拦卖, 空头必卖)",
                        sell_n, self.cfg.max_orders_per_day)
        except Exception as e:
            log.warning("卖出超量告警写入失败: %s", e)

    def record_order(self, code: str, action: str, ok: bool,
                     pending: bool = False, price: float = 0.0):
        """下单后更新风控状态。

        - order_count: 已成交或已挂单(pending)都计(占用当日下单名额)
        - BUY已成交: 台账filled + 每股每日买入次数+1
        - BUY仅挂单: 台账pending(不占每股买入名额, 撤单后可按价格条件重挂)
        - 卖出不记买入限制
        """
        if not self.cfg.enable:
            return
        # order_count 只统计买入(卖出为风控止损, 不占当日下单名额)
        if action.upper() == "BUY" and (ok or pending):
            self._state["order_count"] = self._state.get("order_count", 0) + 1
        # sell_count 统计已发出的卖出(成交或挂单; 2026-09-18采纳, 超量只告警)
        if action.upper() == "SELL" and (ok or pending):
            self._state["sell_count"] = int(self._state.get("sell_count", 0)) + 1
        if action.upper() == "BUY":
            if ok and not pending:
                self.record_attempt(code, "BUY", price, "filled")
            elif pending:
                # 挂单中: 仅当无台账时记pending(发键时已record_attempt则保留)
                if not self.get_attempt(code, "BUY"):
                    self.record_attempt(code, "BUY", price, "pending")
                else:
                    self._save_state()
            else:
                self._save_state()
        else:
            self._save_state()
