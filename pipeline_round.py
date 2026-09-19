"""阶段一+阶段二联调流水线 (一次性执行, 非常驻循环):

  Phase A  拉取同花顺热榜TopN
  Phase B  云端"我的自选"整表同步为热榜 (手机App三端云同步自动刷新)
  Phase C  手机进入自选列表首只(日K)
  Phase D  主轮'>'顺序全量扫描, 记录所有多/空信号 -> logs/pipeline_*.csv
  Phase E  按扫描顺序对前N只多头信号执行快捷键买入(F1, 25%仓)
  Phase F  全程计时报告 -> logs/pipeline_timing_*.json + stdout

用法:
    python pipeline_round.py                     # 默认 Top100 / 买4只
    python pipeline_round.py --top 100 --buy-count 4
    python pipeline_round.py --after-hours      # 盘后人工执行: 放宽时段检查
                                                # (仅放开时间窗, kill_switch/
                                                #  冷却/日限额/单笔限额仍生效)
注意:
- A股T+1: 当日买入次日才能卖。
- 买入走行情端快捷键(F1), 每单后回查xiadan当日委托确认按键生效。
"""
import argparse
import json
import logging
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

log = logging.getLogger("pipeline")


LOG_MAX_BYTES = 10 * 1024 * 1024   # 单个日志10MB后轮转, 保留3份(同main.py)
LOG_BACKUP_COUNT = 3


def setup_logging(logs_dir: str) -> str:
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"pipeline_{time.strftime('%Y%m%d_%H%M%S')}.log")
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


def read_cookie(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("--top", type=int, default=0, help="热榜取前N只(默认config)")
    ap.add_argument("--buy-count", type=int, default=4, help="买入多头前N只")
    ap.add_argument("--after-hours", action="store_true",
                    help="盘后人工执行: 放宽交易时段检查(其余风控保留)")
    ap.add_argument("--no-buy", action="store_true", help="只扫不买")
    args = ap.parse_args()

    from config import load_config
    cfg = load_config(args.config)

    # adb path注入
    import os as _os
    adb = cfg.device.adb_path
    if adb:
        exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
        if _os.path.isfile(exe):
            _os.environ["PATH"] = _os.path.dirname(exe) + _os.pathsep + _os.environ["PATH"]

    logs_dir = cfg.resolve(cfg.paths.logs_dir)
    log_path = setup_logging(logs_dir)
    log.info("流水线启动, 日志: %s", log_path)

    # 单实例锁: pipeline会整表覆盖云自选并真实发键买入, 必须与auto_round
    # 互斥(同锁); 获取不到直接退出, 防止双进程重复下单/覆盖持仓
    from instance_lock import acquire_or_exit
    instance_lock = acquire_or_exit(
        _os.path.join(logs_dir, "app.lock"), kind="pipeline联调流水线")

    T0 = time.time()
    timing = {"phases": {}}

    # ===== Phase A: 热榜 + 标的过滤 =====
    ta = time.time()
    from ths import fetcher
    top_n = args.top or cfg.hot_list.top_n
    hot = fetcher.fetch(top_n, fallback_file=cfg.resolve(cfg.hot_list.fallback_file))
    if not hot:
        log.error("热榜拉取失败且无本地兜底, 终止")
        return
    from ths.universe_filter import filter_hot_stocks
    from models.positions import PositionStore as _PS
    _held = _PS(cfg.resolve(cfg.positions.file)).codes()
    hot_codes, name_map, excluded = filter_hot_stocks(hot, cfg, held_codes=_held)
    if excluded:
        log.info("[A] 标的过滤剔除%d只: %s", len(excluded),
                 ", ".join(f"{c}{n}({r})" for c, n, r in excluded[:10])
                 + ("..." if len(excluded) > 10 else ""))
    timing["phases"]["A_热榜拉取过滤"] = round(time.time() - ta, 1)
    log.info("[A] 热榜Top%d 过滤后可交易%d只: %s...", len(hot),
             len(hot_codes), hot_codes[:8])

    # ===== Phase B: 云自选整表同步 =====
    tb = time.time()
    from ths.watchlist import (CloudWatchlist, WatchlistAuthError,
                               WatchlistDeleteGuardError, WatchlistError)
    cookie = read_cookie(cfg.resolve(cfg.watchlist.cookie_file))
    if not cookie:
        log.error("Cookie缺失(%s), 无法云同步自选, 终止", cfg.watchlist.cookie_file)
        return
    try:
        wl = CloudWatchlist(
            cookie, timeout=cfg.watchlist.request_timeout,
            delete_guard_enable=cfg.watchlist.delete_guard_enable,
            delete_guard_max_pct=cfg.watchlist.delete_guard_max_pct)
        rep = wl.sync(hot_codes)
    except WatchlistAuthError as e:
        log.error("Cookie失效, 云同步终止: %s", e)
        return
    except WatchlistDeleteGuardError as e:
        log.critical("云自选删除护栏触发, 终止(云端零删除): %s", e)
        return
    except WatchlistError as e:
        log.error("云同步接口异常, 终止: %s", e)
        return
    timing["phases"]["B_云自选同步"] = round(time.time() - tb, 1)
    log.info("[B] 同步完成: 新增%d 删除%d 保留%d 失败=%s",
             len(rep["added"]), len(rep["removed"]), rep["kept"],
             rep["failed_add"] + rep["failed_del"] or "无")
    # 等手机云同步刷新(enter_watchlist自带下拉刷新兜底, 无需长等)
    log.info("[B] 等待5s手机端云同步刷新...")
    time.sleep(5)

    # ===== Phase C: 手机进自选 =====
    from adb.control import UIController
    from adb.device import Device
    from adb.screenshot import Screenshotter
    from detector.code_reader import CodeReader
    from detector.signal import SignalDetector
    from detector.ui_detector import UIHierarchyDetector
    from detector.vision_detector import VisionDetector
    from scanner.scanner import StockScanner
    from scanner.timing import write_results_csv
    from ths.navigator import Navigator
    from ths.page_detector import PageDetector

    tc = time.time()
    device = Device(cfg.device.serial)
    control = UIController(device)
    shot = Screenshotter(device, cfg.device.screenshot_max_retries)
    navigator = Navigator(control, cfg, name_map)
    page_detector = PageDetector(shot, cfg)
    detector = SignalDetector(cfg, UIHierarchyDetector(), VisionDetector(cfg))
    code_reader = CodeReader(cfg)
    scanner = StockScanner(cfg, device, control, shot, detector, navigator,
                           page_detector, code_reader)
    ok = scanner._with_reconnect(lambda: navigator.enter_watchlist(""))
    if not ok:
        log.error("进入自选页失败, 终止")
        return
    timing["phases"]["C_进入自选页"] = round(time.time() - tc, 1)
    log.info("[C] 已进入自选列表首只(日K)")

    # ===== Phase D: 全量扫描 =====
    td = time.time()
    results, summary = scanner.scan_loop(max_stocks=top_n + 10)
    timing["phases"]["D_全量扫描"] = round(time.time() - td, 1)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(logs_dir, f"pipeline_{ts}.csv")
    write_results_csv(csv_path, results)

    ok_results = [r for r in results if r.status.value == "OK"]
    longs = [r for r in ok_results if r.signal.value == "LONG"]
    shorts = [r for r in ok_results if r.signal.value == "SHORT"]
    log.info("[D] 扫描%d只: 成功%d 多头%d 空头%d 无%d 明细=%s",
             len(results), len(ok_results), len(longs), len(shorts),
             sum(1 for r in ok_results if r.signal.value == "NONE"), csv_path)
    for r in longs:
        log.info("    多头: %s %s", r.stock_code, name_map.get(r.stock_code, ""))
    for r in shorts:
        log.info("    空头: %s %s", r.stock_code, name_map.get(r.stock_code, ""))

    # ===== Phase E: 前4只多头买入 =====
    timing["phases"]["E_买入"] = 0.0
    buy_report = []
    bought_codes = []   # 实际发出买入委托的股票(含filled/pending), 供盘后撤单
    if not args.no_buy and longs:
        from models.positions import PositionStore
        from trader.hotkey_trader import HotkeyTrader
        from trader.risk_control import RiskController

        if args.after_hours:
            cfg.risk.sessions = [["00:00:00", "23:59:59"]]
            # 盘后不可能成交: F6首查即可判定pending, 缩短轮询窗口加速测试
            cfg.hotkey.buy_quick_confirm = 10.0
            log.warning("[E] --after-hours: 已放宽交易时段检查(仅时间窗, "
                        "其余风控保留); F6确认窗口缩至10s(盘后无成交)")
        risk = RiskController(cfg.risk, cfg.project_root,
                             positions_file=cfg.positions.file)
        trader = HotkeyTrader(cfg, risk)
        boot = trader.daily_bootstrap()
        if boot.get("ok"):
            log.info("[E] 每日交易bootstrap: %s", boot.get("detail"))
        else:
            log.error("[E] 每日交易bootstrap失败: %s, 终止买入", boot.get("error"))
            return
        positions = PositionStore(cfg.resolve(cfg.positions.file))

        targets = longs[:args.buy_count]
        log.info("[E] 买入目标(扫描顺序前%d): %s", len(targets),
                 [r.stock_code for r in targets])
        for i, r in enumerate(targets, 1):
            tbo = time.time()
            rep = trader.execute_order(r.stock_code, "BUY",
                                       name=name_map.get(r.stock_code, ""))
            dt = round(time.time() - tbo, 1)
            timing["phases"]["E_买入"] = round(timing["phases"]["E_买入"] + dt, 1)
            nm = name_map.get(r.stock_code, "")
            entry = {"code": r.stock_code, "name": nm, "seconds": dt, **rep}
            buy_report.append(entry)
            bought_codes.append(r.stock_code)
            if rep.get("ok"):
                # F6确认成交(filled)才写持仓; pending=挂单未成交不写
                positions.add(r.stock_code, name=nm,
                              entry_price=rep.get("filled_price", 0.0),
                              note=f"流水线买入{i} {time.strftime('%m-%d %H:%M')}")
                log.info("    买入%d %s %s: 成交 status=%s (%.1fs)",
                         i, r.stock_code, nm, rep.get("status"), dt)
            elif rep.get("pending"):
                log.info("    买入%d %s %s: 挂单中pending(未成交, 后台5min复查"
                         "/撤单) (%.1fs)", i, r.stock_code, nm, dt)
            else:
                log.error("    买入%d %s %s: 失败 %s (%.1fs)", i, r.stock_code,
                          nm, rep.get("error"), dt)

        # 盘后测试: F1挂的是夜市委托(次日才可能成交), 测完先F8逐只撤清理,
        # 再F5兜底全撤; 同时验证F8/F5撤单链路
        if args.after_hours and bought_codes:
            log.info("[E] 盘后测试: 等待2s后F8逐只撤单清理%d笔夜市委托...",
                     len(bought_codes))
            time.sleep(2)
            tc = time.time()
            try:
                trader._connect_hexin()
                for bc in bought_codes:
                    ok = trader._cancel_single_by_hotkey(bc)
                    log.info("[E] F8单只撤 %s: %s", bc,
                             "已生效" if ok else "需人工确认")
                log.info("[E] F8逐只撤完成, 耗时%.1fs", time.time() - tc)
                # F5兜底全撤(确保无遗漏)
                cancelled = trader._cancel_by_hotkey()
                log.info("[E] F5兜底全撤(%s), 总耗时%.1fs",
                         "已生效/无确认框" if cancelled else "需人工确认",
                         time.time() - tc)
            except Exception as e:
                log.error("[E] 撤单异常: %s (请次日开盘前人工撤单)", e)
    elif args.no_buy:
        log.info("[E] --no-buy: 跳过买入")

    # ===== Phase F: 计时报告 =====
    total = round(time.time() - T0, 1)
    timing["total_seconds"] = total
    timing["scan_summary"] = summary.to_dict()
    timing["buy_report"] = buy_report
    timing_path = os.path.join(logs_dir, f"pipeline_timing_{ts}.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump(timing, f, ensure_ascii=False, indent=1)

    print("\n========== 流水线计时报告 ==========")
    print(f"{'阶段':<14}{'耗时(s)':>10}")
    for k, v in timing["phases"].items():
        print(f"{k:<16}{v:>8.1f}")
    print(f"{'总计':<16}{total:>8.1f}")
    print(f"明细CSV: {csv_path}")
    print(f"计时JSON: {timing_path}")


if __name__ == "__main__":
    main()
