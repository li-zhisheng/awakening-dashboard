"""人工操作主动对账: 人工买卖后点按钮触发, 校验并同步系统持仓。

用法 (双击bat或命令行, 不需要手机, 可在auto_round运行时使用):
    python main.py manual_audit --side buy    # 人工买入后点击
    python main.py manual_audit --side sell   # 人工卖出后点击
    python main.py manual_audit --side both   # 不确定方向时全查

流程:
  1. 读系统事实源: positions.json + 当日系统交易事件(logs/events_*.jsonl)
  2. 连xiadan读券商当日成交(失败降级当日委托中已成交行), 筛出系统无
     交易记录的成交 => 疑似人工操作
  3. 逐条报告: "您于HH:MM:SS人工买入 光迅科技(002281) 1000股 @25.30",
     用户回车确认或输入正确的股票名称/代码做进一步校验
  4. 确认后同步positions.json(补录建仓/移除清仓) + 审计事件 + 手机推送
  5. 关闭xiadan窗口恢复热键就绪(委托窗开着F1-F4热键不生效)

边界: 网格读取可能触发"拷贝验证码"弹窗, 用户在场(刚点的按钮)直接人工
输入即可; 查询链路失败只报告不改动持仓, 绝不凭不完整数据修positions。
"""
import logging
import os
import time

from config import AppConfig
from models.audit import get_event_log
from models.positions import PositionStore

log = logging.getLogger("manual_audit")


def _pick(row: dict, *keys, default=""):
    """网格行字段容错取值: 同花顺各版本列名可能有差异。"""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return default


def _code6(v) -> str:
    """提取6位数字代码(网格里可能是'002281'或带前后缀)。"""
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _norm_trade(row: dict) -> dict | None:
    """当日成交行 -> {code,name,side,qty,price,t}; 无法解析返回None。"""
    code = _code6(_pick(row, "证券代码", "证券代码 ", "stock_code"))
    if not code:
        return None
    op = str(_pick(row, "操作", "业务名称", "买卖标志"))
    side = "BUY" if "买" in op else ("SELL" if "卖" in op else "")
    if not side:
        return None
    return {"code": code, "name": str(_pick(row, "证券名称", "证券简称")),
            "side": side,
            "qty": int(_num(_pick(row, "成交数量", default=0))),
            "price": _num(_pick(row, "成交价格", "成交均价")),
            "t": str(_pick(row, "成交时间", "发生时间", "委托时间"))}


def _norm_entrust(row: dict) -> dict | None:
    """当日委托行(只取已成交部分) -> 成交记录(无成交时间用委托时间)。"""
    t = _norm_trade(row)
    if t is None:
        return None
    t["qty"] = int(_num(_pick(row, "成交数量", default=0)))
    t["price"] = _num(_pick(row, "委托价格"))
    t["t"] = str(_pick(row, "委托时间", "下单时间")) + "(委托)"
    return t if t["qty"] > 0 else None


def _norm_position(row: dict) -> dict | None:
    """持仓网格行 -> {code,name,qty,cost}; 空仓行剔除。"""
    code = _code6(_pick(row, "证券代码"))
    if not code:
        return None
    qty = int(_num(_pick(row, "可用数量", "股票余额", "库存数量",
                         "持仓数量", default=0)))
    if qty <= 0:
        return None
    return {"code": code, "name": str(_pick(row, "证券名称")),
            "qty": qty, "cost": _num(_pick(row, "成本价", "摊薄成本价"))}


def _norm_open_entrust(row: dict) -> dict | None:
    """当日委托网格中的未完成委托 -> {code,name,side,qty,filled,price,t}。

    已撤单/已成完的剔除; 列名缺失无法判定时保守视为未完成(宁可多熔断
    让人工看一眼, 不可漏掉崩溃前遗留的活单)。
    """
    code = _code6(_pick(row, "证券代码", "stock_code"))
    if not code:
        return None
    # 状态列各版本叫法不一: 状态/备注/信息; 含"撤"=已废单
    status_txt = str(_pick(row, "状态", "备注", "信息", "委托状态"))
    if "撤" in status_txt or "废" in status_txt:
        return None
    op = str(_pick(row, "操作", "业务名称", "买卖标志"))
    side = "BUY" if "买" in op else ("SELL" if "卖" in op else "")
    if not side:
        return None
    qty = int(_num(_pick(row, "委托数量", default=0)))
    filled = int(_num(_pick(row, "成交数量", default=0)))
    if qty <= 0 or filled >= qty:
        return None
    return {"code": code, "name": str(_pick(row, "证券名称", "证券简称")),
            "side": side, "qty": qty, "filled": filled,
            "price": _num(_pick(row, "委托价格")),
            "t": str(_pick(row, "委托时间", "下单时间")),
            "status": status_txt}


def _system_traded_codes(el) -> set:
    """当日系统自己交易过的代码集合(事件日志kind=trade, 对账基准)。"""
    codes = set()
    for evt in el.read_day():
        if evt.get("kind") != "trade":
            continue
        c = _code6(evt.get("code", ""))
        if c:
            codes.add(c)
    return codes


def _connect_xiadan(cfg: AppConfig):
    """复用HotkeyTrader的xiadan连接链路(托盘恢复+显式主窗定位)。

    返回 (user, trader) 或 (None, None); user为easytrader实例。
    """
    try:
        from trader.hotkey_trader import HotkeyTrader
        from trader.risk_control import RiskController
        trader = HotkeyTrader(cfg, RiskController(
            cfg.risk, cfg.project_root, positions_file=cfg.positions.file))
        trader._connect_xiadan()
        return trader._td_user, trader
    except Exception as e:
        log.error("xiadan连接失败: %s", e)
        return None, None


def collect_broker_data(cfg: AppConfig) -> dict:
    """采集对账基准数据(采集完即关窗恢复热键), 全程容错。

    返回 {ok, error?, trades, broker_positions, system_positions,
          system_traded, errors{trades?, positions?}}:
      trades            券商当日成交(降级当日委托已成交行), 归一化列表
      broker_positions  券商当前持仓 {code: {code,name,qty,cost}}
      system_positions  系统持仓快照(positions.json)
      system_traded     当日系统自己交易过的代码(事件日志)
    ok=False表示xiadan连接失败(其余字段缺省)。
    """
    el = get_event_log(cfg.resolve(cfg.paths.logs_dir))
    store = PositionStore(cfg.resolve(cfg.positions.file))
    user, trader = _connect_xiadan(cfg)
    if user is None:
        return {"ok": False,
                "error": "无法连接同花顺交易端(xiadan), 请先打开/登录交易客户端"}
    data = {"ok": True, "error": "", "trades": [], "broker_positions": {},
            "system_positions": store.snapshot(),
            "system_traded": sorted(_system_traded_codes(el)),
            "open_entrusts": [], "errors": {}}
    try:
        trades, err = _fetch_trades(user)
        data["trades"] = trades
        if err:
            data["errors"]["trades"] = err
        pos, perr = _fetch_positions(user)
        data["broker_positions"] = pos
        if perr:
            data["errors"]["positions"] = perr
        opens, oerr = _fetch_open_entrusts(user)
        data["open_entrusts"] = opens
        if oerr:
            data["errors"]["entrusts"] = oerr
    finally:
        try:
            trader._close_xiadan()
        except Exception as e:
            log.warning("关闭xiadan窗口失败: %s", e)
    return data


def apply_audit_ops(cfg: AppConfig, ops: list) -> dict:
    """应用页面/CLI确认的对账操作(系统边界, 逐字段校验)。

    op: {"op": "add"|"remove", "code", "name", "price", "qty", "t",
         "detail"}。返回 {added, removed, failed}; 有变更时推送手机。
    """
    added, removed, failed = [], [], []
    store = PositionStore(cfg.resolve(cfg.positions.file))
    el = get_event_log(cfg.resolve(cfg.paths.logs_dir))
    for op in ops or []:
        try:
            if not isinstance(op, dict):
                raise ValueError("op格式错误")
            code = _code6(op.get("code"))
            kind = op.get("op")
            if not code or kind not in ("add", "remove"):
                raise ValueError("op/code无效")
            name = str(op.get("name") or "")[:20]
            price = min(max(_num(op.get("price")), 0.0), 100000.0)
            qty = int(min(max(_num(op.get("qty")), 0.0), 10 ** 9))
            t = str(op.get("t") or "")[:20]
            detail = str(op.get("detail") or "人工对账确认")[:100]
            if kind == "add":
                note = (f"人工对账 {t} {qty}股" if qty
                        else f"人工对账 {t}".strip())
                if not store.add(code, name=name, entry_price=price,
                                 note=note):
                    failed.append({"code": code,
                                   "error": "系统已持有, 不重复录入"})
                    continue
                added.append(f"{name}({code})")
            else:
                pos = store.remove(code)
                if pos is None:
                    failed.append({"code": code, "error": "系统无该持仓记录"})
                    continue
                removed.append(f"{pos.name}({code})")
            el.log("trade", stage="manual_audit",
                   action="BUY" if kind == "add" else "SELL",
                   code=code, name=name, qty=qty, price=price,
                   trade_time=t, detail=detail)
        except Exception as e:
            failed.append({"code": str((op or {}).get("code", "")),
                           "error": str(e)[:100]})
    result = {"added": added, "removed": removed, "failed": failed}
    if added or removed:
        lines = [f"补录建仓{len(added)}只: {added or '无'}",
                 f"移除记录{len(removed)}只: {removed or '无'}"]
        try:
            from notify.notifier import Notifier
            notifier = Notifier(cfg)
            if notifier.enabled():
                notifier.send("人工对账完成(页面确认)", "\n".join(lines))
        except Exception as e:
            log.warning("对账结果推送失败: %s", e)
    return result


def run_manual_audit(cfg: AppConfig, side: str = "both") -> bool:
    """执行人工对账主流程(CLI交互版), 返回是否正常完成。"""
    side = side if side in ("buy", "sell", "both") else "both"
    logs_dir = cfg.resolve(cfg.paths.logs_dir)
    el = get_event_log(logs_dir)
    store = PositionStore(cfg.resolve(cfg.positions.file))
    sys_codes = _system_traded_codes(el)

    print("=" * 62)
    print(f"人工操作对账 ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"系统持仓{len(store.codes())}只: {store.codes()}  "
          f"今日系统交易过: {sorted(sys_codes) or '无'}")
    print("=" * 62)

    data = collect_broker_data(cfg)
    if not data["ok"]:
        print(f"错误: {data['error']}")
        return False

    applied = {"added": [], "removed": []}
    trades = data["trades"]
    err = data["errors"].get("trades", "")
    broker_pos = data["broker_positions"]
    pos_err = data["errors"].get("positions", "")

    # ---- 1) 当日成交中的"系统外"操作 (对账主体) ----
    if err:
        print(f"\n[提示] 当日成交读取异常: {err}")
        print("       将仅按券商持仓做差异校验, 成交明细无法核对")
    want = ("BUY", "SELL") if side == "both" else (side.upper(),)
    manual = [t for t in trades
              if t["side"] in want and t["code"] not in sys_codes]
    if manual:
        print(f"\n发现{len(manual)}笔系统无交易记录的当日成交"
              f"(疑似人工操作), 逐条确认:")
        for i, t in enumerate(manual, 1):
            held = store.get(t["code"])
            state = f"系统已记录持仓(自{held.entry_time})" if held \
                else "系统未记录该股持仓"
            print(f"\n[{i}] {_fmt_trade(t)}  |  {state}")
            _confirm_one(t, held, store, el, applied)
    else:
        side_txt = {"buy": "买入", "sell": "卖出"}.get(side, "买卖")
        print(f"\n当日成交中未发现系统外{side_txt}记录"
              + ("" if not err else "(成交明细读取失败除外)"))

    # ---- 2) 持仓差异校验 (与成交互补: 隔日/未成交挂单也能发现) ----
    if pos_err:
        print(f"\n[提示] {pos_err}, 跳过持仓差异校验")
    else:
        extra = [c for c in broker_pos if c not in store.codes()]
        missing = [c for c in store.codes() if c not in broker_pos]
        for c in extra:
            p = broker_pos[c]
            print(f"\n[持仓差异] 券商有持仓但系统未记录: "
                  f"{p['name']}({c}) {p['qty']}股")
            _confirm_add_position(store, el, applied, c, p["name"],
                                  p["cost"], time.strftime("%H:%M:%S"),
                                  f"{p['qty']}股")
        for c in missing:
            pos = store.get(c)
            print(f"\n[持仓差异] 系统记录持仓但券商无持仓: "
                  f"{pos.name}({c})(自{pos.entry_time}, 疑似人工卖出)")
            ans = input("    移除系统持仓记录? [y=移除 / n=保留]: ").strip()
            if ans == "y":
                removed = store.remove(c)
                if removed:
                    applied["removed"].append(f"{pos.name}({c})")
                    el.log("trade", stage="manual_audit", action="SELL",
                           code=c, name=pos.name,
                           detail="人工对账确认移除(券商无持仓)")
                    print(f"    已移除 {pos.name}({c})")
            else:
                print("    保留系统记录(挂单未成交属于此情况, 正常)")

    _summarize(cfg, applied)
    return True


def _fetch_trades(user) -> tuple:
    """读当日成交, 返回 (trades列表, 错误文本)。优先当日成交, 失败降级
    当日委托中已成交行。网格读取可能触发验证码弹窗, 用户在场可人工输入。"""
    try:
        rows = user.today_trades or []
        trades = [t for t in (_norm_trade(r) for r in rows) if t]
        if trades:
            return trades, ""
        return [], "当日成交网格为空(今天没成交? 或列名不识别)"
    except Exception as e:
        log.warning("当日成交读取失败: %s, 降级当日委托", e)
    try:
        rows = user.today_entrusts or []
        trades = [t for t in (_norm_entrust(r) for r in rows) if t]
        return trades, ("" if trades else "当日委托读取为空或无成交行")
    except Exception as e:
        return [], f"当日成交/委托网格读取均失败(可能触发验证码): {e}"


def _fetch_positions(user) -> tuple:
    """读券商持仓网格, 返回 ({code: pos}, 错误文本); 失败不臆测空仓。"""
    try:
        rows = user.position or []
        out = {}
        for r in rows:
            p = _norm_position(r)
            if p:
                out[p["code"]] = p
        return out, ""
    except Exception as e:
        return {}, f"券商持仓网格读取失败: {e}"


def _fetch_open_entrusts(user) -> tuple:
    """读当日委托中未完成(未成交/部分成交/未撤)的委托列表。"""
    try:
        rows = user.today_entrusts or []
        return [e for e in (_norm_open_entrust(r) for r in rows) if e], ""
    except Exception as e:
        return [], f"当日委托网格读取失败: {e}"


def _fmt_trade(t: dict) -> str:
    side_txt = "买入" if t["side"] == "BUY" else "卖出"
    price_txt = f" @{t['price']:.2f}" if t["price"] > 0 else ""
    t_txt = f"于 {t['t']} " if t["t"] else ""
    return (f"您{t_txt}人工{side_txt} {t['name']}({t['code']}) "
            f"{t['qty']}股{price_txt}")


def _search(code_or_name: str, trades: list, broker_pos: dict) -> list:
    """按代码/名称关键词在当日成交+券商持仓中检索(用户纠正时用)。"""
    key = code_or_name.strip()
    hits = []
    for t in trades:
        if key == t["code"] or (key in t["name"] and key):
            hits.append(("trade", t))
    for p in broker_pos.values():
        if key == p["code"] or (key in p["name"] and key):
            hits.append(("position", p))
    return hits


def _confirm_one(t: dict, held, store: PositionStore, el, applied: dict):
    """单笔人工成交的确认/纠正交互循环。"""
    while True:
        ans = input("    确认并同步系统持仓? [y=确认 / n=跳过 / "
                    "输入正确的代码或名称再校验]: ").strip()
        if ans in ("", "n"):
            print("    跳过")
            return
        if ans == "y":
            if t["side"] == "BUY":
                _record_buy(store, el, applied, t)
            else:
                _record_sell(store, el, applied, t)
            return
        # 用户纠正: 提供了股票名称/代码, 做进一步校验
        hits = _search(ans, [t], {})
        if not hits:
            print(f"    当日成交中未找到'{ans}'相关记录, 请核对代码/名称"
                  f"(可输入6位代码或名称关键词)")
            continue
        _, hit = hits[0]
        if hit is not t:
            print(f"    校验命中: {_fmt_trade(hit)}")
            t = hit
        held = store.get(t["code"])
        print(f"    该股系统持仓: "
              + (f"已记录(自{held.entry_time})" if held else "未记录"))


def _record_buy(store: PositionStore, el, applied: dict, t: dict):
    """人工买入补录建仓(已持有则提示不重复录)。"""
    note = f"人工买入对账 {t['t']} {t['qty']}股"
    ok = store.add(t["code"], name=t["name"], entry_price=t["price"],
                   note=note)
    if ok:
        applied["added"].append(f"{t['name']}({t['code']})")
        el.log("trade", stage="manual_audit", action="BUY", code=t["code"],
               name=t["name"], qty=t["qty"], price=t["price"],
               trade_time=t["t"], detail="人工对账确认补录建仓")
        print(f"    已补录建仓: {t['name']}({t['code']}) "
              f"@{t['price']:.2f} (注: {note})")
    else:
        print(f"    系统已持有 {t['name']}({t['code']}), 不重复录入")


def _record_sell(store: PositionStore, el, applied: dict, t: dict):
    """人工卖出移除系统记录(未持有则仅提示)。"""
    pos = store.remove(t["code"])
    if pos:
        applied["removed"].append(f"{t['name']}({t['code']})")
        el.log("trade", stage="manual_audit", action="SELL", code=t["code"],
               name=t["name"], qty=t["qty"], price=t["price"],
               trade_time=t["t"], detail="人工对账确认移除持仓")
        print(f"    已移除系统持仓: {t['name']}({t['code']}) "
              f"(原自{pos.entry_time})")
    else:
        print(f"    系统本无 {t['name']}({t['code']}) 持仓记录, 无需处理")


def _confirm_add_position(store: PositionStore, el, applied: dict,
                          code: str, name: str, price: float,
                          t: str, qty_txt: str):
    """券商有/系统无 的持仓补录确认(无成交明细时用持仓网格信息)。"""
    ans = input("    补录为系统持仓? [y=确认 / n=跳过]: ").strip()
    if ans == "y":
        note = f"人工对账补录 {t} {qty_txt}"
        ok = store.add(code, name=name, entry_price=price, note=note)
        if ok:
            applied["added"].append(f"{name}({code})")
            el.log("trade", stage="manual_audit", action="BUY", code=code,
                   name=name, price=price, trade_time=t,
                   detail="持仓差异校验补录(券商有系统无)")
            print(f"    已补录: {name}({code}) @{price:.2f}")
        else:
            print(f"    系统已持有 {name}({code}), 不重复录入")
    else:
        print("    跳过(如该股为今日刚挂单未成交等, 属正常)")


def _summarize(cfg: AppConfig, applied: dict):
    """终端汇总 + 手机推送(配置了推送通道才发)。"""
    added = applied["added"]
    removed = applied["removed"]
    lines = [f"补录建仓{len(added)}只: {added or '无'}",
             f"移除记录{len(removed)}只: {removed or '无'}"]
    print("\n" + "=" * 62)
    print("人工对账完成: " + "; ".join(lines))
    print("=" * 62)
    try:
        from notify.notifier import Notifier
        notifier = Notifier(cfg)
        if notifier.enabled() and (added or removed):
            notifier.send("人工对账完成",
                          "\n".join(lines) +
                          "\npositions.json已同步, 请留意后续巡检")
    except Exception as e:
        log.warning("对账结果推送失败: %s", e)


# ============ 启动恢复门 (crash recovery, auto模式重启时调用) ============

def _arm_kill_switch(cfg: AppConfig, reason: str):
    """写入kill_switch熔断文件(内容为熔断原因), 人工处理后删除即恢复。"""
    path = cfg.resolve(cfg.risk.kill_switch_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"startup_reconcile {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"{reason}\n处理流程: 监控台人工对账核对并确认差异 -> "
                f"删除本文件恢复自动交易\n")
    return path


def startup_reconcile(cfg: AppConfig, notifier=None) -> dict:
    """重启后启动恢复门: 券商真实状态 vs 本地状态对账。

    典型场景: 10:37程序崩溃(买单挂单中), 10:42重启——补建仓线程已死,
    positions.json可能永久少持仓, 未成交委托也无人撤。本函数在auto模式
    首轮开扫前执行一次:

    - 券商持仓代码集合 != positions.json, 或存在未成交委托
      => 写kill_switch熔断(买卖全停, 扫描继续), 手机告警+anomaly事件,
         等人工在监控台对账处理并删除熔断文件;
    - xiadan连不上/持仓网格读取失败(无法证伪, 可能只是验证码)
      => 只WARN告警不熔断, 保持原有启动行为(避免误锁死);
    - 完全一致 => 记恢复通过事件, 正常运行。

    注意: positions.json不记股数, 仅做代码集合比对, 同股数量差异
    (如人工加仓)发现不了, 属已知边界。返回dict供启动日志打印。
    """
    el = get_event_log(cfg.resolve(cfg.paths.logs_dir))
    store = PositionStore(cfg.resolve(cfg.positions.file))
    sys_codes = set(store.codes())
    result = {"ok": False, "halted": False, "reasons": [],
              "system_codes": sorted(sys_codes)}

    data = collect_broker_data(cfg)
    if not data.get("ok"):
        msg = f"启动恢复对账跳过(交易端不可读): {data.get('error')}"
        log.warning(msg)
        el.log("anomaly", type="startup_reconcile_skip", error=msg[:300])
        result["reasons"].append(msg)
        if notifier and notifier.enabled():
            notifier.send("启动恢复: 对账未执行", msg + "\n不阻断启动, "
                          "但请人工确认持仓/委托无异常")
        return result

    broker = data.get("broker_positions", {})
    broker_codes = set(broker)
    pos_err = data.get("errors", {}).get("positions", "")
    opens = data.get("open_entrusts", [])
    reasons = []

    if not pos_err:
        extra = sorted(broker_codes - sys_codes)
        missing = sorted(sys_codes - broker_codes)
        result["broker_codes"] = sorted(broker_codes)
        if extra:
            reasons.append("券商有持仓但系统无记录(疑似崩溃前挂单延迟成交/"
                           f"人工买入): {extra}")
        if missing:
            names = [f"{c}({store.get(c).name if store.get(c) else ''})"
                     for c in missing]
            reasons.append("系统记录持仓但券商无持仓(疑似人工卖出/"
                           f"本地脏数据): {names}")
    else:
        reasons.append(f"券商持仓网格读取失败, 未做持仓比对: {pos_err}")

    if opens:
        desc = [f"{e['side']} {e['name']}({e['code']}) "
                f"{e['filled']}/{e['qty']}股@{e['price']:.2f} {e['t']}"
                for e in opens[:10]]
        reasons.append(f"存在{len(opens)}笔未完成委托(崩溃前挂单?): {desc}")

    # 只有"确凿不一致"(非读取失败)才熔断; 网格读取类原因只告警
    hard_halt = bool(
        (not pos_err and broker_codes != sys_codes) or opens)

    if not hard_halt:
        if reasons:
            msg = "启动恢复: 存在读取类告警但未熔断 -> " + " | ".join(reasons)
            log.warning(msg)
            el.log("anomaly", type="startup_reconcile_warn",
                    reasons=" | ".join(reasons)[:500])
            if notifier and notifier.enabled():
                notifier.send("启动恢复: 部分数据不可读",
                              " | ".join(reasons)[:800])
            result["reasons"] = reasons
            return result
        log.info("启动恢复对账通过: 本地持仓%d只与券商一致, 无未完成委托",
                 len(sys_codes))
        el.log("anomaly", type="startup_reconcile_ok",
               holdings=len(sys_codes))
        result["ok"] = True
        return result

    reason_txt = "\n".join(f"{i+1}. {r}" for i, r in enumerate(reasons))
    try:
        ks = _arm_kill_switch(cfg, reason_txt)
    except OSError as e:
        ks = f"(熔断文件写入失败: {e})"
        log.error("熔断文件写入失败: %s", e)
    log.error("启动恢复发现状态不一致, 已熔断自动交易:\n%s\n熔断文件: %s",
              reason_txt, ks)
    el.log("anomaly", type="startup_reconcile_halt",
           reasons=" | ".join(reasons)[:800],
           system_codes=sorted(sys_codes),
           broker_codes=sorted(broker_codes) if not pos_err else [],
           open_entrusts=len(opens))
    if notifier and notifier.enabled():
        notifier.send(
            "启动恢复: 账户状态不一致, 已熔断自动交易",
            f"{reason_txt[:700]}\n\n自动交易已停止(扫描继续)。请在监控台"
            f"人工对账核对处理, 确认无误后删除熔断文件恢复交易:\n{ks}")
    result.update(ok=False, halted=True, reasons=reasons,
                  kill_switch=ks)
    return result
