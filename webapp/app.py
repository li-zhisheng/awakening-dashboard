"""Awakening 可视化监控台: Python标准库HTTP服务(零新依赖) + 静态单页前端。

用法:
    python main.py web [--port 8899]

- 不需要手机, 只读本地状态文件(positions.json / logs/*), 可与auto_round
  同时运行; auto_round每轮落盘的CSV/事件日志就是页面的实时数据源。
- 仅绑定127.0.0.1(本机访问), 不做鉴权, 请勿暴露到局域网/公网。
- 人工对账按钮走 /api/audit/scan + /api/audit/apply: 页面点按钮采集
  券商当日成交/持仓(短暂弹xiadan查询页, 查完自动关窗恢复热键), 用户在
  页面确认差异后回写positions.json并推送手机。
"""
import csv
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from config import AppConfig

log = logging.getLogger("webapp")

_STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_STATIC_DIR, "static")
MAX_BODY = 1 << 20          # POST body上限1MB(防误发大包)
_DATE_RE = re.compile(r"^\d{8}$")          # events文件日期白名单(防路径穿越)
# /api/audit/scan会连接xiadan读网格, 并发采集会双开客户端/抢窗口
_AUDIT_SCAN_LOCK = threading.Lock()
# 完整盘前自检会连接行情端窗口/F12, 串行执行避免与交易抢窗口
_PREFLIGHT_LOCK = threading.Lock()
# 轻量探活结果缓存(避免多客户端同时轮询打爆ADB/Cookie接口)
_HEALTH_CACHE = {"ts": 0.0, "data": None}
_HEALTH_TTL = 15.0
_HEALTH_LOCK = threading.Lock()
# 截图文件名白名单(code可能为unknown): {code}_{日期}_{时间}_{种类}.png
_SHOT_RE = re.compile(r"^[A-Za-z0-9]{3,12}_\d{8}_\d{6}_(raw|region|annotated)\.png$")
# 核对轮CSV白名单: verify_日期_时间/生产轮auto_round_序号/pipeline_日期_时间.csv
_VERIFY_FILE_RE = re.compile(
    r"^(verify_\d{8}_\d{6}|auto_round_\w{1,32}|pipeline_\d{8}_\d{6})\.csv$")


def _screenshots_dir(cfg: AppConfig) -> str:
    return cfg.resolve(cfg.paths.screenshots_dir)


def _pair_shots(cfg: AppConfig, rows: list) -> list:
    """给每行CSV配对检测时保存的标注截图(按文件名时间戳最近且±30分钟内)。

    截图文件名 {code}_{YYYYMMDD_HHMMSS}_annotated.png 与行capture_time
    同一时刻生成, 取该代码时间戳最接近的 annotated 图; 无截图置空。
    """
    import glob
    idx = {}   # code -> [(filets, fname), ...]
    pat = os.path.join(_screenshots_dir(cfg), "*_annotated.png")
    for f in glob.glob(pat):
        m = re.match(r"^([A-Za-z0-9]{3,12})_(\d{8}_\d{6})_annotated\.png$",
                     os.path.basename(f))
        if m:
            idx.setdefault(m.group(1), []).append(
                (time.mktime(time.strptime(m.group(2), "%Y%m%d_%H%M%S")),
                 os.path.basename(f)))
    for v in idx.values():
        v.sort()
    for r in rows:
        cap = r.get("capture_time") or ""
        best, best_d = "", 1800.0
        try:
            t = time.mktime(time.strptime(cap, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            t = None
        if t:
            for filets, fname in idx.get(r.get("stock_code") or "", []):
                d = abs(filets - t)
                if d <= best_d:
                    best, best_d = fname, d
        r["shot"] = best
    return rows


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _read_jsonl_tail(path: str, limit: int, kinds=None) -> list:
    """读JSONL尾部N条(按kinds过滤后), 返回时间升序列表。"""
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if kinds and evt.get("kind") not in kinds:
                    continue
                out.append(evt)
    except OSError as e:
        log.warning("读取%s失败: %s", path, e)
    return out[-limit:] if limit > 0 else out


class _State:
    """只读状态采集器(每个请求现场读盘, 文件都很小, 无需缓存)。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def logs_dir(self) -> str:
        return self.cfg.resolve(self.cfg.paths.logs_dir)

    def heartbeat(self) -> dict:
        hb = _read_json(os.path.join(self.logs_dir(), "heartbeat.json"), {})
        if hb.get("ts"):
            hb["age"] = round(time.time() - hb["ts"], 1)
        return hb

    def positions(self) -> list:
        raw = _read_json(self.cfg.resolve(self.cfg.positions.file),
                         {"positions": []})
        return raw.get("positions", [])

    def risk_state(self) -> dict:
        st = _read_json(os.path.join(self.logs_dir(), "trade_state.json"), {})
        kill = os.path.exists(self.cfg.resolve(self.cfg.risk.kill_switch_file))
        st["kill_switch"] = kill
        buy_halt = os.path.exists(
            self.cfg.resolve(self.cfg.risk.buy_halt_file))
        st["buy_halt"] = buy_halt
        return st

    def gate(self) -> dict:
        """TradingGate只读闸门总览(R6): 此刻买卖/撤单/重启是否放行。"""
        from monitor.trading_gate import build_gate_snapshot
        return build_gate_snapshot(self.cfg)

    def today_events(self, date: str = "", kinds=None, limit: int = 0) -> list:
        date = date or time.strftime("%Y%m%d")
        # 白名单校验: date直接拼文件名, 杜绝../穿越读取任意jsonl
        if not _DATE_RE.match(date):
            return []
        path = os.path.join(self.logs_dir(), f"events_{date}.jsonl")
        return _read_jsonl_tail(path, limit, kinds)

    def signal_states(self, events: list) -> dict:
        """当日每股最新信号(code -> LONG/SHORT/NONE/UNKNOWN)。"""
        states = {}
        for e in events:
            if e.get("kind") == "signal" and e.get("code"):
                states[e["code"]] = e.get("to", "")
        return states

    def name_map(self, events: list, need=None) -> dict:
        """code->名称: 今日事件+热榜快照+持仓+持久缓存; need里的代码仍缺名时
        腾讯行情兜底拉取并写回缓存(旧轮CSV不在最新热榜, 股票名曾大量空白)。"""
        names = {}
        for e in events:
            if e.get("name") and e.get("code"):
                names[e["code"]] = e["name"]
        hot = _read_json(os.path.join(self.logs_dir(),
                                      "hotlist_local.json"), [])
        for s in hot:
            names.setdefault(str(s.get("code", "")).zfill(6),
                             s.get("name", ""))
        for p in self.positions():
            names.setdefault(p.get("code", ""), p.get("name", ""))
        cache_path = os.path.join(self.logs_dir(), "name_cache.json")
        cache = _read_json(cache_path, {})
        merged = {**cache, **names}
        if need:
            missing = {c for c in need
                       if c and c not in merged and len(c) == 6
                       and c.isdigit()}
            if missing:
                from concurrent.futures import ThreadPoolExecutor
                from ths.quote import realtime_quote
                with ThreadPoolExecutor(max_workers=8) as ex:
                    got = zip(missing, ex.map(
                        lambda c: realtime_quote(c, 4.0), missing))
                    for c, q in got:
                        if q and q.get("name"):
                            merged[c] = q["name"]
                            cache[c] = q["name"]
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False)
                except OSError as e:
                    log.warning("名称缓存写入失败: %s", e)
        return merged

    def overview(self) -> dict:
        events = self.today_events()
        trades = [e for e in events if e.get("kind") == "trade"]
        coverage = [e for e in events if e.get("kind") == "coverage"]
        states = self.signal_states(events)
        held = {p["code"] for p in self.positions()}
        pos = self.positions()
        for p in pos:
            p["signal"] = states.get(p["code"], "")
        n = self.cfg.notify
        return {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat": self.heartbeat(),
            "config": {
                "mode": self.cfg.execution.mode,
                "channel": self.cfg.execution.channel,
                "max_positions": self.cfg.positions.max_positions,
                "recheck_interval": self.cfg.positions.recheck_interval,
                "round_interval": self.cfg.hot_list.round_interval,
                "top_n": self.cfg.hot_list.top_n,
                "notify_enabled": bool(n.qywechat_webhook or n.feishu_webhook
                                       or n.dingtalk_webhook
                                       or n.serverchan_key),
            },
            "positions": pos,
            "full": len(pos) >= self.cfg.positions.max_positions,
            "risk": self.risk_state(),
            "signals": states,
            "stats": {
                "scan_count": sum(1 for e in events if e.get("kind") == "scan"),
                "long_today": sum(1 for e in events
                                  if e.get("kind") == "signal"
                                  and e.get("to") == "LONG"),
                "short_today": sum(1 for e in events
                                   if e.get("kind") == "signal"
                                   and e.get("to") == "SHORT"),
                "full_signal_count": sum(1 for e in events
                                         if e.get("kind") == "full_signal"),
                "order_ok": sum(1 for e in trades
                                if e.get("stage") == "order_result"
                                and e.get("ok")),
                "order_fail": sum(1 for e in trades
                                  if e.get("stage") == "order_failed"),
                "manual_audit": sum(1 for e in trades
                                    if e.get("stage") == "manual_audit"),
                "coverage_last": (coverage[-1].get("coverage", 0)
                                  if coverage else None),
            },
        }

    def scan_latest(self) -> dict:
        """最近一轮扫描CSV(auto_round_*.csv按修改时间) + 名称回填。"""
        import glob
        files = (glob.glob(os.path.join(self.logs_dir(), "auto_round_*.csv"))
                 + glob.glob(os.path.join(self.logs_dir(), "verify_*.csv"))
                 + glob.glob(os.path.join(self.logs_dir(), "pipeline_*.csv")))
        if not files:
            return {"file": "", "mtime": 0, "rows": [], "summary": {}}
        path = max(files, key=os.path.getmtime)
        rows = []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    rows.append(r)
        except OSError as e:
            log.warning("读取%s失败: %s", path, e)
        names = self.name_map(
            self.today_events(),
            need={r.get("stock_code", "") for r in rows})
        for r in rows:
            r["name"] = names.get(r.get("stock_code", ""), "")
        ok = [r for r in rows if r.get("status") == "OK"]
        sig = lambda v: sum(1 for r in rows if r.get("signal") == v)
        elapsed = [float(r["elapsed_time"]) for r in ok
                   if r.get("elapsed_time")]
        summary = {
            "total": len(rows), "ok": len(ok),
            "long": sig("多"), "short": sig("空"), "none": sig("无"),
            "unknown": len(rows) - len(ok),
            "avg_elapsed": round(sum(elapsed) / len(elapsed), 2)
                           if elapsed else 0,
            "max_elapsed": round(max(elapsed), 2) if elapsed else 0,
        }
        return {"file": os.path.basename(path),
                "mtime": round(os.path.getmtime(path), 1),
                "mtime_str": time.strftime(
                    "%H:%M:%S", time.localtime(os.path.getmtime(path))),
                "rows": rows, "summary": summary}

    def verify_rounds(self) -> list:
        """可核对的轮次列表(人工核对轮verify_* + 生产轮auto_round_*)。"""
        import glob
        out = []
        for pat in ("verify_*.csv", "auto_round_*.csv", "pipeline_*.csv"):
            for f in glob.glob(os.path.join(self.logs_dir(), pat)):
                out.append({
                    "file": os.path.basename(f),
                    "mtime": round(os.path.getmtime(f), 1),
                    "mtime_str": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(os.path.getmtime(f))),
                })
        out.sort(key=lambda x: -x["mtime"])
        return out

    def verify_round(self, fname: str) -> dict:
        """读取一轮核对CSV + 名称回填 + 配对标注截图。"""
        if not _VERIFY_FILE_RE.match(fname or ""):
            return {"error": "文件名非法"}
        path = os.path.join(self.logs_dir(), fname)
        if not os.path.isfile(path):
            return {"error": "文件不存在"}
        rows = []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    rows.append(r)
        except OSError as e:
            log.warning("读取%s失败: %s", path, e)
        names = self.name_map(
            self.today_events(),
            need={r.get("stock_code", "") for r in rows})
        for r in rows:
            r["name"] = names.get(r.get("stock_code", ""), "")
        _pair_shots(self.cfg, rows)
        sig = lambda v: sum(1 for r in rows if r.get("signal") == v)
        ok = [r for r in rows if r.get("status") == "OK"]
        summary = {"total": len(rows), "ok": len(ok),
                   "long": sig("多"), "short": sig("空"), "none": sig("无"),
                   "unknown": len(rows) - len(ok)}
        return {"file": fname, "rows": rows, "summary": summary}

    def shot_path(self, fname: str):
        """校验并返回截图绝对路径; 非白名单/不存在返回None。"""
        if not _SHOT_RE.match(fname or ""):
            return None
        path = os.path.join(_screenshots_dir(self.cfg), fname)
        return path if os.path.isfile(path) else None

    def first_signals(self) -> dict:
        """当日首次多/空快照(最新一份first_signal_*.jsonl, 按时间升序)。"""
        import glob
        files = glob.glob(os.path.join(self.logs_dir(),
                                       "first_signal_*.jsonl"))
        if not files:
            return {"rows": [], "long": 0, "short": 0, "mtime_str": ""}
        path = max(files, key=os.path.getmtime)
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError as e:
            log.warning("读取%s失败: %s", path, e)
        rows.sort(key=lambda r: r.get("ts", ""))
        return {"rows": rows,
                "long": sum(1 for r in rows if r.get("dir") == "多"),
                "short": sum(1 for r in rows if r.get("dir") == "空"),
                "mtime_str": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(os.path.getmtime(path)))}

    def health(self, force: bool = False) -> dict:
        """轻量只读系统探活(15s缓存); force强制重跑。"""
        now = time.time()
        if not force and _HEALTH_CACHE["data"] and \
                now - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
            d = dict(_HEALTH_CACHE["data"])
            d["cached"] = True
            return d
        # 防止并发时多个请求同时真跑(只放一个, 其余等缓存)
        with _HEALTH_LOCK:
            if not force and _HEALTH_CACHE["data"] and \
                    time.time() - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
                d = dict(_HEALTH_CACHE["data"])
                d["cached"] = True
                return d
            from preflight import run_health_checks
            data = run_health_checks(self.cfg)
            _HEALTH_CACHE["ts"] = time.time()
            _HEALTH_CACHE["data"] = data
            data = dict(data)
            data["cached"] = False
            return data

    def run_full_preflight(self) -> dict:
        """完整盘前自检(连行情端/F12/热榜预热, 不发测试推送避免刷手机)。"""
        from dataclasses import asdict
        from preflight import run_preflight
        if not _PREFLIGHT_LOCK.acquire(blocking=False):
            return {"error": "已有自检在执行中(或交易窗口操作占用), 请稍候"}
        try:
            t0 = time.time()
            checks = run_preflight(self.cfg, send_notify=False)
            rows = [asdict(c) for c in checks]
            return {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed": round(time.time() - t0, 1),
                "checks": rows,
                "summary": {
                    "total": len(rows),
                    "ok": sum(1 for c in rows if c["ok"]),
                    "block": sum(1 for c in rows
                                 if c["level"] == "BLOCK" and not c["ok"]),
                    "warn": sum(1 for c in rows
                                if c["level"] == "WARN" and not c["ok"])},
            }
        except Exception as e:
            log.exception("完整自检失败")
            return {"error": f"自检异常: {e}"}
        finally:
            _PREFLIGHT_LOCK.release()

    def alerts(self, limit: int = 100) -> list:
        out = _read_jsonl_tail(
            os.path.join(self.logs_dir(), "alerts.jsonl"), limit)
        return out

    def hotlist(self, limit: int = 100) -> list:
        return _read_json(os.path.join(self.logs_dir(),
                                       "hotlist_local.json"), [])[:limit]

    def quotes(self, codes: list) -> dict:
        """实时行情快照(腾讯主/新浪备); 单只失败容错跳过。"""
        out = {}
        try:
            from ths.quote import realtime_quote
            for c in codes[:30]:
                try:
                    q = realtime_quote(str(c).strip())
                    if q:
                        out[str(c).strip()] = q
                except Exception:
                    continue
        except Exception as e:
            log.warning("行情查询失败: %s", e)
        return out

    def attempts(self) -> dict:
        """当日挂单尝试台账终态+实时盘口(挂单状态页数据源)。

        行: code/name/action/status/首挂价/时间/是否持仓/现价/相对首挂价
        偏移/涨跌停封板/停牌; pending/unknown置顶(最需要人工盯)。
        """
        st = self.risk_state()
        raw = st.get("attempts", {}) or {}
        codes = list(raw.keys())
        held = {p.get("code") for p in self.positions()}
        quotes = self.quotes(codes)
        events = self.today_events()
        names = self.name_map(events, need=codes)
        order = {"pending": 0, "unknown": 1, "filled": 2, "canceled": 3}
        rows = []
        for code, a in raw.items():
            for act in ("BUY", "SELL"):
                t = a.get(act)
                if not t:
                    continue
                q = quotes.get(code, {}) or {}
                first = float(t.get("price") or 0)
                price = float(q.get("price") or 0)
                diff = ((price - first) / first * 100
                        if first > 0 and price > 0 else None)
                rows.append({
                    "code": code,
                    "name": names.get(code, ""),
                    "action": act,
                    "status": t.get("status", ""),
                    "first_price": first,
                    "ts": t.get("ts", ""),
                    "held": code in held,
                    "price": price,
                    "pct": q.get("pct"),
                    "diff_vs_first": diff,
                    "at_limit_up": bool(q.get("at_limit_up")),
                    "at_limit_down": bool(q.get("at_limit_down")),
                    "sealed_up": bool(q.get("sealed_up")),
                    "sealed_down": bool(q.get("sealed_down")),
                    "halted": bool(q.get("halted")),
                    "source": q.get("source", ""),
                })
        rows.sort(key=lambda r: (order.get(r["status"], 9),
                                 r["code"], r["action"]))
        return {
            "date": st.get("date", ""),
            "kill_switch": st.get("kill_switch", False),
            "buy_halt": st.get("buy_halt", False),
            "order_count": st.get("order_count", 0),
            "rows": rows,
        }


class _Handler(BaseHTTPRequestHandler):
    state: _State = None       # 注入
    server_version = "Awakening/1.0"

    # ---- 基础 ----

    def log_message(self, fmt, *args):   # 静默默认访问日志(走应用日志)
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _csrf_ok(self) -> bool:
        """防跨站POST(本地无鉴权服务)。两道独立防线:

        1. 必须显式Content-Type: application/json——跨站带此头会触发CORS
           预检, 本服务不返回任何CORS允许头, 恶意网页的浏览器预检即失败;
           text/plain/form简单请求一律拒(原漏洞: 恶意网页可直接跨站POST)。
        2. 带Origin/Referer的请求必须同源(127.0.0.1/localhost本端口);
           两者都没有的非浏览器请求(curl/本机进程)放行。
        """
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in ctype:
            log.warning("拒绝非JSON POST %s (Content-Type=%r)",
                        self.path, ctype)
            return False
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin:
            try:
                ol = urlparse(origin)
                if ol.netloc.lower() != host.lower() or ol.scheme != "http":
                    log.warning("拒绝跨源POST %s Origin=%s", self.path, origin)
                    return False
            except ValueError:
                return False
        else:
            ref = self.headers.get("Referer")
            if ref:
                try:
                    rl = urlparse(ref)
                    if rl.netloc.lower() != host.lower():
                        log.warning("拒绝跨源POST %s Referer=%s",
                                    self.path, ref)
                        return False
                except ValueError:
                    return False
        return True

    # ---- 路由 ----

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._static()
            if u.path == "/api/overview":
                return self._json(self.state.overview())
            if u.path == "/api/events":
                kinds = [k for k in q.get("kinds", "").split(",") if k]
                limit = min(int(q.get("limit", 300) or 300), 5000)
                return self._json({
                    "date": q.get("date", time.strftime("%Y%m%d")),
                    "events": self.state.today_events(
                        q.get("date", ""), kinds or None, limit)})
            if u.path == "/api/scan_latest":
                return self._json(self.state.scan_latest())
            if u.path == "/api/verify_rounds":
                return self._json({"rounds": self.state.verify_rounds()})
            if u.path == "/api/verify_round":
                return self._json(self.state.verify_round(q.get("file", "")))
            if u.path == "/api/first_signals":
                return self._json(self.state.first_signals())
            if u.path == "/api/attempts":
                return self._json(self.state.attempts())
            if u.path == "/api/gate":
                return self._json(self.state.gate())
            if u.path == "/api/health":
                return self._json(self.state.health(
                    force=q.get("force", ["0"])[0] == "1"))
            if u.path.startswith("/shots/"):
                return self._shot(u.path[len("/shots/"):])
            if u.path == "/api/alerts":
                return self._json({"alerts": self.state.alerts(
                    min(int(q.get("limit", 100) or 100), 1000))})
            if u.path == "/api/hotlist":
                return self._json({"hot": self.state.hotlist(
                    min(int(q.get("limit", 30) or 30), 200))})
            if u.path == "/api/quotes":
                codes = [c for c in q.get("codes", "").split(",")
                         if len(c) == 6 and c.isdigit()]
                return self._json({"quotes": self.state.quotes(codes)})
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("GET %s异常", u.path)
            return self._json({"error": str(e)[:200]}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._csrf_ok():
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            try:
                self.wfile.write(b'{"error":"cross-origin or non-JSON POST rejected"}')
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = self._read_body()
        try:
            if u.path == "/api/audit/scan":
                side = str(body.get("side", "both"))
                if side not in ("buy", "sell", "both"):
                    return self._json({"error": "side无效"}, 400)
                # 串行采集: xiadan同一时刻只允许一个连接, 防连点双开抢窗口
                if not _AUDIT_SCAN_LOCK.acquire(blocking=False):
                    return self._json(
                        {"error": "上一次对账采集仍在执行, 请稍候"}, 409)
                try:
                    from trader.manual_audit import collect_broker_data
                    data = collect_broker_data(self.state.cfg)
                finally:
                    _AUDIT_SCAN_LOCK.release()
                data["side"] = side
                return self._json(data)
            if u.path == "/api/audit/apply":
                ops = body.get("ops")
                if not isinstance(ops, list) or not ops:
                    return self._json({"error": "ops为空"}, 400)
                if len(ops) > 50:
                    return self._json({"error": "ops过多"}, 400)
                from trader.manual_audit import apply_audit_ops
                return self._json(apply_audit_ops(self.state.cfg, ops))
            if u.path == "/api/preflight/run":
                # 完整自检(连行情端/F12/预热), 约30-60s, 锁内串行
                return self._json(self.state.run_full_preflight())
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            log.exception("POST %s异常", u.path)
            return self._json({"error": str(e)[:200]}, 500)

    def _shot(self, fname: str):
        """标注截图服务(白名单校验+不可变缓存)。"""
        path = self.state.shot_path(fname)
        if not path:
            return self._json({"error": "not found"}, 404)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "read failed"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        # 文件名含秒级时间戳不可变, 浏览器缓存一天(核对页一轮约50MB)
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self):
        path = os.path.join(STATIC_DIR, "index.html")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "index.html缺失"}, 500)
        self._send(200, body, "text/html; charset=utf-8")


def run_web(cfg: AppConfig, port: int = 8899):
    """启动监控台HTTP服务(阻塞, Ctrl+C退出)。"""
    _Handler.state = _State(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    log.info("监控台已启动: http://127.0.0.1:%d  (Ctrl+C退出)", port)
    print(f"监控台已启动: http://127.0.0.1:{port}  (Ctrl+C退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log.info("监控台已停止")
