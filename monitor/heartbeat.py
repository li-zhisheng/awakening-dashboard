"""系统心跳 + 看门狗: 解决"程序死了/卡死了但你不知道"。

心跳(交易主进程内):
  后台线程每 heartbeat_interval 秒写 logs/heartbeat.json:
    {"ts": ..., "time": "...", "pid": ..., "phase": "扫描中", "round": 3}
  关键设计: 仅当主线程最近 heartbeat_touch_stale 秒内 touch() 过才写。
  => 进程死亡(线程消失)或主线程挂死(ADB死等/死锁)都会让文件停止更新。

看门狗(独立子进程 python main.py watchdog):
  每15s检查心跳文件, 超 heartbeat_stale 秒未更新且主进程pid仍存活
  -> 推送"程序可能已死/卡死"告警(企业微信/飞书), 同一卡死周期只报一次,
  恢复后报解除; 主进程pid已不存在(正常退出)则看门狗自行退出。
  由 auto_round 启动时以分离进程自动拉起(主进程死了它也活着)。
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time

log = logging.getLogger("heartbeat")

PID_FILE = "logs/watchdog.pid"


def pid_alive(pid: int) -> bool:
    """检测进程是否存活(Windows优先, 其他平台用os.kill探活)。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                k.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Heartbeat:
    """交易主进程心跳: 主线程touch, 后台线程落盘。"""

    def __init__(self, path: str, interval: float = 30.0,
                 touch_stale: float = 90.0):
        self.path = path
        self.interval = max(5.0, interval)
        self.touch_stale = max(10.0, touch_stale)
        self._lock = threading.Lock()
        self._last_touch = 0.0
        self._phase = "启动"
        self._extra = {}
        self._round = 0
        self._stop = threading.Event()
        self._thread = None

    # ---- 主线程调用 ----

    def touch(self, **extra):
        """主线程报告"我还活着且在干活"(每只股票扫描/每个阶段调一次)。"""
        with self._lock:
            self._last_touch = time.time()
            if extra:
                self._extra.update(extra)

    def set_phase(self, phase: str, **extra):
        with self._lock:
            self._phase = phase
            self._last_touch = time.time()
            # extra是"本阶段"上下文(如交易中trade=代码): 切换阶段必须整体
            # 替换, 否则上阶段字段(trade/codes)会残留到新阶段误导监控页
            self._extra = dict(extra)
        self._write()

    def set_round(self, rnd: int):
        with self._lock:
            self._round = rnd

    def start(self):
        self.touch()
        self._write()
        self._thread = threading.Thread(target=self._loop,
                                        name="heartbeat", daemon=True)
        self._thread.start()
        log.info("心跳启动: %s (间隔%.0fs, 主线程%0fs无动作则停跳)",
                 self.path, self.interval, self.touch_stale)

    def stop(self):
        self._stop.set()

    # ---- 内部 ----

    def _snapshot(self) -> dict:
        with self._lock:
            fresh = (time.time() - self._last_touch) <= self.touch_stale
            return {"ts": round(time.time(), 2),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "pid": os.getpid(),
                    "phase": self._phase if fresh else "主线程无响应",
                    "round": self._round,
                    "fresh": fresh,
                    **self._extra}

    def _write(self):
        try:
            data = self._snapshot()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("心跳写盘失败: %s", e)

    def _loop(self):
        while not self._stop.wait(self.interval):
            # 主线程超时未touch: 停止写心跳 -> 文件变陈旧 -> 看门狗告警
            with self._lock:
                fresh = (time.time() - self._last_touch) <= self.touch_stale
            if fresh:
                self._write()


class Watchdog:
    """看门狗进程主体(python main.py watchdog 运行)。"""

    CHECK_INTERVAL = 15.0
    REPEAT_ALERT = 600.0   # 同一卡死周期重复告警间隔(秒)
    RESTART_GRACE = 120.0  # 拉起auto_round后等新主进程心跳上线的宽限(秒)

    def __init__(self, cfg, notifier=None, config_path: str = ""):
        self.cfg = cfg
        self.notifier = notifier
        self.config_path = config_path or os.path.join(
            cfg.project_root, "config.yaml")
        self.heartbeat_path = os.path.join(cfg.project_root, "logs",
                                           "heartbeat.json")
        self.alerts_dir = os.path.join(cfg.project_root, "logs")
        self.pid_file = os.path.join(cfg.project_root, PID_FILE)
        m = cfg.monitor
        self.restart_state_path = (
            m.watchdog_restart_state
            if os.path.isabs(m.watchdog_restart_state)
            else os.path.join(cfg.project_root, m.watchdog_restart_state))

    def run(self):
        m = self.cfg.monitor
        stale_after = max(30.0, m.heartbeat_stale)
        log.info("看门狗启动: 监控 %s (超过%.0fs未更新即告警)",
                 self.heartbeat_path, stale_after)
        self._write_pid()
        alerting = False
        last_alert = 0.0
        while True:
            hb = self._read()
            now = time.time()
            if hb:
                age = now - hb.get("ts", 0)
                main_alive = pid_alive(hb.get("pid", 0))
                if age > stale_after and main_alive:
                    if not alerting or now - last_alert >= self.REPEAT_ALERT:
                        alerting = True
                        last_alert = now
                        self._alert(
                            f"心跳超时 {age:.0f}s",
                            f"交易程序可能已死或主线程卡死!\n"
                            f"pid={hb.get('pid')} phase={hb.get('phase')} "
                            f"round={hb.get('round')}\n"
                            f"最后心跳: {hb.get('time')}\n请立即人工检查PC。")
                elif alerting and age <= stale_after:
                    alerting = False
                    self._alert("心跳恢复",
                                f"交易程序心跳已恢复({hb.get('time')})。")
                if not main_alive:
                    # 裁定5: 主进程死亡 -> 带护栏自动重启, 看门狗继续值守等新心跳;
                    # 宽限期内(重启引导中)只等待不重复拉起; 护栏不满足才退出
                    now2 = time.time()
                    if self._restart_in_flight(now2):
                        log.info("已自动拉起auto_round, 宽限%.0fs内等待"
                                 "新主进程心跳上线", self.RESTART_GRACE)
                    elif self._guarded_restart(hb):
                        log.info("已带护栏自动重启auto_round, 看门狗继续值守")
                    else:
                        log.info("主进程%d已退出且不满足重启护栏, "
                                 "看门狗随之退出", hb.get("pid"))
                        self._clear_pid()
                        return
            else:
                # 无心跳文件: 主进程从未启动或已删; 不告警(由启动方负责)
                log.debug("心跳文件不存在")
            time.sleep(self.CHECK_INTERVAL)

    def _read(self) -> dict:
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    # ---------- 带护栏自动重启 (裁定5, 2026-09-15) ----------

    def _read_restart_state(self) -> dict:
        """读当日重启计数 {date,count,last_ts}, 跨天自动清零。"""
        try:
            with open(self.restart_state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == time.strftime("%Y%m%d"):
                return {"date": d["date"],
                        "count": int(d.get("count", 0)),
                        "last_ts": float(d.get("last_ts", 0.0))}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
        return {"date": time.strftime("%Y%m%d"), "count": 0, "last_ts": 0.0}

    def _write_restart_state(self, st: dict):
        try:
            os.makedirs(os.path.dirname(self.restart_state_path),
                        exist_ok=True)
            tmp = self.restart_state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
            os.replace(tmp, self.restart_state_path)
        except OSError as e:
            log.warning("看门狗重启计数写盘失败: %s", e)

    def _restart_in_flight(self, now: float) -> bool:
        """宽限期内刚拉起过: 等新主进程心跳上线, 不重复拉起。"""
        st = self._read_restart_state()
        return bool(st["last_ts"]
                    and 0 < now - st["last_ts"] < self.RESTART_GRACE)

    def _restart_time_ok(self) -> bool:
        """工作日且当前时刻早于重启截止(默认14:57, 让路尾盘结算)。"""
        lt = time.localtime()
        if lt.tm_wday >= 5:
            return False
        return time.strftime("%H:%M:%S", lt) < \
            self.cfg.monitor.watchdog_restart_deadline

    def _config_arg(self) -> str:
        override = self.cfg.monitor.watchdog_restart_config
        return self.cfg.resolve(override) if override else self.config_path

    def _guarded_restart(self, hb: dict) -> bool:
        """护栏全部通过才拉起新auto_round; 任一不满足返回False(看门狗退出)。

        护栏(用户裁定): 开关开; kill_switch人工全停期间不重启(kill_buy不禁);
        工作日14:57前; 当日重启<上限(默认3); 先跑preflight且returncode=0。
        """
        m = self.cfg.monitor
        if not m.watchdog_restart_enable:
            log.info("看门狗自动重启未启用(watchdog_restart_enable=false)")
            return False
        ks = os.path.join(self.cfg.project_root,
                          self.cfg.risk.kill_switch_file)
        if os.path.isfile(ks):
            log.warning("kill_switch存在(%s), 不自动重启auto_round", ks)
            return False
        if not self._restart_time_ok():
            log.warning("非工作日或已过重启截止%s, 不自动重启",
                        m.watchdog_restart_deadline)
            return False
        st = self._read_restart_state()
        if st["count"] >= max(0, m.watchdog_restart_max_daily):
            self._alert(
                "看门狗重启已达日上限",
                f"今日自动重启已{st['count']}次(上限"
                f"{m.watchdog_restart_max_daily}), 不再拉起, "
                f"请人工检查PC并手动启动auto_round。")
            return False
        main_py = os.path.join(self.cfg.project_root, "main.py")
        # preflight: BLOCK即放弃重启; --dry-run只抑制其自身推送,
        # 告警统一由看门狗发出避免双通道重复
        try:
            pf = subprocess.run(
                [sys.executable, main_py, "preflight",
                 "--config", self._config_arg(), "--dry-run"],
                cwd=self.cfg.project_root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300)
            if pf.returncode != 0:
                self._alert(
                    "看门狗重启中止",
                    f"preflight未通过(returncode={pf.returncode}), "
                    f"未自动拉起auto_round, 请人工检查PC。")
                return False
        except Exception as e:
            self._alert("看门狗重启中止",
                        f"preflight执行异常({e}), 未自动拉起, 请人工检查。")
            return False
        flags = 0
        if os.name == "nt":
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            subprocess.Popen(
                [sys.executable, main_py, "auto_round",
                 "--config", self._config_arg()],
                creationflags=flags, cwd=self.cfg.project_root,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
        except Exception as e:
            self._alert("看门狗重启失败",
                        f"auto_round拉起失败({e}), 请人工手动启动。")
            return False
        st["count"] += 1
        st["last_ts"] = time.time()
        self._write_restart_state(st)
        self._alert(
            "看门狗自动重启交易主进程",
            f"主进程pid={hb.get('pid')}已死亡, preflight通过, 已重新拉起"
            f"auto_round(今日第{st['count']}/{m.watchdog_restart_max_daily}次)。\n"
            f"死亡前 phase={hb.get('phase')} round={hb.get('round')} "
            f"最后心跳={hb.get('time')}\n请关注新进程心跳是否恢复。")
        return True

    def _alert(self, title: str, content: str):
        # 标题含"告警"以满足群机器人关键词过滤
        log.warning("看门狗告警: %s | %s", title, content)
        try:
            from models.audit import write_alert_file
            write_alert_file(self.alerts_dir, "HEARTBEAT", "WATCHDOG",
                             f"{title}: {content}", "watchdog")
        except Exception:
            pass
        if self.notifier and self.notifier.enabled():
            try:
                self.notifier.send(f"告警-看门狗: {title}", content,
                                   level="CRITICAL")
            except Exception as e:
                log.warning("看门狗推送失败: %s", e)

    def _write_pid(self):
        try:
            os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
            with open(self.pid_file, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except OSError as e:
            log.warning("看门狗pid写入失败: %s", e)

    def _clear_pid(self):
        try:
            os.remove(self.pid_file)
        except OSError:
            pass


def ensure_watchdog(cfg, config_path: str, logger=None) -> bool:
    """auto_round启动时调用: 确保看门狗子进程在运行(分离进程, 主进程死了也在)。

    已在运行(pid存活)则跳过; 否则以DETACHED_PROCESS拉起。返回是否新拉起。
    """
    pid_file = os.path.join(cfg.project_root, PID_FILE)
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            old = int(f.read().strip())
        if pid_alive(old):
            if logger:
                logger.info("看门狗已在运行(pid=%d), 跳过拉起", old)
            return False
    except (OSError, ValueError):
        pass
    main_py = os.path.join(cfg.project_root, "main.py")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [sys.executable, main_py, "watchdog", "--config", config_path],
            creationflags=flags, cwd=cfg.project_root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        if logger:
            logger.info("看门狗子进程已拉起(独立于主进程存活)")
        return True
    except Exception as e:
        if logger:
            logger.warning("看门狗拉起失败(不影响交易, 可手动: python main.py "
                           "watchdog): %s", e)
        return False
