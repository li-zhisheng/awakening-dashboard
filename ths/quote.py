"""实时行情: 腾讯qt.gtimg.cn(主) + 新浪hq.sinajs.cn(备) 双源冗余。

腾讯返回GBK编码的 ~ 分隔字符串, 关键字段(0基下标):
  f[1]=名称  f[2]=代码  f[3]=现价  f[4]=昨收  f[5]=今开
  f[9]=买一价 f[10]=买一量(手)  f[19]=卖一价 f[20]=卖一量(手)
  f[32]=涨幅% f[33]=最高 f[34]=最低
  f[47]=涨停价 f[48]=跌停价
新浪返回GBK逗号分隔(需Referer), 下标:
  0名称 1今开 2昨收 3现价 4最高 5最低 6买一价 7卖一价 8成交量(股)
  10买一量(股) 11买一价 20卖一量(股) 21卖一价 30日期 31时间
  (新浪无涨跌停字段, 按昨收±10%(ST±5%)计算)

失败容错: 双源都失败返回None, 调用方降级处理, 不阻断交易。
停牌: 停牌股两源现价均为0(昨收有值), 返回halted=True由决策层拦截。
"""
import logging
import time

import requests

log = logging.getLogger("quote")

URL_TX = "http://qt.gtimg.cn/q={sym}"
HEADERS_TX = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
URL_SINA = "https://hq.sinajs.cn/list={sym}"
HEADERS_SINA = {"User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/"}

# 复用TCP连接(封单监控每10s轮询 + 决策层快照, 避免每次新建连接增加延迟)
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0"})


def _market(code: str) -> str:
    if code.startswith(("60", "68", "51", "11", "5")):
        return "sh"
    if code.startswith(("00", "30", "12", "15", "16")):
        return "sz"
    return ""


def _f(f, i, default=0.0):
    try:
        return float(f[i])
    except (IndexError, ValueError):
        return default


def _limit_prices(prev_close: float, name: str = ""):
    """按昨收计算涨跌停价(主板10%/ST5%); 交易所四舍五入到分。"""
    if prev_close <= 0:
        return 0.0, 0.0
    ratio = 0.05 if "ST" in (name or "").upper() else 0.10

    def _r(x):
        return int(x * 100 + 0.5) / 100.0

    return _r(prev_close * (1 + ratio)), _r(prev_close * (1 - ratio))


def _in_call_auction() -> bool:
    """当前是否处于集合竞价时段(9:15-9:30)。

    此期间现价可能为0(连续竞价未开始), 不能据此判定停牌。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")
    return "09:15:00" <= now <= "09:30:00"


def _parse_hms_ts(date_s: str, time_s: str) -> float:
    """新浪 f30(YYYY-MM-DD)+f31(HH:MM:SS) -> epoch秒, 解析失败返回0。"""
    try:
        return time.mktime(time.strptime(
            f"{date_s.strip()} {time_s.strip()}", "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OSError):
        return 0.0


def _parse_compact_ts(s: str) -> float:
    """腾讯 f30 紧凑时间(YYYYMMDDHHMMSS, 可能带毫秒尾) -> epoch秒, 失败0。"""
    s = (s or "").strip()
    if len(s) < 14 or not s[:14].isdigit():
        return 0.0
    try:
        return time.mktime(time.strptime(s[:14], "%Y%m%d%H%M%S"))
    except (ValueError, OSError):
        return 0.0


def _build_snapshot(name, price, prev, opn, high, low, pct,
                    bid1, bid1v, ask1, ask1v,
                    limit_up, limit_down, source,
                    quote_ts: float = 0.0, local_ts: float = 0.0):
    """统一组装快照(两源共用), 含涨跌停/封板/停牌判定。"""
    eps = 0.005   # 价格比较容差(半分钱)
    # 停牌: 现价0且昨收有值; 或连续竞价时段无任何成交(开/高/低全0且价=昨收)
    # 集合竞价(9:15-9:30)现价可能为0属正常, 不判停牌
    if _in_call_auction():
        halted = False
    else:
        halted = (price <= 0 < prev) or (
            price > 0 and opn <= 0 and high <= 0 and low <= 0
            and abs(price - prev) <= eps)
    at_up = limit_up > 0 and price >= limit_up - eps
    at_dn = limit_down > 0 and price <= limit_down + eps
    # 封死涨停: 卖一价/量为空(无卖盘); 封死跌停: 买一价/量为空(无买盘)
    sealed_up = at_up and ask1 <= 0
    sealed_dn = at_dn and bid1 <= 0
    return {
        "name": name or "",
        "price": price,
        "prev_close": prev,
        "open": opn,
        "pct": pct,
        "high": high,
        "low": low,
        "bid1": bid1, "bid1_vol": bid1v,
        "ask1": ask1, "ask1_vol": ask1v,
        "limit_up": limit_up, "limit_down": limit_down,
        "at_limit_up": at_up,
        "at_limit_down": at_dn,
        "sealed_up": sealed_up,
        "sealed_down": sealed_dn,
        # 封单量(手): 涨停看买一, 跌停看卖一
        "seal_vol": bid1v if sealed_up else (ask1v if sealed_dn else 0.0),
        "halted": halted,
        "source": source,
        # 行情源成交时刻(解析失败回退本机接收时刻)与本机接收时刻,
        # ClockGuard据此检测本机时钟漂移/行情陈旧(只告警)
        "quote_ts": quote_ts or local_ts,
        "local_ts": local_ts,
    }


def _tencent_quote(sym: str, timeout: float):
    r = _session.get(URL_TX.format(sym=sym), headers=HEADERS_TX,
                     timeout=timeout)
    r.encoding = "gbk"
    body = r.text
    if '="' not in body:
        return None
    payload = body.split('="', 1)[1].rstrip('";\n ')
    f = payload.split("~")
    if len(f) < 5:
        return None
    name = f[1] if len(f) > 1 else ""
    prev = _f(f, 4)
    local_ts = time.time()
    return _build_snapshot(
        name=name, price=_f(f, 3), prev=prev, opn=_f(f, 5),
        high=_f(f, 33), low=_f(f, 34), pct=_f(f, 32),
        bid1=_f(f, 9), bid1v=_f(f, 10),
        ask1=_f(f, 19), ask1v=_f(f, 20),
        limit_up=_f(f, 47), limit_down=_f(f, 48),
        source="tencent",
        quote_ts=_parse_compact_ts(f[30] if len(f) > 30 else ""),
        local_ts=local_ts)


def _sina_quote(sym: str, timeout: float):
    r = _session.get(URL_SINA.format(sym=sym), headers=HEADERS_SINA,
                     timeout=timeout)
    r.encoding = "gbk"
    body = r.text
    if '="' not in body:
        return None
    payload = body.split('="', 1)[1].rstrip('";\n ')
    f = payload.split(",")
    # 无效代码/盘前无数据: 载荷为空(名称都没有)
    if len(f) < 32 or not f[0]:
        return None
    name = f[0]
    prev = _f(f, 2)
    price = _f(f, 3)
    limit_up, limit_down = _limit_prices(prev, name)
    pct = ((price - prev) / prev) * 100 if prev > 0 and price > 0 else 0.0
    local_ts = time.time()
    return _build_snapshot(
        name=name, price=price, prev=prev, opn=_f(f, 1),
        high=_f(f, 4), low=_f(f, 5), pct=pct,
        bid1=_f(f, 11), bid1v=_f(f, 10) / 100.0,   # 股→手
        ask1=_f(f, 21), ask1v=_f(f, 20) / 100.0,
        limit_up=limit_up, limit_down=limit_down,
        source="sina",
        quote_ts=_parse_hms_ts(f[30] if len(f) > 30 else "",
                               f[31] if len(f) > 31 else ""),
        local_ts=local_ts)


_clock_guard = None


def set_clock_guard(guard) -> None:
    """注入配置化ClockGuard(阈值/开关来自config); 不注入则用默认30s/120s。"""
    global _clock_guard
    _clock_guard = guard


def _clock_check(snap: dict):
    global _clock_guard
    if _clock_guard is None:
        try:
            from monitor.clock_guard import ClockGuard
            _clock_guard = ClockGuard()
        except Exception:
            return
    try:
        _clock_guard.check(snap)
    except Exception:
        pass


def realtime_quote(code: str, timeout: float = 6.0) -> dict:
    """返回完整行情快照; 双源均失败返回None。

    入参支持6位股票代码(600487)或带市场前缀代码(sh000001, 用于指数)。
    {name, price, prev_close, open, pct, high, low,
     bid1, bid1_vol, ask1, ask1_vol, limit_up, limit_down,
     at_limit_up, at_limit_down, sealed_up, sealed_down, seal_vol,
     halted, source}
    价量单位: 元 / 手。
    """
    import re
    m = re.fullmatch(r"(sh|sz)(\d{6})", code or "")
    if m:
        sym = code
    else:
        mk = _market(code)
        if not mk:
            return None
        sym = f"{mk}{code}"
    try:
        snap = _tencent_quote(sym, timeout)
        if snap is not None:
            _clock_check(snap)
        return snap
    except Exception as e:
        log.warning("腾讯行情查询失败 %s, 切新浪源: %s", code, e)
    try:
        q = _sina_quote(sym, timeout)
        if q is not None:
            _clock_check(q)
            log.info("新浪备用源成功 %s(%s 现价%.2f)", code,
                     q["name"], q["price"])
        return q
    except Exception as e:
        log.warning("新浪行情查询也失败 %s: %s", code, e)
        return None


def realtime_price(code: str, timeout: float = 6.0) -> float:
    """现价; 失败或停牌返回0.0。"""
    q = realtime_quote(code, timeout)
    return q["price"] if q else 0.0


def price_compare_text(code: str, signal_price: float) -> str:
    """生成告警用价格对比文本: '信号时价X.XX → 现价Y.YY(+Z.ZZ%)'。

    任一价格缺失都降级展示, 不报错。
    """
    parts = []
    if signal_price and signal_price > 0:
        parts.append(f"信号时价 {signal_price:.2f}")
    else:
        parts.append("信号时价获取失败")
    cur = realtime_price(code)
    if cur > 0:
        if signal_price and signal_price > 0:
            pct = (cur - signal_price) / signal_price * 100
            parts.append(f"现价 {cur:.2f}({pct:+.2f}%)")
        else:
            parts.append(f"现价 {cur:.2f}")
    else:
        parts.append("现价获取失败(可能停牌)")
    return " → ".join(parts)
