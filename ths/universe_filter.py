"""交易标的过滤: 热榜拉取后、云同步前剔除不可交易股票。

规则(全部可在config.yaml universe段开关):
  1. 沪深主板: 代码60/00开头(600/601/603/605沪主板, 000/001/002/003深主板);
     排除688/689科创板、300/301创业板、8·4·920北交所
  2. 非ST: 名称含"ST"(*ST/SST/ST)一律剔除
  3. 非次新: 上市以来日K根数 < subnew_min_bars(默认250≈1年)视为次新剔除

时效性设计:
  - 规则1/2纯代码/名称判断, 零成本
  - 规则3需日K根数(腾讯接口), 每日缓存到 logs/universe_cache.json:
    当天首轮并行查询(8线程约3-5s), 后续轮次直接读缓存, 零网络开销
  - 查询失败的股票默认放行(宁可多扫不可漏判), 记日志
  - 持仓股不过滤(保证卖出链路始终可扫, 由调用方并入held)
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("universe_filter")

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
KLINE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def is_main_board(code: str) -> bool:
    """沪深主板: 60开头(沪) / 00开头(深, 含原中小板002)。"""
    return code.startswith(("60", "00"))


def is_st(name: str) -> bool:
    """名称含ST(覆盖ST/*ST/SST); 名称缺失时按非ST处理(热榜正常都带名称)。"""
    return "ST" in (name or "").upper()


def market_prefix(code: str) -> str:
    """6位代码 -> 腾讯行情市场前缀; 非沪深返回''。"""
    if code.startswith(("60", "68", "51", "11", "5")):
        return "sh"
    if code.startswith(("00", "30", "12", "15", "16")):
        return "sz"
    return ""


def fetch_kline_bar_count(code: str, timeout: float = 8.0) -> int:
    """日K根数(上市以来交易日数); 失败返回-1(调用方放行)。

    count给大值(2000)取全部历史, 返回实际根数; 次新股根数明显少。
    """
    mk = market_prefix(code)
    if not mk:
        return -1
    symbol = f"{mk}{code}"
    try:
        r = requests.get(KLINE_URL,
                         params={"param": f"{symbol},day,,,2000,qfq"},
                         headers=KLINE_HEADERS, timeout=timeout)
        d = r.json()
        node = (d.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        return len(rows)
    except Exception as e:
        log.warning("日K根数查询失败 %s: %s", code, e)
        return -1


class SubNewCache:
    """次新判定每日缓存: {date, bars:{code: 根数}}。"""

    def __init__(self, path: str, ttl_hours: float = 20.0):
        self.path = path
        self.ttl_seconds = ttl_hours * 3600
        self.bars = {}
        self._date = ""
        self._load()

    def _load(self):
        try:
            if not os.path.isfile(self.path):
                return
            if time.time() - os.path.getmtime(self.path) > self.ttl_seconds:
                return
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._date = d.get("date", "")
            self.bars = d.get("bars", {})
        except Exception as e:
            log.warning("次新缓存读取失败: %s", e)
            self.bars = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"date": time.strftime("%Y-%m-%d"),
                           "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "bars": self.bars}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("次新缓存写入失败: %s", e)

    def filter_subnew(self, codes: list, min_bars: int,
                      workers: int = 8) -> set:
        """返回次新股代码集合。codes中未缓存的并行查询后更新缓存。"""
        today = time.strftime("%Y-%m-%d")
        if self._date != today:
            self.bars = {}      # 跨交易日清空重查
            self._date = today
        missing = [c for c in codes if c not in self.bars]
        if missing:
            log.info("次新判定: %d只待查日K根数(并行%d线程)", len(missing),
                     workers)
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                counts = list(ex.map(fetch_kline_bar_count, missing))
            for code, cnt in zip(missing, counts):
                if cnt >= 0:
                    self.bars[code] = cnt
                # cnt=-1查询失败: 不写缓存(下轮重试), 本次放行
            self._save()
            log.info("次新判定完成: 查询%d只 耗时%.1fs", len(missing),
                     time.time() - t0)
        subnew = set()
        for c in codes:
            cnt = self.bars.get(c, -1)
            if 0 <= cnt < min_bars:
                subnew.add(c)
        return subnew


def filter_hot_stocks(stocks, cfg, held_codes: list = None) -> tuple:
    """过滤热榜股票, 返回 (tradeable_codes顺序保留, name_map, excluded列表)。

    stocks: ths.fetcher.HotStock列表(code/name/rank)。
    held_codes: 持仓股代码, 始终保留(不过滤), 保证卖出链路可扫。
    excluded: [(code, name, reason), ...] 供日志/复盘。
    """
    u = cfg.universe
    name_map = {s.code: s.name for s in stocks}
    held = set(held_codes or [])
    tradeable = []
    excluded = []

    if not u.enable:
        return [s.code for s in stocks], name_map, excluded

    # 规则1+2: 零成本预判
    pending_subnew = []
    for s in stocks:
        code, name = s.code, s.name
        if code in held:
            tradeable.append(code)
            continue
        if u.main_board_only and not is_main_board(code):
            excluded.append((code, name, "非沪深主板"))
            continue
        if u.exclude_st and is_st(name):
            excluded.append((code, name, "ST"))
            continue
        pending_subnew.append(code)

    # 规则3: 次新(需日K根数, 每日缓存)
    if u.exclude_subnew and pending_subnew:
        cache_path = cfg.resolve(u.cache_file)
        cache = SubNewCache(cache_path, ttl_hours=u.cache_ttl_hours)
        subnew = cache.filter_subnew(pending_subnew, u.subnew_min_bars)
        for code in pending_subnew:
            if code in subnew:
                excluded.append((code, name_map.get(code, ""),
                                 f"次新(日K<{u.subnew_min_bars}根)"))
            else:
                tradeable.append(code)
    else:
        tradeable.extend(pending_subnew)

    # 保持热榜顺序
    order = {s.code: i for i, s in enumerate(stocks)}
    tradeable.sort(key=lambda c: order.get(c, 999))
    return tradeable, name_map, excluded
