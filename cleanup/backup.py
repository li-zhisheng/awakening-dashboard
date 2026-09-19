"""每日关键数据zip备份 (2026-09-18第二版点评采纳, backup.enable默认关)。

auto_round每日首轮启动时调用run_backup, 把关键状态打成zip:
positions.json / logs/trade_state.json / logs/positions.snapshot.json /
config.yaml / 当日events_YYYYMMDD.jsonl / 当日lunch/closing日报。
按retain_days滚动清理旧zip。默认排除ths_cookie.txt凭据(不打包);
config.yaml内含webhook URL, 备份目录为本地目录, 请勿外传。
同日只执行一次(guard文件), force=True可手动强制。
"""
import glob
import logging
import os
import time
import zipfile

log = logging.getLogger("backup")

GUARD_FILE = "logs/last_backup.date"


def _prune(bdir: str, retain_days: int) -> int:
    """删除超过retain_days的备份zip, 返回清理个数。"""
    cutoff = time.time() - float(retain_days) * 86400
    n = 0
    for fp in glob.glob(os.path.join(bdir, "backup_*.zip")):
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                n += 1
        except OSError:
            pass
    return n


def run_backup(cfg, force: bool = False) -> dict:
    """执行当日备份, 返回 {path, files, pruned, skipped}。"""
    bc = cfg.backup
    root = cfg.project_root
    today = time.strftime("%Y-%m-%d")
    guard = os.path.join(root, GUARD_FILE)
    if not force:
        try:
            with open(guard, "r", encoding="utf-8") as f:
                if f.read().strip() == today:
                    return {"path": "", "files": 0, "pruned": 0,
                            "skipped": True}
        except OSError:
            pass

    bdir = cfg.resolve(bc.dir)
    os.makedirs(bdir, exist_ok=True)
    date8 = time.strftime("%Y%m%d")
    path = os.path.join(
        bdir, f"backup_{date8}_{time.strftime('%H%M%S')}.zip")
    nfiles = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def add(fp, arc=""):
            nonlocal nfiles
            if fp and os.path.isfile(fp):
                z.write(fp, arc or os.path.basename(fp))
                nfiles += 1

        # 关键状态文件(存在才打)
        add(os.path.join(root, "positions.json"))
        add(os.path.join(root, "logs", "trade_state.json"))
        add(os.path.join(root, "logs", "positions.snapshot.json"))
        add(os.path.join(root, "config.yaml"))
        # 当日审计事件
        add(os.path.join(root, "logs", f"events_{date8}.jsonl"))
        # 当日日报
        add(os.path.join(root, "logs", f"lunch_report_{date8}.txt"))
        add(os.path.join(root, "logs", f"closing_report_{date8}.txt"))

    if not force:
        os.makedirs(os.path.dirname(guard), exist_ok=True)
        with open(guard, "w", encoding="utf-8") as f:
            f.write(today)
    pruned = _prune(bdir, bc.retain_days)
    log.info("每日备份完成: %s (%d个文件), 清理过期备份%d个",
             path, nfiles, pruned)
    return {"path": path, "files": nfiles, "pruned": pruned,
            "skipped": False}
