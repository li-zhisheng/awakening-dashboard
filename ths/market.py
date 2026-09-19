"""市场概貌: 指数行情/全市场涨跌家数/热门板块(日报用, 纯PC HTTP)。

数据源:
- 指数: ths.quote.realtime_quote 双源(腾讯主/新浪备), 上证/深证成指/创业板指
- 涨跌家数: 东方财富 ulist.np 接口, 取上证指数(1.000001)与深证成指(0.399001)
  的 f104/f105/f106(涨/跌/平家数)相加, 单次请求即全市场
- 热门板块: 东方财富 clist 行业板块(m:90 t:2)涨跌幅榜
东财多子域轮试(push2在部分网络下间歇断连); 各部分独立失败降级, 不影响日报发送。
"""
import json
import logging
import urllib.request

log = logging.getLogger("market")

INDEX_CODES = ("sh000001", "sz399001", "sz399006")
_EM_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com",
             "82.push2.eastmoney.com", "83.push2.eastmoney.com")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://quote.eastmoney.com/"}
_BREADTH_PATH = ("/api/qt/ulist.np/get?fltt=2&invt=2"
                 "&fields=f12,f14,f104,f105,f106"
                 "&secids=1.000001,0.399001")
_SECTOR_PATH = ("/api/qt/clist/get?pn=1&pz={n}&po={po}&np=1&fltt=2&invt=2"
                "&fid=f3&fs=m:90+t:2&fields=f3,f14")


def _em_json(path: str, timeout: float = 6.0):
    """多子域轮试; 全失败抛最后一个异常。"""
    last = None
    for host in _EM_HOSTS:
        try:
            req = urllib.request.Request(
                f"http://{host}{path}", headers=_HEADERS)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last = e
    raise last


def market_indices(codes=INDEX_CODES, timeout: float = 5.0):
    """指数快照列表 [{code,name,price,pct}]; 单只失败跳过, 全失败返回[]。"""
    from ths.quote import realtime_quote
    out = []
    for c in codes:
        try:
            q = realtime_quote(c, timeout)
            if q and q.get("price"):
                out.append({"code": c, "name": q.get("name", c),
                            "price": q["price"], "pct": q.get("pct", 0)})
        except Exception as e:
            log.warning("指数行情失败 %s: %s", c, e)
    return out


def market_breadth(timeout: float = 6.0):
    """全A涨跌家数 {up,down,flat,total}(沪深相加); 异常返回None。"""
    try:
        rows = ((_em_json(_BREADTH_PATH, timeout).get("data") or {})
                .get("diff") or [])
        if len(rows) < 2:
            return None
        up = down = flat = 0
        for r in rows:
            up += int(r.get("f104") or 0)
            down += int(r.get("f105") or 0)
            flat += int(r.get("f106") or 0)
        total = up + down + flat
        if total < 100:     # 盘前/接口空口径保护
            return None
        return {"up": up, "down": down, "flat": flat, "total": total}
    except Exception as e:
        log.warning("涨跌家数获取失败: %s", e)
        return None


def hot_sectors(top: int = 5, bottom: int = 0, timeout: float = 6.0):
    """行业板块涨跌幅榜 {'gainers':[(name,pct)...], 'losers':[...]}。"""
    def fetch(po: int, n: int):
        if n <= 0:
            return []
        try:
            rows = ((_em_json(_SECTOR_PATH.format(n=n, po=po), timeout)
                     .get("data") or {}).get("diff") or [])
            return [(r.get("f14", ""), r.get("f3", 0)) for r in rows]
        except Exception as e:
            log.warning("板块榜获取失败(po=%s): %s", po, e)
            return []
    return {"gainers": fetch(1, top), "losers": fetch(0, bottom)}


def market_overview(top_sectors: int = 5, bottom_sectors: int = 0):
    """汇总(各部分独立降级): {indices,breadth,sectors}。"""
    return {
        "indices": market_indices(),
        "breadth": market_breadth(),
        "sectors": hot_sectors(top=top_sectors, bottom=bottom_sectors),
    }


def format_overview(ov: dict, title: str = "大盘") -> list:
    """概貌格式化为日报行(不含标题行)。"""
    lines = []
    idx = ov.get("indices") or []
    if idx:
        lines.append(title + ": " + " | ".join(
            f"{x['name']}{x['pct']:+.2f}%" for x in idx))
    b = ov.get("breadth")
    if b:
        lines.append(
            f"全A {b['total']}只: 涨{b['up']} 跌{b['down']} 平{b['flat']}")
    sec = ov.get("sectors") or {}
    g = sec.get("gainers") or []
    if g:
        lines.append("领涨板块: " + " ".join(f"{n}{v:+.2f}%" for n, v in g))
    l = sec.get("losers") or []
    if l:
        lines.append("领跌板块: " + " ".join(f"{n}{v:+.2f}%" for n, v in l))
    return lines
