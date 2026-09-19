"""cleanup模块离线测试: 夹具目录验证TTL删除/保留/dry_run/daily_guard。

通过项:
T1 dry_run只统计不删除
T2 过期文件删除, 新文件保留
T3 状态文件(hotlist/trade_state/alerts/cookie)永不清理
T4 daily_guard同日只执行一次
T5 白名单外文件不受影响
"""
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_clean_test")


def make_cfg(root):
    from config import AppConfig, CleanupConfig
    cfg = AppConfig()
    cfg.project_root = root
    # 缩短TTL: 截图1h(旧文件设为2h前), 其他用默认
    cfg.cleanup = CleanupConfig(screenshot_ttl_hours=1)
    return cfg


def touch(path, age_hours=0.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x" * 100)
    t = time.time() - age_hours * 3600
    os.utime(path, (t, t))


def main():
    from config import AppConfig
    from cleanup.cleaner import Cleaner, daily_guard

    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)

    # --- 夹具 ---
    # 截图: 1旧2新 (TTL 1h)
    touch(f"{ROOT}/screenshots/old_1.png", age_hours=2.0)
    touch(f"{ROOT}/screenshots/new_1.png", age_hours=0.1)
    touch(f"{ROOT}/screenshots/new_2.png", age_hours=0.5)
    # scan日志: 1旧 (TTL 336h)
    touch(f"{ROOT}/logs/scan_old.log", age_hours=400)
    touch(f"{ROOT}/logs/scan_new.log", age_hours=10)
    # 诊断: 旧png+xml (TTL 168h)
    touch(f"{ROOT}/logs/diag_old.png", age_hours=200)
    touch(f"{ROOT}/logs/ui_old.xml", age_hours=200)
    # 档案: 旧csv/json (TTL 2160h)
    touch(f"{ROOT}/logs/results_old.csv", age_hours=3000)
    touch(f"{ROOT}/logs/summary_old.json", age_hours=3000)
    # 状态文件(必须保留)
    touch(f"{ROOT}/logs/trade_state.json", age_hours=9999)
    touch(f"{ROOT}/logs/hotlist_local.json", age_hours=9999)
    touch(f"{ROOT}/logs/alerts.jsonl", age_hours=9999)
    touch(f"{ROOT}/ths_cookie.txt", age_hours=9999)
    # 白名单外: 随意命名的json/png不在规则内? png在logs/*.png规则内会删,
    # 但根目录png不在任何规则内
    touch(f"{ROOT}/positions.json", age_hours=9999)
    touch(f"{ROOT}/random.txt", age_hours=9999)
    touch(f"{ROOT}/logs/auto_round_001.csv", age_hours=1)  # 新档案, 保留

    cfg = make_cfg(ROOT)

    # T1 dry_run
    stats = Cleaner(cfg).run(dry_run=True)
    n_dry = sum(n for n, _ in stats.values())
    exp = 1 + 1 + 1 + 1 + 1  # old截图+scan旧+diag_old+ui_old+results_old = 5
    # summary_old.json 也在内 = 6
    exp = 6
    ok1 = n_dry == exp
    kept = (os.path.exists(f"{ROOT}/screenshots/old_1.png")
            and os.path.exists(f"{ROOT}/logs/scan_old.log"))
    print(f"T1 dry_run: 统计{n_dry}=={exp} -> {ok1}; 文件未删 -> {kept}")
    assert ok1 and kept, "T1失败"

    # T2/T3/T5 实删
    stats = Cleaner(cfg).run()
    survivors = {
        "新截图": os.path.exists(f"{ROOT}/screenshots/new_1.png"),
        "新截图2": os.path.exists(f"{ROOT}/screenshots/new_2.png"),
        "新scan日志": os.path.exists(f"{ROOT}/logs/scan_new.log"),
        "trade_state": os.path.exists(f"{ROOT}/logs/trade_state.json"),
        "hotlist": os.path.exists(f"{ROOT}/logs/hotlist_local.json"),
        "alerts": os.path.exists(f"{ROOT}/logs/alerts.jsonl"),
        "cookie": os.path.exists(f"{ROOT}/ths_cookie.txt"),
        "positions(根目录)": os.path.exists(f"{ROOT}/positions.json"),
        "白名单外txt": os.path.exists(f"{ROOT}/random.txt"),
        "新档案csv": os.path.exists(f"{ROOT}/logs/auto_round_001.csv"),
    }
    gone = {
        "旧截图删除": not os.path.exists(f"{ROOT}/screenshots/old_1.png"),
        "旧scan删除": not os.path.exists(f"{ROOT}/logs/scan_old.log"),
        "旧diag删除": not os.path.exists(f"{ROOT}/logs/diag_old.png"),
        "旧ui删除": not os.path.exists(f"{ROOT}/logs/ui_old.xml"),
        "旧results删除": not os.path.exists(f"{ROOT}/logs/results_old.csv"),
        "旧summary删除": not os.path.exists(f"{ROOT}/logs/summary_old.json"),
    }
    print(f"T2 过期删除: {gone}")
    print(f"T3/T5 保留: {survivors}")
    assert all(gone.values()), "T2失败"
    assert all(survivors.values()), "T3/T5失败"

    # T4 daily_guard
    s1 = daily_guard(cfg)
    s2 = daily_guard(cfg)
    print(f"T4 daily_guard: 第一次='{s1}' 第二次='{s2}'")
    assert "跳过" not in s1 and "跳过" in s2, "T4失败"

    print("\n全部通过 (6/6项)")
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
