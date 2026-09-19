"""盘前预置检查(preflight): 开盘前一次性自检全链路, 一次做完管到收盘。

设计:
- 三级结论: BLOCK(阻断, 不过不建议开盘) / WARN(警告, 可开盘但需人工确认)
            / INFO(信息+预热, 顺带把首轮成本做掉)
- 能自动的自动做(bootstrap登录、次新缓存预热、告警测试), 不能自动的给明确人工指引
- 每项检查独立try, 单点失败不影响其余项; 调用真实运行时API(非mock)
- 盘中兜底机制见文末"兜底清单", 节点出问题时自动降级, 兜不住才告警

用法: python main.py preflight
"""
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("preflight")

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"
LEVEL_ICON = {BLOCK: "❌阻断", WARN: "⚠️警告", INFO: "ℹ️ 信息"}

# 手机端同花顺Android包名(实测 topResumedActivity=com.hexin.plat.android/.Hexin)
THS_ANDROID_PKG = "com.hexin.plat.android"


@dataclass
class Check:
    name: str
    level: str
    ok: bool
    detail: str = ""
    fix: str = ""          # 失败时的人工操作指引


def _now_hhmm():
    return datetime.now().strftime("%H:%M:%S")


def _in_trade_time(cfg) -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hhmm = now.strftime("%H:%M:%S")
    return any(s <= hhmm <= e for s, e in cfg.risk.sessions)


def run_preflight(cfg, send_notify: bool = True) -> list:
    """执行盘前自检, 返回Check列表。"""
    checks = []

    def add(name, level, ok, detail="", fix=""):
        checks.append(Check(name, level, ok, detail, fix))
        log.info("[%s][%s] %s: %s", "✅" if ok else LEVEL_ICON.get(level, level),
                 level, name, detail)

    print(f"\n{'='*60}\n  盘前预置检查  {_now_hhmm()}\n{'='*60}")

    # ---- 1. ADB设备在线 (BLOCK) ----
    try:
        from adb.device import list_devices
        try:
            serials = list_devices()
        except Exception:
            # ADB服务可能未启动, 尝试拉起后重试
            import subprocess
            subprocess.run(["adb", "start-server"],
                           capture_output=True, timeout=15)
            serials = list_devices()
        if serials:
            add("Android设备连接", BLOCK, True,
                f"在线设备: {', '.join(serials)}")
        else:
            add("Android设备连接", BLOCK, False, "未检测到设备",
                "检查: 1)USB已连接 2)手机已开USB调试并授权 "
                "3)adb devices能看到设备; 手机端同花顺App保持在前台自选页")
    except Exception as e:
        add("Android设备连接", BLOCK, False, f"检测异常: {e}",
            "确认adb可用、数据线连接正常; 可尝试: adb kill-server && adb start-server")

    # ---- 2. 行情端hexin进程 (BLOCK) ----
    hexin_pid = None
    risk = None
    try:
        from trader.hotkey_trader import HotkeyTrader
        from trader.risk_control import RiskController
        risk = RiskController(cfg.risk, project_root=cfg.project_root,
                             positions_file=cfg.positions.file)
        hexin_pid = HotkeyTrader._find_pid("hexin.exe")
        if hexin_pid:
            add("同花顺行情端进程", BLOCK, True, f"hexin.exe PID={hexin_pid}")
        else:
            add("同花顺行情端进程", BLOCK, False, "hexin.exe未运行",
                "启动PC同花顺行情端并登录, 保持行情界面打开")
    except Exception as e:
        add("同花顺行情端进程", BLOCK, False, f"检测异常: {e}",
            "启动PC同花顺行情端")

    # ---- 3. 行情端连接 + 快捷键面板 (BLOCK, 下单链路核心) ----
    trader = None
    if hexin_pid:
        try:
            trader = HotkeyTrader(cfg, risk)
            trader._connect_hexin()
            if trader._panel_visible():
                add("快捷键面板(F1-F8)", BLOCK, True,
                    "面板可见, 热键就绪")
            else:
                # 尝试自动点击唤出
                ok = trader._ensure_hotkey_panel()
                if ok:
                    add("快捷键面板(F1-F8)", WARN, True,
                        "面板初始隐藏, 已自动点击唤出")
                else:
                    add("快捷键面板(F1-F8)", BLOCK, False,
                        "面板不可见且唤出失败",
                        "用鼠标点一下行情界面调出顶部快捷按钮条; "
                        "确认F1买/F4卖/F5撤键位已设置为闪电下单")
        except Exception as e:
            add("行情端连接/面板", BLOCK, False, f"异常: {e}",
                "重启同花顺行情端, 确认窗口未最小化到托盘")

    # ---- 4. 持仓文件可读写 (BLOCK) ----
    store = None
    try:
        from models.positions import PositionStore
        ppath = cfg.resolve(cfg.positions.file)
        store = PositionStore(ppath)
        store.load()
        held = store.codes()
        # 写权限探测(存回不改变内容)
        store.save()
        add("持仓文件positions.json", BLOCK, True,
            f"可读写, 当前持仓{len(held)}只: {held if held else '空仓'}")
    except Exception as e:
        add("持仓文件positions.json", BLOCK, False, f"异常: {e}",
            "检查positions.json是否被占用/只读, 目录可写")

    # ---- 5. 熔断开关 (BLOCK) ----
    try:
        ks = cfg.resolve(cfg.risk.kill_switch_file)
        if os.path.exists(ks):
            add("熔断开关kill_switch", BLOCK, False, f"熔断文件存在: {ks}",
                "确认要交易后删除该熔断文件")
        else:
            add("熔断开关kill_switch", BLOCK, True, "未触发, 可正常交易")
    except Exception as e:
        add("熔断开关kill_switch", WARN, False, f"检测异常: {e}")

    # ---- 6. 云同步Cookie (WARN, 人工导入项) ----
    try:
        from ths.watchlist import (CloudWatchlist, WatchlistAuthError,
                                   WatchlistError)
        cpath = cfg.resolve(cfg.watchlist.cookie_file)
        if not os.path.exists(cpath):
            add("云自选Cookie", WARN, False, "Cookie文件不存在",
                "浏览器登录10jqka.com.cn, F12复制*.10jqka请求的Cookie头, "
                f"整行粘贴到 {cpath}")
        else:
            with open(cpath, "r", encoding="utf-8") as f:
                cookie = f.read().strip()
            try:
                wl = CloudWatchlist(cookie, timeout=cfg.watchlist.request_timeout)
                items = wl.list_self()
                add("云自选Cookie", WARN, True,
                    f"有效, 云端自选{len(items)}只 (盘中CookieKeeper每30分钟保活)")
            except WatchlistAuthError:
                add("云自选Cookie", WARN, False, "Cookie已失效",
                    "重新登录10jqka复制Cookie粘贴到文件; "
                    "失效会导致热榜无法同步到手机自选")
            except WatchlistError as e:
                add("云自选Cookie", WARN, False, f"接口错误: {e}",
                    "检查网络, 稍后重试")
    except Exception as e:
        add("云自选Cookie", WARN, False, f"检测异常: {e}")

    # ---- 7. 屏保进程 (WARN) ----
    try:
        from keepawake import check_screensaver
        blockers = check_screensaver()
        if blockers:
            add("屏保/锁屏进程", WARN, False, f"检测到: {', '.join(blockers)}",
                "手动退出屏保播放程序(如ScreenSaverPlayer.exe), "
                "否则会阻断键盘/鼠标注入")
        else:
            add("屏保/锁屏进程", WARN, True, "无屏保干扰(防睡眠保护运行中)")
    except Exception as e:
        add("屏保/锁屏进程", WARN, False, f"检测异常: {e}")

    # ---- 8. 交易登录bootstrap (WARN, 自动尝试F12) ----
    if trader is not None:
        try:
            rep = trader.daily_bootstrap(force=False)
            if rep.get("ok"):
                if rep.get("skipped"):
                    add("交易通道登录(F12)", WARN, True, "今日已完成登录")
                else:
                    add("交易通道登录(F12)", WARN, True,
                        "已自动F12登录并关闭委托窗, 热键就绪")
            else:
                add("交易通道登录(F12)", WARN, False,
                    rep.get("error", "未知错误"),
                    "人工按F12登录交易账户, 等委托窗弹出后关闭, "
                    "再确认F1-F4闪电下单可用")
        except Exception as e:
            add("交易通道登录(F12)", WARN, False, f"异常: {e}",
                "人工F12登录交易系统并确认闪电下单快捷键")

    # ---- 9. 告警推送 (WARN) ----
    try:
        from notify.notifier import Notifier
        notifier = Notifier(cfg)
        if not notifier.enabled():
            add("手机告警推送", WARN, False, "未配置任何推送通道",
                "config.yaml notify段配置企业微信/飞书webhook(强烈建议, "
                "下单失败/撤单等需人工介入时会推送)")
        elif send_notify:
            ok = notifier.send("交易告警·盘前自检",
                               f"盘前自检 {_now_hhmm()} 告警通道测试正常")
            add("手机告警推送", WARN, ok,
                "测试消息已发送, 请确认手机收到" if ok else "推送发送失败",
                "" if ok else "检查webhook配置/网络, 关键词需含'告警'")
        else:
            add("手机告警推送", INFO, True, "通道已配置(跳过测试发送)")
    except Exception as e:
        add("手机告警推送", WARN, False, f"异常: {e}")

    # ---- 10. 热榜拉取 + 标的过滤 + 次新缓存预热 (INFO) ----
    try:
        from ths import fetcher
        from ths.universe_filter import filter_hot_stocks
        hot = fetcher.fetch(cfg.hot_list.top_n,
                            fallback_file=cfg.resolve(cfg.hot_list.fallback_file))
        trade, _, excluded = filter_hot_stocks(
            hot, cfg, held_codes=store.codes() if store is not None else [])
        add("热榜+标的过滤(预热)", INFO, True,
            f"热榜{len(hot)}只→可交易{len(trade)}只, 剔除{len(excluded)}只; "
            f"次新缓存已预热(首轮0成本)")
    except Exception as e:
        add("热榜+标的过滤(预热)", INFO, False, f"异常: {e}",
            "检查网络; 热榜有本地快照兜底, 不阻断开盘")

    # ---- 11. 交易时段/距开盘 (INFO) ----
    try:
        now = datetime.now()
        if now.weekday() >= 5:
            add("交易时段", INFO, True, "今天非交易日(周末)")
        elif _in_trade_time(cfg):
            add("交易时段", INFO, True, "当前在交易时段内(09:25起可挂单)")
        else:
            add("交易时段", INFO, True,
                f"当前{_now_hhmm()}非交易时段(09:25-11:30/13:00-15:00), "
                "等待开盘自动运行")
    except Exception as e:
        add("交易时段", INFO, False, f"异常: {e}")

    # ---- 12. 大盘行情源+系统性风控阈值 (INFO) ----
    try:
        from ths.quote import realtime_quote
        rc = cfg.risk
        q = realtime_quote(rc.market_index, 5.0)
        if q:
            add("大盘行情源/风控", INFO, True,
                f"{q['name']} {q['price']:.2f} ({q['pct']:+.2f}%) "
                f"源={q.get('source')}; 预警{rc.market_warn_pct:.1f}%/"
                f"熔断{rc.market_crash_pct:.1f}%"
                + ("" if rc.market_guard_enable else " (当前已禁用)"))
        else:
            add("大盘行情源/风控", WARN, False,
                "腾讯+新浪指数行情均失败",
                "检查网络; 大盘风控轮头会降级跳过(不因行情故障误熔断)")
    except Exception as e:
        add("大盘行情源/风控", INFO, False, f"异常: {e}")

    # ---- 13. 时钟与行情时效实时校验 (WARN, 2026-09-18采纳) ----
    try:
        from monitor.clock_guard import ClockGuard
        cg = ClockGuard(cfg.monitor.clock_skew_warn_sec,
                        cfg.monitor.clock_stale_warn_sec,
                        cfg.monitor.clock_guard_enable)
        kind = cg.check_once(cfg.risk.market_index)
        if not cfg.monitor.clock_guard_enable:
            add("时钟/行情时效(实时)", INFO, True, "时钟守卫已关闭")
        elif kind == "skew":
            add("时钟/行情时效(实时)", WARN, False,
                "行情时刻与本机偏差超阈值, 本机时钟可能漂移",
                "校时本机(14:55买截止/14:57禁撤依赖本机时间)")
        elif kind == "stale":
            add("时钟/行情时效(实时)", WARN, False,
                "行情时刻滞后超阈值, 行情源可能缓存/中断",
                "检查网络/源站; 盘中以最新行情为准")
        else:
            add("时钟/行情时效(实时)", WARN, True, "时钟与行情时效正常")
    except Exception as e:
        add("时钟/行情时效(实时)", WARN, False, f"检测异常: {e}")

    # ---- 汇总 ----
    blocks = [c for c in checks if c.level == BLOCK and not c.ok]
    warns = [c for c in checks if c.level == WARN and not c.ok]
    print("\n" + "-" * 60)
    for c in checks:
        mark = "✅" if c.ok else LEVEL_ICON.get(c.level, "·")
        line = f"{mark} [{c.level}] {c.name}: {c.detail}"
        print(line)
        if not c.ok and c.fix:
            print(f"       → 处理: {c.fix}")
    print("-" * 60)

    if blocks:
        print(f"\n🔴 有 {len(blocks)} 项阻断问题, 修复后再开盘自动交易:")
        for c in blocks:
            print(f"   · {c.name}: {c.fix or c.detail}")
    elif warns:
        print(f"\n🟡 阻断项全部通过, 有 {len(warns)} 项警告建议确认后开盘。")
    else:
        print("\n🟢 全部检查通过, 具备开盘自动交易条件。")

    # 人工每日清单
    print("\n【每日人工确认清单】(程序能探测但需你确保):")
    print("   1. 手机同花顺App在前台、停在自选股列表/日K页")
    print("   2. PC同花顺行情端已登录、顶部快捷键按钮条可见")
    print("   3. (如Cookie失效)重新导入web端Cookie")
    print("   4. 确认模拟账户资金/持仓正常")

    # 盘中兜底机制
    print("\n【盘中兜底机制】(节点出问题自动降级):")
    print("   · 手机断开 → 自动重连; 热榜API失败 → 本地快照")
    print("   · 快捷键面板消失 → 自动点击唤出; 唤不出 → xiadan表单兜底")
    print("   · Cookie临期 → CookieKeeper每30分钟保活; 失效 → 手机告警")
    print("   · 买入挂单2分钟未成交 → F8撤该股挂单 + 告警(不自动追价)")
    print("   · 系统级异常(熔断/设备掉线) → F5秒撤全部挂单 + 告警(损失最小化)")
    print("   · 热键未生效(F6无持仓) → uncertain + 手机告警人工确认")
    print("   · 告警推送失败 → 本地alerts.jsonl照记, 不影响交易")

    # ---- 磁盘空间检查 (WARN) ----
    try:
        import shutil
        logs_dir = cfg.resolve(cfg.paths.logs_dir)
        usage = shutil.disk_usage(logs_dir if os.path.exists(logs_dir)
                                  else cfg.project_root or ".")
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 2:
            add("磁盘可用空间", BLOCK, False,
                f"仅剩 {free_gb:.1f}GB, 截图/日志写入可能失败导致全盘NONE",
                f"清理 {logs_dir} 下旧文件: screenshots/超48h的png、"
                f"旧scan日志; 或将logs_dir迁到空间充足的盘")
        elif free_gb < 5:
            add("磁盘可用空间", WARN, False,
                f"剩余 {free_gb:.1f}GB, 建议清理旧截图/日志",
                f"清理 {logs_dir} 下的过期文件; 截图48h自动TTL, "
                f"但CSV/events/审计日志会累积")
        else:
            add("磁盘可用空间", WARN, True,
                f"剩余 {free_gb:.1f}GB")
    except Exception as e:
        add("磁盘可用空间", WARN, False, f"检测异常: {e}")

    # 自检结果推送(仅全阻断失败时推一条提醒)
    if send_notify and blocks:
        try:
            from notify.notifier import Notifier
            Notifier(cfg).send(
                "交易告警·盘前自检未通过",
                "阻断项: " + "; ".join(c.name for c in blocks))
        except Exception:
            pass

    return checks


# ===================== 轻量只读探活(前端页面轮询用) =====================

def _pid_alive(pid: int) -> bool:
    """tasklist按PID查进程是否存活(不依赖psutil)。"""
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8).stdout
        return f'"{pid}"' in out
    except Exception:
        return False


def _ths_foreground(serial: str):
    """查手机前台Activity; 返回 (前台包名==同花顺, 当前包名或错误串)。"""
    from adb.device import Device
    try:
        out = Device(serial).shell("dumpsys activity activities", timeout=8)
        for line in out.splitlines():
            if "topResumedActivity" in line or "ResumedActivity:" in line:
                return (THS_ANDROID_PKG in line,
                        line.strip()[:120])
        return False, "未解析到前台Activity"
    except Exception as e:
        return False, f"查询失败: {e}"


def run_health_checks(cfg) -> dict:
    """轻量、只读、零副作用的系统探活, 供前端每30s轮询:

    与 run_preflight 的区别: 不连接/操作交易窗口、不F12登录、不发推送、
    不写持仓、不调热榜API(读本地快照时间), 单次通常2-4s, 盘中可安全反复跑。
    返回 {time, elapsed, checks:[{name,level,ok,detail,fix}], summary}。
    """
    t0 = time.time()
    checks = []

    def add(name, level, ok, detail="", fix=""):
        checks.append({"name": name, "level": level, "ok": bool(ok),
                       "detail": detail, "fix": fix})

    serial = None
    # ---- 1. ADB设备在线 (BLOCK) ----
    try:
        from adb.device import list_devices
        try:
            devs = list_devices()
        except Exception:
            import subprocess
            subprocess.run(["adb", "start-server"],
                           capture_output=True, timeout=15)
            devs = list_devices()
        if devs:
            serial = devs[0]
            add("Android设备连接", BLOCK, True,
                f"在线设备: {', '.join(devs)}")
        else:
            add("Android设备连接", BLOCK, False, "未检测到设备",
                "1) 确认USB数据线连接; 2) 手机开启USB调试并授权本机; "
                "3) 命令行 adb devices 能看到设备; 4) 拔插后在手机弹窗点允许")
    except Exception as e:
        add("Android设备连接", BLOCK, False, f"检测异常: {e}",
            "确认 platform-tools/adb 可用, 重启ADB服务: adb kill-server && adb start-server")

    # ---- 2. 手机同花顺App前台 (BLOCK, 新增) ----
    if serial:
        ok, cur = _ths_foreground(serial)
        if ok:
            add("手机同花顺App", BLOCK, True, "同花顺在手机前台")
        else:
            add("手机同花顺App", BLOCK, False, f"前台不是同花顺: {cur}",
                "在手机上打开同花顺App, 进入 自选→任一股票→日K页 并保持屏幕常亮; "
                "勿让手机息屏或切到其他App")
    else:
        add("手机同花顺App", BLOCK, False, "设备不在线, 无法检查前台App",
            "先恢复ADB连接(见上一节点), 再打开手机同花顺自选日K页")

    # ---- 3. PC行情端hexin进程 (BLOCK) ----
    hexin_pid = None
    try:
        from trader.hotkey_trader import HotkeyTrader
        hexin_pid = HotkeyTrader._find_pid("hexin.exe")
        if hexin_pid:
            add("PC同花顺行情端", BLOCK, True, f"hexin.exe 运行中 PID={hexin_pid}")
        else:
            add("PC同花顺行情端", BLOCK, False, "hexin.exe 未运行",
                "启动PC同花顺行情端并登录账户, 保持行情窗口打开(可最小化但勿退出); "
                "闪电下单快捷键依赖该进程")
    except Exception as e:
        add("PC同花顺行情端", BLOCK, False, f"检测异常: {e}", "启动PC同花顺行情端")

    # ---- 4. 监控进程/心跳 (BLOCK卡死 / INFO未启动) ----
    try:
        hb_path = os.path.join(cfg.resolve(cfg.paths.logs_dir), "heartbeat.json")
        hb = _read_json_file(hb_path)
        age = time.time() - float(hb.get("ts", 0)) if hb else None
        today = time.strftime("%Y-%m-%d")
        hb_day = str(hb.get("time", ""))[:10] if hb else ""
        stale_limit = float(getattr(cfg.monitor, "heartbeat_touch_stale", 90)) + 30
        if not hb or hb_day != today:
            add("监控心跳", INFO, False,
                "今日尚未启动自动监控(auto_round)",
                f"开盘前在服务器运行: python main.py auto_round  "
                f"(paper模式); 启动后心跳每{int(getattr(cfg.monitor, 'heartbeat_interval', 30))}s一跳")
        else:
            pid = int(hb.get("pid", 0))
            phase = hb.get("phase", "")
            if not _pid_alive(pid):
                add("监控心跳", BLOCK, False,
                    f"监控进程已退出(PID={pid}, 最后心跳 {hb.get('time')} 阶段={phase})",
                    "查看logs/最新scan日志定位崩溃原因, 重新运行 python main.py auto_round; "
                    "看门狗未拉起时需人工启动")
            elif age is not None and age > stale_limit:
                add("监控心跳", BLOCK, False,
                    f"心跳停滞{int(age)}秒(阈值{int(stale_limit)}s), 阶段={phase}, "
                    f"最后{hb.get('time')}",
                    "主线程可能卡死: 1) 查看logs/scan日志; 2) 检查手机是否弹窗/设备掉线; "
                    "3) 必要时重启auto_round")
            else:
                add("监控心跳", BLOCK, True,
                    f"正常 PID={pid} {int(age)}s前 阶段={phase}"
                    + (f" 第{hb.get('round')}轮" if hb.get("round") else ""))
    except Exception as e:
        add("监控心跳", WARN, False, f"检测异常: {e}")

    # ---- 5. 云自选Cookie (WARN, 单次HTTP) ----
    try:
        from ths.watchlist import CloudWatchlist, WatchlistAuthError, WatchlistError
        cpath = cfg.resolve(cfg.watchlist.cookie_file)
        if not os.path.exists(cpath):
            add("云自选Cookie", WARN, False, "Cookie文件不存在",
                f"浏览器登录 10jqka.com.cn → F12网络面板复制请求Cookie头 → "
                f"整行粘贴到 {os.path.basename(cpath)}")
        else:
            cookie = open(cpath, "r", encoding="utf-8").read().strip()
            try:
                items = CloudWatchlist(
                    cookie, timeout=cfg.watchlist.request_timeout).list_self()
                add("云自选Cookie", WARN, True,
                    f"有效, 云端自选 {len(items)} 只")
            except WatchlistAuthError:
                add("云自选Cookie", WARN, False, "Cookie已失效",
                    "重新登录10jqka.com.cn, 复制最新Cookie整行覆盖cookie文件; "
                    "盘中CookieKeeper每30分钟保活, 长时间停机后需人工更新")
            except WatchlistError as e:
                add("云自选Cookie", WARN, False, f"接口错误: {e}", "检查网络后点刷新重试")
    except Exception as e:
        add("云自选Cookie", WARN, False, f"检测异常: {e}")

    # ---- 6. 持仓文件只读校验 (BLOCK) ----
    try:
        from models.positions import PositionStore
        store = PositionStore(cfg.resolve(cfg.positions.file))
        held = store.codes()
        add("持仓文件", BLOCK, True,
            f"可读, 当前持仓{len(held)}只: {held if held else '空仓'}")
    except Exception as e:
        add("持仓文件", BLOCK, False, f"读取失败: {e}",
            "检查 positions.json 是否损坏/被占用; 若损坏, 系统已自动备份为"
            " .corrupt 文件, 需人工核对后恢复")

    # ---- 7. 熔断开关 (BLOCK) ----
    try:
        ks = cfg.resolve(cfg.risk.kill_switch_file)
        if os.path.exists(ks):
            add("熔断开关", BLOCK, False, "熔断文件存在, 自动交易被禁止",
                f"确认风险已排除后删除文件: {ks}")
        else:
            add("熔断开关", BLOCK, True, "未触发, 可正常交易")
    except Exception as e:
        add("熔断开关", WARN, False, f"检测异常: {e}")

    # ---- 8. 屏保进程 (WARN) ----
    try:
        from keepawake import check_screensaver
        blockers = check_screensaver()
        if blockers:
            add("屏保/锁屏", WARN, False, f"检测到: {', '.join(blockers)}",
                "手动退出屏保播放程序(如ScreenSaverPlayer.exe), 它会阻断键鼠注入; "
                "并确认电源设置不会自动熄屏")
        else:
            add("屏保/锁屏", WARN, True, "无干扰(防睡眠保护随监控启动)")
    except Exception as e:
        add("屏保/锁屏", WARN, False, f"检测异常: {e}")

    # ---- 9. 告警通道配置 (WARN, 只查配置不发送) ----
    try:
        from notify.notifier import Notifier
        if Notifier(cfg).enabled():
            add("手机告警推送", WARN, True, "推送通道已配置")
        else:
            add("手机告警推送", WARN, False, "未配置任何推送通道",
                "config.yaml 的 notify 段配置企业微信/飞书机器人webhook"
                "(关键词需含'告警'); 下单失败/设备掉线等靠它通知你")
    except Exception as e:
        add("手机告警推送", WARN, False, f"检测异常: {e}")

    # ---- 10. 热榜本地快照新鲜度 (INFO, 不调API减负载) ----
    try:
        hp = cfg.resolve(cfg.hot_list.fallback_file)
        if os.path.exists(hp):
            mtime = os.path.getmtime(hp)
            data = _read_json_file(hp) or []
            age_h = (time.time() - mtime) / 3600
            add("热榜数据快照", INFO, age_h < 20,
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))} "
                f"快照{len(data)}只 ({age_h:.1f}小时前)",
                "" if age_h < 20 else "快照较旧: 启动auto_round首轮会自动拉取最新热榜; "
                                      "也可运行完整自检立即预热")
        else:
            add("热榜数据快照", INFO, False, "无本地快照",
                "启动 auto_round 或运行一次完整自检即会拉取热榜")
    except Exception as e:
        add("热榜数据快照", INFO, False, f"检测异常: {e}")

    # ---- 11. 磁盘可用空间 (WARN) ----
    try:
        import shutil
        logs_dir = cfg.resolve(cfg.paths.logs_dir)
        check_dir = logs_dir if os.path.exists(logs_dir) else \
            (cfg.project_root or ".")
        usage = shutil.disk_usage(check_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 2:
            add("磁盘可用空间", BLOCK, False,
                f"仅剩 {free_gb:.1f}GB, 截图写入可能失败导致检测全NONE",
                f"清理 {logs_dir} 下旧文件: screenshots/超48h的png、"
                f"旧scan日志; 或将logs_dir迁到空间充足的盘")
        elif free_gb < 5:
            add("磁盘可用空间", WARN, False,
                f"剩余 {free_gb:.1f}GB, 建议清理旧截图/日志",
                f"清理 {logs_dir} 下的过期文件")
        else:
            add("磁盘可用空间", WARN, True,
                f"剩余 {free_gb:.1f}GB")
    except Exception as e:
        add("磁盘可用空间", WARN, False, f"检测异常: {e}")

    # ---- 12. 交易时段 (INFO) ----
    try:
        now = datetime.now()
        if now.weekday() >= 5:
            add("交易时段", INFO, True, "今天非交易日(周末)")
        elif _in_trade_time(cfg):
            add("交易时段", INFO, True, f"当前在交易时段内 {_now_hhmm()}")
        else:
            add("交易时段", INFO, True,
                f"非交易时段 {_now_hhmm()} (9:30-11:30/13:00-15:00)")
    except Exception as e:
        add("交易时段", INFO, False, f"检测异常: {e}")

    blocked = sum(1 for c in checks if c["level"] == BLOCK and not c["ok"])
    warned = sum(1 for c in checks if c["level"] == WARN and not c["ok"])
    info_bad = sum(1 for c in checks if c["level"] == INFO and not c["ok"])
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t0, 1),
        "checks": checks,
        "summary": {"total": len(checks), "ok": sum(1 for c in checks if c["ok"]),
                    "block": blocked, "warn": warned, "info_pending": info_bad},
    }


def _read_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
