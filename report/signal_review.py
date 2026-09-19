"""信号质量回溯: 统计历史"多/空"信号发出后N个交易日的实际涨跌。

数据源:
  信号: logs/ 下全部扫描CSV(auto_round_*.csv / pipeline_*.csv / results_*.csv,
        字段见scanner.timing.CSV_FIELDS), 取status=OK的多/空信号
  行情: 腾讯日K接口 web.ifzq.gtimg.cn (免费无key, 前复权)

口径:
  入场基准 = 信号日收盘价; 多头后N日收益>0记胜, 空头后N日收益<0记胜
  (空信号正确=之后下跌)。信号日为最近N个交易日内、尚无后续K线的样本
  计入"待观察"不计胜率。

用法:
    python -m report.signal_review                # 全部档案, 后1/3/5日
    python -m report.signal_review --hold 1 3 5   # 自定义持有交易日
    python -m report.signal_review --export       # 明细导出CSV
"""
import argparse
import csv
import glob
import json
import logging
import os
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")
log = logging.getLogger("signal_review")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
KLINE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


def market_prefix(code: str) -> str:
    """6位代码 -> 腾讯行情市场前缀。北交所(8/4开头)接口不支持, 返回''。"""
    if code.startswith(("60", "68", "51", "11", "5")):
        return "sh"
    if code.startswith(("00", "30", "12", "15", "16")):
        return "sz"
    return ""


def load_signals(logs_dir: str) -> list:
    """扫描logs下所有结果CSV, 提取多/空信号(去重: 同code同日同信号取最高置信度)。"""
    best = {}   # (code, date, signal) -> (confidence, source_file)
    patterns = ["auto_round_*.csv", "pipeline_*.csv", "results_*.csv",
                "scan_*.csv"]
    files = []
    for pat in patterns:
        files += glob.glob(os.path.join(logs_dir, pat))
    log.info("发现结果CSV %d 个", len(files))
    for path in files:
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    sig = (row.get("signal") or "").strip()
                    if sig not in ("多", "空"):
                        continue
                    if (row.get("status") or "").strip() != "OK":
                        continue
                    code = (row.get("stock_code") or "").strip()
                    if len(code) != 6 or not code.isdigit():
                        continue
                    date = (row.get("start_time") or "")[:10]
                    if not date:
                        continue
                    try:
                        conf = float(row.get("detection_confidence") or 0)
                    except ValueError:
                        conf = 0.0
                    key = (code, date, sig)
                    if key not in best or conf > best[key][0]:
                        best[key] = (conf, os.path.basename(path))
        except Exception as e:
            log.warning("读取%s失败: %s", path, e)
    signals = [{"code": c, "date": d, "signal": s, "confidence": cf,
                "source": src} for (c, d, s), (cf, src) in best.items()]
    signals.sort(key=lambda x: (x["date"], x["code"]))
    log.info("提取去重信号 %d 条 (多%d 空%d)", len(signals),
             sum(1 for s in signals if s["signal"] == "多"),
             sum(1 for s in signals if s["signal"] == "空"))
    return signals


_kline_cache = {}


def fetch_kline(code: str, limit: int = 60) -> list:
    """拉取前复权日K, 返回 [(date, close), ...] 按日期升序; 失败[]。"""
    if code in _kline_cache:
        return _kline_cache[code]
    mk = market_prefix(code)
    if not mk:
        log.warning("%s 非沪深代码(北交所接口不支持), 跳过", code)
        _kline_cache[code] = []
        return []
    symbol = f"{mk}{code}"
    try:
        r = requests.get(KLINE_URL,
                         params={"param": f"{symbol},day,,,{limit},qfq"},
                         headers=KLINE_HEADERS, timeout=10)
        d = r.json()
        node = (d.get("data") or {}).get(symbol) or {}
        rows = node.get("qfqday") or node.get("day") or []
        kl = [(row[0], float(row[2])) for row in rows if len(row) >= 3]
        _kline_cache[code] = kl
        time.sleep(0.15)                # 温和限速
        return kl
    except Exception as e:
        log.warning("拉取%s日K失败: %s", code, e)
        _kline_cache[code] = []
        return []


def evaluate(signals: list, holds: tuple) -> list:
    """对每条信号计算后N个交易日收益(%)。"""
    out = []
    for s in signals:
        kl = fetch_kline(s["code"])
        if not kl:
            continue
        dates = [d for d, _ in kl]
        closes = [c for _, c in kl]
        # 信号日: 取<=信号日期的最后一根K线(盘中信号=当日K线)
        idx = -1
        for i, d in enumerate(dates):
            if d <= s["date"]:
                idx = i
            else:
                break
        if idx < 0:
            continue
        rec = dict(s)
        rec["entry_close"] = closes[idx]
        rec["kline_date"] = dates[idx]
        for n in holds:
            j = idx + n
            key = f"ret_{n}d"
            if j < len(closes) and closes[idx] > 0:
                rec[key] = round((closes[j] / closes[idx] - 1) * 100, 2)
                rec[f"future_{n}d_date"] = dates[j]
            else:
                rec[key] = None       # 尚未走到, 待观察
        out.append(rec)
    return out


def report(records: list, holds: tuple, export: str = ""):
    """按信号方向x持有期汇总胜率与平均收益。"""
    print("\n========== 信号质量回溯 ==========")
    for direction in ("多", "空"):
        recs = [r for r in records if r["signal"] == direction]
        if not recs:
            print(f"\n【{direction}信号】无样本")
            continue
        print(f"\n【{direction}信号】样本 {len(recs)} 条"
              f"({'后涨=胜' if direction == '多' else '后跌=胜'})")
        print(f"{'持有期':<8}{'可评估':>6}{'待观察':>6}{'胜率':>8}"
              f"{'平均收益%':>10}{'最好%':>8}{'最差%':>8}")
        for n in holds:
            vals = [r[f"ret_{n}d"] for r in recs if r.get(f"ret_{n}d") is not None]
            pending = len(recs) - len(vals)
            if not vals:
                print(f"{n}个交易日{'':<2}{0:>6}{pending:>6}{'--':>8}")
                continue
            if direction == "多":
                wins = sum(1 for v in vals if v > 0)
            else:
                wins = sum(1 for v in vals if v < 0)
            win_rate = wins / len(vals) * 100
            avg = sum(vals) / len(vals)
            print(f"{n}个交易日{'':<2}{len(vals):>6}{pending:>6}"
                  f"{win_rate:>7.1f}%{avg:>10.2f}{max(vals):>8.2f}"
                  f"{min(vals):>8.2f}")
    # 最近信号明细
    print("\n------ 最近20条信号 ------")
    print(f"{'信号日':<12}{'代码':<8}{'方向':<5}{'置信度':>6}"
          + "".join(f"{f'{n}日%':>8}" for n in holds))
    for r in records[-20:]:
        line = f"{r['kline_date']:<12}{r['code']:<8}{r['signal']:<5}{r['confidence']:>6.2f}"
        for n in holds:
            v = r.get(f"ret_{n}d")
            line += f"{(v if v is not None else '待观察'):>8}"
        print(line)
    if export:
        with open(export, "w", encoding="utf-8-sig", newline="") as f:
            fields = ["date", "code", "signal", "confidence", "entry_close",
                      "kline_date", "source"]
            fields += [f"ret_{n}d" for n in holds]
            fields += [f"future_{n}d_date" for n in holds]
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        print(f"\n明细已导出: {export}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"))
    ap.add_argument("--hold", type=int, nargs="+", default=[1, 3, 5],
                    help="持有交易日列表, 默认1 3 5")
    ap.add_argument("--export", action="store_true", help="导出明细CSV")
    args = ap.parse_args()

    signals = load_signals(args.logs)
    if not signals:
        print("未发现任何多/空信号档案(先跑几轮扫描)")
        return
    records = evaluate(signals, tuple(sorted(set(args.hold))))
    export_path = ""
    if args.export:
        export_path = os.path.join(
            args.logs, f"signal_review_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    report(records, tuple(sorted(set(args.hold))), export_path)


if __name__ == "__main__":
    main()
