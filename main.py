"""同花顺"多空趋势"信号采集器 MVP 入口。

用法:
    python main.py test_current   # 只检测当前页面股票, 不切换
    python main.py scan_loop      # 从当前页股票开始循环扫描, 回到起点即一轮结束(推荐)
    python main.py scan_10        # 扫描 stocks.txt 前10只
    python main.py scan_100       # 扫描前100只
    python main.py scan_all       # 扫描全部
    python main.py auto_round     # 自动轮: 热榜->云同步自选->持仓巡检->主轮插扫->决策闭环
    python main.py wl_check       # 诊断云自选Cookie: 列出云端"我的自选"(不需要手机)
    python main.py manual_audit   # 人工买卖后主动对账: 校验券商当日成交/持仓,
                                  # 确认后同步positions.json (--side buy/sell/both)
    python main.py web            # 可视化监控台: http://127.0.0.1:8899 (--port改端口)
"""
import argparse
import json
import logging
import os
import sys
import time

from adb.control import UIController
from adb.device import Device, list_devices
from adb.screenshot import Screenshotter
from config import load_config
from detector.code_reader import CodeReader
from detector.signal import SignalDetector
from detector.ui_detector import UIHierarchyDetector
from detector.vision_detector import VisionDetector
from scanner.scanner import StockScanner
from scanner.timing import format_summary, write_results_csv
from ths.navigator import Navigator
from ths.page_detector import PageDetector


def _read_cookie(path: str) -> str:
    """读取 Cookie 文件内容, 失败返回空串。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""

LIMITS = {"scan_10": 10, "scan_100": 100}


LOG_MAX_BYTES = 10 * 1024 * 1024   # 单个日志10MB后轮转(失控保护, 正常一天远小于此)
LOG_BACKUP_COUNT = 3               # 保留 scan_*.log.1/.2/.3 共4份(约40MB上限)


def setup_logging(logs_dir: str) -> str:
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"scan_{time.strftime('%Y%m%d_%H%M%S')}.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES,
                             backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    return log_path


def load_stocks(path: str):
    """每行格式: '代码' 或 '代码 名称'。返回 (codes, name_to_code)。"""
    codes, name_to_code = [], {}
    if not os.path.isfile(path):
        return codes, name_to_code
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").replace("\t", " ").split()
            code = parts[0]
            codes.append(code)
            if len(parts) >= 2:
                name_to_code[parts[1]] = code
    return codes, name_to_code


def apply_adb_path(adb_path: str):
    """config.device.adb_path 支持绝对路径, 注入本进程 PATH 以便 subprocess 调用。"""
    if not adb_path:
        return
    exe = adb_path if adb_path.lower().endswith(".exe") else os.path.join(adb_path, "adb.exe")
    if os.path.isfile(exe):
        os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ.get("PATH", "")


def main():
    parser = argparse.ArgumentParser(description="同花顺多空趋势信号采集器 MVP")
    parser.add_argument("mode",
                        choices=["test_current", "scan_loop", "scan_10",
                                 "scan_100", "scan_all", "auto_round",
                                 "wl_check", "cleanup", "preflight",
                                 "watchdog", "manual_audit", "web",
                                 "selftest"])
    parser.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    parser.add_argument("--limit", type=int, default=0, help="覆盖扫描数量(调试用)")
    parser.add_argument("--rounds", type=int, default=0,
                        help="auto_round轮数上限(0=无限循环)")
    parser.add_argument("--dry-run", action="store_true",
                        help="cleanup模式: 只统计不删除")
    parser.add_argument("--side", default="both",
                        choices=["buy", "sell", "both"],
                        help="manual_audit模式: 对账方向(人工买入buy/卖出sell)")
    parser.add_argument("--port", type=int, default=8899,
                        help="web模式: 监控台端口(仅绑定127.0.0.1)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # Schema分级strict门禁(2026-09-18采纳): 仅schema.strict_enable开启时生效,
    # risk/seal/timing段存在配置校验违规则拒绝启动(普通校验仍只WARNING)。
    try:
        from config import strict_violations
        sv = strict_violations(cfg)
    except Exception:
        sv = []
    if sv:
        print("=" * 60)
        print("配置严格校验(schema.strict_enable)未通过, 拒绝启动:")
        for m in sv:
            print(f"  - {m}")
        print("请修正上述配置, 或将schema.strict_enable置为false(普通模式只告警)。")
        print("=" * 60)
        sys.exit(2)
    try:
        # 行情时钟守卫(只告警): 阈值/开关随config, 注入双源行情模块单例
        from monitor.clock_guard import ClockGuard
        from ths.quote import set_clock_guard
        set_clock_guard(ClockGuard(
            cfg.monitor.clock_skew_warn_sec,
            cfg.monitor.clock_stale_warn_sec,
            cfg.monitor.clock_guard_enable))
    except Exception:
        pass
    apply_adb_path(cfg.device.adb_path)
    logs_dir = cfg.resolve(cfg.paths.logs_dir)
    os.makedirs(cfg.resolve(cfg.paths.screenshots_dir), exist_ok=True)
    os.makedirs(cfg.resolve(cfg.vision.templates_dir), exist_ok=True)
    log_path = setup_logging(logs_dir)
    log = logging.getLogger("main")
    log.info("日志文件: %s", log_path)

    if args.mode == "watchdog":
        # 看门狗(独立进程): 监控心跳文件, 主程序死/卡死超阈值推送告警
        from monitor.heartbeat import Watchdog
        from notify.notifier import Notifier
        Watchdog(cfg, notifier=Notifier(cfg),
                 config_path=args.config).run()
        return

    if args.mode == "cleanup":
        # 垃圾清理 (不需要手机)
        from cleanup.cleaner import Cleaner
        stats = Cleaner(cfg).run(dry_run=args.dry_run)
        head = "预览(不删除)" if args.dry_run else "清理完成"
        print(f"[{head}] {Cleaner.format_stats(stats)}")
        return

    if args.mode == "wl_check":
        # 云自选Cookie诊断 (不需要手机)
        from ths.watchlist import (CloudWatchlist, WatchlistAuthError,
                                   WatchlistError, hexin_market)
        path = cfg.resolve(cfg.watchlist.cookie_file)
        log.info("wl_check: 读取Cookie %s", path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
        except OSError:
            print(f"错误: Cookie文件不存在: {path}")
            print("请用浏览器登录 https://www.10jqka.com.cn/ 后, F12复制任一 "
                  "*.10jqka.com.cn 请求的Cookie头, 整行粘贴到该文件")
            sys.exit(1)
        try:
            wl = CloudWatchlist(cookie, timeout=cfg.watchlist.request_timeout)
            items = wl.list_self()
        except WatchlistAuthError as e:
            print(f"Cookie失效/未登录: {e}")
            sys.exit(2)
        except WatchlistError as e:
            print(f"接口错误: {e}")
            sys.exit(3)
        print(f"云端'我的自选': {len(items)} 只")
        for code, mk in items[:20]:
            print(f"  {code}  市场ID={mk} (本地推断={hexin_market(code)})")
        if len(items) > 20:
            print(f"  ... 共{len(items)}只")
        return

    if args.mode == "selftest":
        # 交易规则回归集(离线可跑, 双源行情用例无网自动跳过)
        import runpy
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tests", "test_rules.py")
        runpy.run_path(path, run_name="__main__")
        return

    if args.mode == "preflight":
        # 盘前预置检查: 全链路自检 + 登录/缓存预热 (不需要手机前置, 内部自测)
        from preflight import run_preflight
        checks = run_preflight(cfg, send_notify=not args.dry_run)
        blocks = [c for c in checks if c.level == "BLOCK" and not c.ok]
        sys.exit(1 if blocks else 0)

    if args.mode == "manual_audit":
        # 人工操作主动对账 (不需要手机; 连xiadan读当日成交/持仓, 可在
        # auto_round运行时执行, 不碰手机端)
        from trader.manual_audit import run_manual_audit
        print(f"人工对账方向: {args.side} (人工买入buy/卖出sell/都查both)")
        sys.exit(0 if run_manual_audit(cfg, side=args.side) else 1)

    if args.mode == "web":
        # 可视化监控台 (不需要手机, 只读本地状态文件, 可与auto_round同跑;
        # 人工对账可直接在页面点按钮触发)
        from webapp.app import run_web
        run_web(cfg, port=args.port)
        return

    if args.mode == "auto_round":
        # 单实例锁(动设备前获取): 防止多开auto_round/pipeline导致重复发键
        # 下单、positions.json读改写互相覆盖; web/scan/watchdog/人工对账不持锁
        from instance_lock import acquire_or_exit
        instance_lock = acquire_or_exit(os.path.join(logs_dir, "app.lock"),
                                        kind="auto_round自动轮巡")

    # 设备检查: 无设备明确退出
    serials = list_devices()
    if not serials:
        print("错误: 未检测到 Android 设备。")
        print("请检查: 1)USB已连接 2)手机已开启USB调试 3)已授权此电脑 "
              "4)命令行 adb devices 能看到设备")
        sys.exit(1)
    log.info("已连接设备: %s", ", ".join(serials))

    device = Device(cfg.device.serial)
    control = UIController(device)
    shot = Screenshotter(device, cfg.device.screenshot_max_retries)

    # 名称映射所有模式都加载 (test_current 也需要反查代码)
    codes, name_to_code = load_stocks(cfg.resolve(cfg.paths.stocks_file))
    if args.mode == "test_current":
        log.info("测试模式: 检测当前页面股票(不切换)")
    elif args.mode == "scan_loop":
        log.info("循环模式: 从当前页面股票开始, 回到起点自动结束一轮")
    else:
        if not codes:
            print(f"错误: 股票池为空或不存在: {cfg.resolve(cfg.paths.stocks_file)}")
            sys.exit(1)
        limit = args.limit or LIMITS.get(args.mode)
        if limit:
            codes = codes[:limit]
        log.info("开始扫描 %d 只股票: %s%s",
                 len(codes), codes[:10], "..." if len(codes) > 10 else "")

    navigator = Navigator(control, cfg, name_to_code)
    page_detector = PageDetector(shot, cfg)
    detector = SignalDetector(cfg, UIHierarchyDetector(), VisionDetector(cfg))
    code_reader = CodeReader(cfg)
    scanner = StockScanner(cfg, device, control, shot, detector, navigator,
                           page_detector, code_reader)

    if args.mode == "auto_round":
        from decision.decision import DecisionEngine, Action, ActionType
        from models.positions import PositionStore
        from scanner.scheduler import AutoRoundScheduler
        from trader.trader import PaperTrader

        # 防休眠/防熄屏(长会话); 屏保进程会封锁PC端模拟输入, 提前告警
        import keepawake
        keepawake.start()
        blockers = keepawake.check_screensaver()
        if blockers:
            log.warning("检测到屏保进程%s运行中, PC端键盘模拟(热键下单)将被封锁, "
                        "请人工退出屏保", blockers)

        # 每日垃圾清理守卫 (每天第一轮启动时执行一次)
        from cleanup.cleaner import daily_guard
        daily_guard(cfg, logger=log)

        # 每日关键数据zip备份 (2026-09-18采纳, backup.enable默认关)
        if cfg.backup.enable:
            from cleanup.backup import run_backup
            br = run_backup(cfg)
            if not br.get("skipped"):
                log.info("每日备份完成: %s (%d个文件, 清理过期%d)",
                         br.get("path"), br.get("files"), br.get("pruned"))

        # 盘前预置检查: 全链路自检(设备/面板/登录/Cookie/告警/热榜预热),
        # 阻断项不过则拒绝启动自动交易, 避免带故障开盘
        from preflight import run_preflight
        pre_checks = run_preflight(cfg, send_notify=True)
        pre_blocks = [c for c in pre_checks if c.level == "BLOCK" and not c.ok]
        if pre_blocks:
            log.error("盘前自检存在%d项阻断问题, 自动交易不启动, 请按上面指引修复后重试",
                      len(pre_blocks))
            sys.exit(1)
        log.info("盘前自检通过(阻断项全部OK), 进入自动轮巡")

        # 系统心跳: 主线程touch + 后台线程写盘; 独立看门狗进程监控,
        # 程序死/卡死(心跳超120s未更新)自动推送告警
        hb = None
        if cfg.monitor.enable:
            from monitor.heartbeat import Heartbeat, ensure_watchdog
            hb = Heartbeat(os.path.join(logs_dir, "heartbeat.json"),
                           interval=cfg.monitor.heartbeat_interval,
                           touch_stale=cfg.monitor.heartbeat_touch_stale)
            hb.start()
            ensure_watchdog(cfg, args.config, logger=log)

        positions = PositionStore(cfg.resolve(cfg.positions.file))

        # 告警手机推送(Server酱微信/钉钉/飞书; 未配置key则自动禁用)
        from notify.notifier import Notifier
        notifier = Notifier(cfg)
        if notifier.enabled():
            log.info("告警手机推送已启用")
        # 设备掉线/重连超时推送(无人值守时唯一感知渠道)
        scanner.on_device_lost = lambda msg: notifier.send(
            "扫描设备断连", msg, level="CRITICAL")

        # 交易执行器: paper=纸面; auto=真实下单(hotkey=行情端快捷键 / form=xiadan表单)
        # 风控两种模式都生效(paper同样过kill_switch/每股每日限买/时段/限额)
        from trader.risk_control import RiskController
        risk = RiskController(cfg.risk, cfg.project_root,
                             positions_file=cfg.positions.file)
        if cfg.execution.mode == "auto":
            if cfg.execution.channel == "hotkey":
                from trader.hotkey_trader import HotkeyTrader
                trader = HotkeyTrader(cfg, risk, positions=positions)
                trader.notifier = notifier   # 后台撤单/延迟成交结果推送
                trader.seal_watcher.notifier = notifier  # 涨跌停封单异动推送
                log.info("交易模式: auto (行情端快捷键闪电下单)")
                boot = trader.daily_bootstrap()
                if boot.get("ok"):
                    log.info("每日交易bootstrap: %s", boot.get("detail"))
                else:
                    log.warning("每日交易bootstrap失败: %s (下单时会重试)",
                                boot.get("error"))
            else:
                from trader.easytrader_client import EasytraderClient
                trader = EasytraderClient(cfg, risk)
                log.info("交易模式: auto (xiadan表单下单)")
        else:
            trader = PaperTrader(risk)
            log.info("交易模式: paper (纸面)")

        # 启动恢复门(仅auto): 崩溃重启后先对账券商真实持仓+未完成委托,
        # 发现不一致(如崩溃前挂单延迟成交未补建仓/遗留活单)即熔断自动
        # 交易并手机告警, 等人工对账处理; 扫描与数据采集照常进行
        if cfg.execution.mode == "auto":
            try:
                from trader.manual_audit import startup_reconcile
                rec = startup_reconcile(cfg, notifier=notifier)
                if rec.get("halted"):
                    log.error("启动恢复门熔断自动交易, 原因: %s",
                              rec.get("reasons"))
                    print("\n" + "=" * 60)
                    print("⚠️  启动恢复发现账户状态不一致, 已熔断自动交易:")
                    for i, r in enumerate(rec.get("reasons", []), 1):
                        print(f"   {i}. {r}")
                    print(f"   熔断文件: {rec.get('kill_switch')}")
                    print("   扫描继续; 请在监控台人工对账处理后删除熔断文件")
                    print("=" * 60)
                elif rec.get("ok"):
                    log.info("启动恢复门通过: 本地持仓与券商一致")
            except Exception as e:
                log.error("启动恢复门执行异常(不阻断启动): %s", e)

        decision = DecisionEngine(positions, trader, logs_dir,
                                  name_map=name_to_code,
                                  mode=cfg.execution.mode,
                                  default_qty=cfg.risk.default_qty,
                                  notifier=notifier,
                                  universe_cfg=cfg.universe,
                                  max_positions=cfg.positions.max_positions)
        scheduler = AutoRoundScheduler(cfg, scanner, navigator, decision,
                                       positions, heartbeat=hb)
        # 大盘系统性风控: 每轮轮头查指数, -3%预警/-4%当日自动熔断
        from monitor.market_guard import MarketGuard
        market_guard = MarketGuard(cfg, risk, notifier=notifier)
        # 运行资源守卫: 每轮轮头查磁盘余量/logs体量/内存, 只告警不改变交易;
        # 磁盘<2GB(会导致持仓原子写失败)走CRITICAL手机推送
        from monitor.resources import ResourceGuard
        resource_guard = ResourceGuard(cfg, notifier=notifier)
        # Cookie 自动保活 (cloud模式): 后台线程每30分钟探测一次, sessionid自动续期
        keeper = None
        if cfg.watchlist.sync_mode == "cloud":
            from ths.cookie_keeper import CookieKeeper
            cookie_path = cfg.resolve(cfg.watchlist.cookie_file)
            keeper = CookieKeeper(
                cookie_loader=lambda: _read_cookie(cookie_path),
                alert_fn=lambda r: decision.execute(
                    Action(ActionType.ALERT, "COOKIE", "-", r, "keepalive")),
                interval=cfg.watchlist.keepalive_interval,
                timeout=cfg.watchlist.request_timeout)
            keeper.start()
        interval = cfg.hot_list.round_interval
        # 自适应: 持仓越多轮间隔越短(需更频繁巡检覆盖卖出信号)
        n_held = len(positions.codes())
        if n_held >= 3:
            interval = max(180, interval - 120)
            log.info("auto_round: 持仓%d只, 轮间隔缩短至%.0fs", n_held, interval)
        elif n_held >= 1:
            interval = max(240, interval - 60)
            log.info("auto_round: 持仓%d只, 轮间隔缩短至%.0fs", n_held, interval)
        else:
            log.info("auto_round: 轮间隔%.0fs 执行模式=%s 空仓",
                     interval, cfg.execution.mode)
        from monitor.report import (maybe_send_lunch_report,
                                    maybe_send_closing_report)

        def idle_sleep(seconds: float):
            """轮间等待: 分片睡眠, 片间touch心跳+日报+尾盘14:57结算(防心跳误报/错过竞价)。"""
            deadline = time.time() + max(0.0, seconds)
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    return
                time.sleep(min(20.0, remain))
                if hb:
                    hb.touch()
                try:
                    # 14:57-15:00尾盘集合竞价兜底结算(每日一次, 内部有时间窗守卫)
                    scheduler.maybe_closing_settle()
                except Exception as e:
                    log.warning("尾盘结算检查异常: %s", e)
                try:
                    maybe_send_lunch_report(cfg, positions, notifier,
                                            rounds_today=rnd)
                except Exception as e:
                    log.warning("盘中日报检查异常: %s", e)
                try:
                    # 15:05后收盘日报(台账终态/尾盘结算/收盘价, 每日一次)
                    maybe_send_closing_report(cfg, positions, notifier,
                                              rounds_today=rnd)
                except Exception as e:
                    log.warning("收盘日报检查异常: %s", e)

        rnd = 0
        while True:
            rnd += 1
            t0 = time.time()
            if hb:
                hb.set_phase("轮开始", round=rnd)
            # 轮头也检查尾盘结算(窗口内每20s内必触发, 不依赖idle路径)
            try:
                scheduler.maybe_closing_settle()
            except Exception as e:
                log.warning("尾盘结算检查异常: %s", e)
            # 大盘风控: -3%预警/-4%买侧熔断(只禁买入, 卖出/撤单照常;
            # 回升自动解除, 行情失败降级绝不误熔断)
            try:
                mg = market_guard.check()
                if mg in ("warn", "halt", "halted", "recovered"):
                    log.warning("大盘风控结果: %s", mg)
            except Exception as e:
                log.warning("大盘风控检查异常: %s", e)
            # unknown挂单悬置超时升级(2026-09-18采纳): 15min CRITICAL/60min标记
            try:
                for s in risk.scan_unknown_stuck():
                    log.log(logging.CRITICAL if s["level"] == "CRITICAL"
                            else logging.WARNING,
                            "挂单悬置升级: %s %s 已%d分钟",
                            s["code"], s["action"], s["age"] // 60)
                    try:
                        notifier.send(
                            f"挂单悬置{s['age'] // 60}分钟 {s['code']}",
                            f"{s['action']}挂单状态长期unknown, "
                            f"已悬置{s['age'] // 60}分钟, 请人工核对成交/撤单。",
                            level=s["level"])
                    except Exception:
                        pass
            except Exception as e:
                log.warning("unknown悬置检查异常: %s", e)
            # 资源守卫: 磁盘/日志体量/内存异常只告警(边沿触发不刷屏)
            try:
                resource_guard.check()
            except Exception as e:
                log.warning("资源检查异常: %s", e)
            results, summary = scheduler.run_round()
            # 主轮可能在14:57被中断让路(跳过云同步提前返回), 立即触发结算,
            # 不必等idle_sleep的20s分片
            try:
                scheduler.maybe_closing_settle()
            except Exception as e:
                log.warning("尾盘结算检查异常: %s", e)
            csv_path = os.path.join(logs_dir, f"auto_round_{rnd:03d}.csv")
            write_results_csv(csv_path, results)
            print(format_summary(summary))
            print(f"明细CSV: {csv_path}")
            try:
                maybe_send_lunch_report(cfg, positions, notifier,
                                        rounds_today=rnd)
            except Exception as e:
                log.warning("盘中日报检查异常: %s", e)
            if args.rounds and rnd >= args.rounds:
                break
            wait = interval - (time.time() - t0)
            if wait > 0:
                idle_iv = cfg.positions.idle_recheck_interval
                if idle_iv > 0 and positions.codes():
                    # 轮间有持仓: 先立即巡检一次(消除两轮交界空信号盲区),
                    # 再按idle_iv周期巡检; 无持仓直接等待
                    log.info("轮间等待%.0fs, 持仓%d只: 立即巡检空信号后每%.0fs复查",
                             wait, len(positions.codes()), idle_iv)
                    while wait > 0:
                        try:
                            scheduler.idle_position_check()
                        except Exception as e:
                            log.error("轮间持仓巡检异常: %s", e)
                        wait = interval - (time.time() - t0)
                        if wait <= 0:
                            break
                        idle_sleep(min(idle_iv, wait))
                        wait = interval - (time.time() - t0)
                else:
                    log.info("等待%.0fs后开始第%d轮", wait, rnd + 1)
                    idle_sleep(wait)
        if keeper:
            keeper.stop()
        if hb:
            hb.stop()
        return

    if args.mode == "test_current":
        results, summary = scanner.scan_current()
    elif args.mode == "scan_loop":
        # 上限: --limit > config.switch.loop_max_stocks; 须>=自选股循环长度
        cap = args.limit or cfg.switch.loop_max_stocks
        log.info("循环模式: 从当前页面股票开始, 回到起点自动结束一轮 (上限%d只)", cap)
        results, summary = scanner.scan_loop(max_stocks=cap)
    else:
        results, summary = scanner.scan(codes)

    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(logs_dir, f"results_{ts}.csv")
    json_path = os.path.join(logs_dir, f"summary_{ts}.json")
    write_results_csv(csv_path, results)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary.to_dict(), f, ensure_ascii=False, indent=2)

    print(format_summary(summary))
    print(f"明细CSV: {csv_path}")
    print(f"汇总JSON: {json_path}")


if __name__ == "__main__":
    main()
