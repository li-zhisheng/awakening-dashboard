"""人工核对轮: 生产路径跑一轮全量扫描并落盘结构化结果, 供监控台"信号核对"页使用。

与 pipeline_round.py 的区别: 不拉热榜、不动云自选、不交易——只进自选页
沿'>'扫一整轮, 结果写 logs/verify_YYYYMMDD_HHMMSS.csv(含每只的
capture_time/信号/置信度/状态/错误), 页面按 capture_time 配对
screenshots/{code}_{ts}_annotated.png 逐只展示人工核对。

用法:
    python verify_round.py            # 扫描当前手机自选全列表
    python verify_round.py --max 30   # 只扫前30只(快速抽查)
"""
import argparse
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    ap.add_argument("--max", type=int, default=0, help="限制扫描只数(0=全部)")
    args = ap.parse_args()

    from config import load_config
    cfg = load_config(args.config)

    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout)

    adb = cfg.device.adb_path
    if adb:
        exe = adb if adb.lower().endswith(".exe") else os.path.join(adb, "adb.exe")
        if os.path.isfile(exe):
            os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ["PATH"]

    from adb.control import UIController
    from adb.device import Device
    from adb.screenshot import Screenshotter
    from detector.code_reader import CodeReader
    from detector.signal import SignalDetector
    from detector.ui_detector import UIHierarchyDetector
    from detector.vision_detector import VisionDetector
    from scanner.scanner import StockScanner
    from scanner.timing import format_summary, write_results_csv
    from ths.navigator import Navigator
    from ths.page_detector import PageDetector

    device = Device(cfg.device.serial)
    control = UIController(device)
    shot = Screenshotter(device, cfg.device.screenshot_max_retries)
    navigator = Navigator(control, cfg)
    page_detector = PageDetector(shot, cfg)
    detector = SignalDetector(cfg, UIHierarchyDetector(), VisionDetector(cfg))
    code_reader = CodeReader(cfg)
    scanner = StockScanner(cfg, device, control, shot, detector, navigator,
                           page_detector, code_reader)

    t0 = time.time()
    ok = scanner._with_reconnect(lambda: navigator.enter_watchlist(""))
    if not ok:
        print("进入自选页失败, 终止")
        return 1
    print(f"已进入自选列表首只(日K) {time.time()-t0:.1f}s")

    results, summary = scanner.scan_loop(
        max_stocks=args.max + 2 if args.max else 10**9)
    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(cfg.resolve(cfg.paths.logs_dir), f"verify_{ts}.csv")
    write_results_csv(csv_path, results)
    print(format_summary(summary))
    print(f"核对CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
