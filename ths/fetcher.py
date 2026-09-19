"""同花顺热榜拉取 (PC侧, 不占手机)。

端点: dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock
必须携带 Referer/Origin 否则 status_code=-1 (实测)。
API 失败时回退读本地热榜文件, 保证扫描不中断。
"""
import json
import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("fetcher")

URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://eq.10jqka.com.cn/",
    "Origin": "https://eq.10jqka.com.cn",
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class HotStock:
    code: str
    name: str
    rank: int
    heat: float
    change_pct: float


def fetch(top_n: int = 100, timeout: float = 10.0,
          fallback_file: str = "") -> list:
    """拉取热榜前 top_n 只。失败时回退本地文件; 都失败返回空列表。"""
    for attempt in range(1, 4):
        try:
            r = requests.get(URL, params={"stock_type": "a", "type": "hour",
                                          "list_type": "normal"},
                             headers=HEADERS, timeout=timeout)
            d = r.json()
            if str(d.get("status_code")) != "0":
                raise RuntimeError(f"status_code={d.get('status_code')}")
            items = (d.get("data") or {}).get("stock_list") or []
            stocks = [HotStock(code=str(it["code"]).zfill(6),
                               name=it.get("name", ""),
                               rank=int(it.get("order", i + 1)),
                               heat=float(it.get("rate") or 0),
                               change_pct=float(it.get("rise_and_fall") or 0))
                      for i, it in enumerate(items) if str(it.get("code", "")).isdigit()]
            if not stocks:
                raise RuntimeError("stock_list 为空")
            log.info("热榜API成功: %d 只 (top%d)", len(stocks), top_n)
            return stocks[:top_n]
        except Exception as e:
            log.warning("热榜API第%d次失败: %s", attempt, e)
            time.sleep(1.0)

    if fallback_file and os.path.isfile(fallback_file):
        try:
            with open(fallback_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            stocks = [HotStock(code=str(x["code"]).zfill(6),
                               name=x.get("name", ""), rank=int(x.get("rank", i + 1)),
                               heat=float(x.get("heat") or 0),
                               change_pct=float(x.get("change_pct") or 0))
                      for i, x in enumerate(raw)]
            log.warning("API失败, 使用本地热榜文件: %d 只", len(stocks))
            return stocks[:top_n]
        except Exception as e:
            log.error("本地热榜文件读取失败: %s", e)
    return []


def save_local(stocks: list, path: str):
    """热榜快照落盘 (既是fallback数据源, 也是轮次审计记录)。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"code": s.code, "name": s.name, "rank": s.rank,
                    "heat": s.heat, "change_pct": s.change_pct} for s in stocks],
                  f, ensure_ascii=False, indent=1)

