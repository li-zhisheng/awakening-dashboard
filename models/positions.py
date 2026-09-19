"""持仓状态模型: positions.json 是持仓的唯一事实源 (PC侧, 不依赖App)。

损坏自愈(2026-09-15): positions.json解析失败时, 先隔离损坏原件(.corrupt-*),
再尝试从"滚动快照(logs/positions.snapshot.json) + events_*.jsonl交易事件"
重建: 快照定基线, 重放快照之后的position_add/position_remove得到最新持仓,
写回事实源; 无任何重建数据源时才置_corrupt拒写(原2026-09-10防护保留)。
"""
import glob
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List

from models.audit import get_event_log

log = logging.getLogger("positions")


class PositionFileCorruptError(RuntimeError):
    """positions.json存在但解析失败; 已隔离备份, 拒绝空状态覆盖写。"""


@dataclass
class Position:
    code: str
    name: str = ""
    entry_time: str = ""
    entry_price: float = 0.0
    note: str = ""


class PositionStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._positions: Dict[str, Position] = {}
        # 文件损坏标记: True时拒绝一切写操作, 防止"解析失败→内存空仓
        # →下次save把唯一事实源覆盖成空文件"导致持仓永久丢失
        self._corrupt = False
        self._logs_dir = os.path.join(os.path.dirname(
            os.path.abspath(path)), "logs")
        # 滚动快照: 每次成功写持仓后同步(tmp+replace), 损坏时作重建基线
        self._snapshot_path = os.path.join(self._logs_dir,
                                           "positions.snapshot.json")
        self._el = get_event_log(self._logs_dir)
        self.load()

    def load(self, quiet: bool = False):
        """读盘合并。文件不存在=真空仓; 存在但解析失败=损坏: 先隔离备份,
        再尝试从快照+事件自愈重建; 重建无门才置_corrupt拒写。"""
        if not os.path.isfile(self.path):
            self._positions = {}
            self._corrupt = False
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._positions = {p["code"]: Position(**p) for p in raw.get("positions", [])}
            self._corrupt = False
            if not quiet:
                log.info("持仓加载: %d 只 %s", len(self._positions),
                         list(self._positions))
        except Exception as e:
            # 关键防护: 绝不用空字典覆盖损坏的事实源文件
            log.error("持仓文件读取失败(%s), 保留原文件并隔离备份", e)
            self._quarantine(str(e))
            if self._rebuild_from_events():
                # 自愈成功: 重建结果原子写回事实源, 恢复读写
                self._corrupt = False
                self._write_locked()
                log.critical("持仓损坏已自愈: 重建%d只并写回%s, "
                             "损坏原件已隔离请人工核对",
                             len(self._positions), self.path)
                try:
                    self._el.log("anomaly", stage="positions_rebuilt",
                                 holdings=list(self._positions),
                                 error=str(e)[:200])
                except Exception:
                    pass
            else:
                log.error("无快照/事件可重建, 修复前禁止写持仓")
                self._corrupt = True

    def _quarantine(self, err: str = ""):
        """把损坏文件复制为 .corrupt-时间戳 供人工恢复(幂等: 每次新发现才备份)。"""
        try:
            bak = f"{self.path}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(self.path, bak)
            log.error("损坏持仓文件已隔离: %s (%s)", bak, err)
        except Exception as e2:
            log.error("损坏文件隔离失败: %s", e2)

    def _write_snapshot_locked(self):
        """调用方持锁; 持仓滚动快照原子写(损坏重建的基线)。

        event_cursor(2026-09-18第二版点评采纳, 智谱A5): 同时记录此刻各事件
        文件的字节偏移。建仓/清仓的审计事件在positions锁释放后才写入, 故游标
        长度必不包含"本次变更事件", 重放时会补上->不漏; 快照前的事件均已反映
        在快照持仓内->不重。旧快照读取侧无cursor时回退ts比较并WARNING。
        """
        data = {"ts": round(time.time(), 3),
                "date": time.strftime("%Y-%m-%d"),
                "positions": [asdict(p) for p in self._positions.values()]}
        try:
            data["event_cursor"] = self._el.cursor()
        except Exception as e:
            log.warning("快照事件游标采集失败(重建将回退ts比较): %s", e)
        os.makedirs(self._logs_dir, exist_ok=True)
        tmp = self._snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._snapshot_path)

    def _read_snapshot(self) -> dict:
        """读滚动快照, 损坏/缺失返回{}。"""
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(
                    data.get("positions"), list):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _rebuild_from_events(self) -> bool:
        """从"滚动快照 + events交易事件"重建内存持仓, 成功返回True。

        有快照: 快照定基线, 仅重放其后的position_add/remove。两种游标:
        - event_cursor(2026-09-18采纳): 按事件文件字节偏移重放(同秒事件不漏);
        - 旧快照无cursor: 回退ts>快照ts比较并WARNING一次。
        无快照: 重放logs内全部事件(事件保留1年, 短线持仓天级, 覆盖充分),
        但无快照又无任何事件=凭空重建不可信, 返回False维持_corrupt拒写。
        """
        snap = self._read_snapshot()
        rebuilt: Dict[str, Position] = {}
        if snap:
            for p in snap["positions"]:
                if isinstance(p, dict) and p.get("code"):
                    rebuilt[str(p["code"])] = Position(
                        code=str(p["code"]), name=p.get("name", ""),
                        entry_time=p.get("entry_time", ""),
                        entry_price=float(p.get("entry_price") or 0.0),
                        note=p.get("note", ""))
        since_ts = float(snap.get("ts") or 0.0) if snap else 0.0
        since_date = (snap.get("date", "").replace("-", "")
                      if snap else "")
        cursor = snap.get("event_cursor") if snap else None
        use_cursor = isinstance(cursor, dict) and bool(cursor)
        if snap and not use_cursor:
            log.warning("旧持仓快照无event_cursor, 重建回退时间戳比较"
                        "(同秒事件理论上可能漏放, 重写一次快照后即升级)")
        n_evt = 0
        ev_files = sorted(glob.glob(
            os.path.join(self._logs_dir, "events_*.jsonl")))
        for fp in ev_files:
            date8 = os.path.basename(fp)[7:15]
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    if use_cursor:
                        start = cursor.get(os.path.basename(fp))
                        if start is None:
                            # 快照后新建的事件文件(如跨天): 全量; 更早文件跳过
                            if since_date and date8 < since_date:
                                continue
                            start = 0
                        f.seek(int(start))
                    else:
                        if since_date and date8 < since_date:
                            continue
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except ValueError:
                            continue
                        if evt.get("kind") != "trade":
                            continue
                        if not use_cursor and snap \
                                and float(evt.get("ts") or 0.0) <= since_ts:
                            continue
                        if self._apply_trade_event(rebuilt, evt):
                            n_evt += 1
            except OSError as e:
                log.warning("重建读取事件失败 %s: %s", fp, e)
        if not snap and n_evt == 0:
            return False
        self._positions = rebuilt
        log.warning("持仓重建数据源: %s, 重放交易事件%d条 -> %d只 %s",
                    "快照+字节游标" if use_cursor else
                    "快照+时间戳" if snap else "仅事件全量(无快照, 请人工核对)",
                    n_evt, len(rebuilt), list(rebuilt))
        return True

    @staticmethod
    def _apply_trade_event(rebuilt: dict, evt: dict) -> bool:
        """对重建集合应用一条position_add/remove交易事件, 返回是否命中。"""
        code = evt.get("code")
        stage = evt.get("stage")
        if not code:
            return False
        code = str(code)
        if stage == "position_add":
            rebuilt[code] = Position(
                code=code, name=evt.get("name", ""),
                entry_time=evt.get("entry_time", ""),
                entry_price=float(evt.get("entry_price") or 0.0),
                note=evt.get("note", ""))
            return True
        if stage == "position_remove":
            rebuilt.pop(code, None)
            return True
        return False

    def _write_locked(self):
        """调用方须持self._lock; 原子写(tmp+replace), 成功后刷快照。"""
        if self._corrupt:
            raise PositionFileCorruptError(
                f"持仓文件损坏未修复, 拒绝覆盖写 {self.path}; "
                f"请人工恢复 .corrupt-* 备份后重启")
        data = {"positions": [asdict(p) for p in self._positions.values()]}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        try:
            self._write_snapshot_locked()
        except OSError as e:
            # 主文件已落盘, 快照只影响下次重建基线, 不阻断本次写
            log.warning("持仓快照写入失败(不影响主文件): %s", e)

    def save(self):
        with self._lock:
            self._write_locked()

    def is_held(self, code: str) -> bool:
        return code in self._positions

    def codes(self) -> List[str]:
        return list(self._positions.keys())

    def get(self, code: str) -> Position | None:
        return self._positions.get(code)

    def snapshot(self) -> List[dict]:
        """持仓快照(线程安全副本, 日报/对账用)。"""
        with self._lock:
            return [asdict(p) for p in self._positions.values()]

    def _audit(self, stage: str, **fields):
        """持仓状态机审计: 每次建仓/清仓落事件(对账"系统vs券商"的依据)。"""
        try:
            self._el.log("trade", stage=stage,
                         holdings=[p["code"] for p in self.snapshot()],
                         **fields)
        except Exception as e:
            log.warning("持仓审计事件写入失败: %s", e)

    def add(self, code: str, name: str = "", entry_price: float = 0.0,
            note: str = "") -> bool:
        """买入建仓。已持有返回 False。

        写前重读文件合并: 后台补建仓线程/其他实例可能已写入新持仓,
        避免读-改-写竞态丢更新。
        """
        with self._lock:
            self.load(quiet=True)      # 合并磁盘最新状态
            if code in self._positions:
                return False
            pos = Position(
                code=code, name=name, entry_price=entry_price, note=note,
                entry_time=time.strftime("%Y-%m-%d %H:%M:%S"))
            self._positions[code] = pos
            self._write_locked()
        log.info("建仓: %s %s", code, name)
        self._audit("position_add", code=code, name=name,
                    entry_price=entry_price, entry_time=pos.entry_time,
                    note=note)
        return True

    def remove(self, code: str) -> Position | None:
        """卖出清仓。未持有返回 None。写前重读文件合并(防竞态)。"""
        with self._lock:
            self.load(quiet=True)
            pos = self._positions.pop(code, None)
            if pos:
                self._write_locked()
        if pos:
            log.info("清仓: %s %s (持仓自 %s)", code, pos.name, pos.entry_time)
            self._audit("position_remove", code=code, name=pos.name,
                        entry_price=pos.entry_price, entry_time=pos.entry_time,
                        held_since=pos.entry_time)
        return pos
