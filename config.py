"""配置加载: dataclass 默认值 + config.yaml 覆盖, 任何配置项缺失都不报错。"""
import logging
import os
import re
from dataclasses import dataclass, field, fields
from typing import List, Tuple

import yaml

Region = Tuple[int, int, int, int]
Point = Tuple[int, int]


@dataclass
class DeviceConfig:
    serial: str = ""
    adb_path: str = ""
    screenshot_max_retries: int = 3


@dataclass
class PathsConfig:
    stocks_file: str = "stocks.txt"
    screenshots_dir: str = "screenshots"
    logs_dir: str = "logs"


@dataclass
class RegionsConfig:
    title_region: Region = (0, 80, 1080, 280)
    kline_region: Region = (0, 400, 1080, 1900)
    latest_k_region: Region = (780, 420, 1080, 950)
    title_code_region: Region = (340, 205, 730, 252)


@dataclass
class ButtonsConfig:
    next_stock: Point = (1000, 150)
    prev_stock: Point = (80, 150)


@dataclass
class SwitchConfig:
    poll_interval: float = 0.2
    post_tap_delay: float = 0.4
    confirm_gap: float = 0.1
    jitter_min: float = 0.05
    jitter_max: float = 0.12
    max_switch_retries: int = 3
    loop_max_stocks: int = 150  # scan_loop默认上限(须>=自选股循环长度)


@dataclass
class DetectionConfig:
    template_threshold: float = 0.70      # 模板匹配置信度阈值(实测0.80会漏检0.71-0.79的真信号)
    save_debug_screenshot: bool = True
    latest_k_center_x: int = 1026
    latest_k_tolerance: int = 16
    color_fallback: bool = False          # 颜色兜底关闭(K线红绿柱体会误命中, 降阈值更可靠)


@dataclass
class VisionConfig:
    templates_dir: str = "detector/templates"
    digits_dir: str = "detector/templates/digits"
    long_prefix: str = "long_"
    short_prefix: str = "short_"


@dataclass
class UIConfig:
    stock_code_pattern: str = r"(?<!\d)\d{6}(?!\d)"
    next_button_keywords: List[str] = field(
        default_factory=lambda: ["下一只", "下一股票", "下一股", "next"]
    )
    next_button_ids: List[str] = field(
        default_factory=lambda: ["next", "switch_right", "stock_right"]
    )


@dataclass
class GotoConfig:
    """搜索跳转(goto)导航坐标与节奏, 全部为真机实测值。"""
    search_btn: Point = (1008, 192)       # K线页顶栏搜索图标
    input_box: Point = (470, 186)         # 搜索页输入框
    first_suggestion: Point = (480, 320)  # 首条联想结果行
    kline_tab: Point = (227, 664)         # 落页(分时)的日K tab (UI dump实测)
    search_load_delay: float = 1.5
    suggest_delay: float = 1.4
    page_load_delay: float = 2.0
    kline_load_delay: float = 1.2
    max_retries: int = 2


@dataclass
class HomeNavConfig:
    """回自选股列表的导航节奏 (goto搜索后会丢失<>循环上下文, 需从此恢复)。

    延迟已按ADB dump自身耗时(约0.7s/次, 天然充当过渡等待)压缩校准。
    """
    back_delay: float = 0.7          # 每次返回后等待
    tab_load_delay: float = 1.3      # 点底部'自选'tab/下拉刷新后等列表
    row_load_delay: float = 1.3      # 点股票行后等行情页
    kline_load_delay: float = 0.9    # 切日K后等图表
    max_back: int = 6                # 返回链最大次数


@dataclass
class WatchlistConfig:
    """自选股云同步: cloud=Web接口自动同步(手机端云同步刷新); none=人工同步。"""
    sync_mode: str = "none"
    cookie_file: str = "ths_cookie.txt"      # 浏览器登录10jqka.com.cn后的完整Cookie
    request_timeout: float = 10.0
    keepalive_interval: float = 1800.0       # Cookie保活探测间隔(秒), 默认30分钟
    # 云同步删除护栏(2026-09-15用户裁定): 一轮整表替换中, 待删除自选股占
    # 现有云自选比例超阈值时中止同步并CRITICAL告警(防Cookie失效/接口返回
    # 异常把"我的自选"清空)。持仓股并入desired永不删除, 不受此限。
    delete_guard_enable: bool = True
    delete_guard_max_pct: float = 0.30       # 待删除/现有>30%即中止(0=关, 1=不护栏)
    # 删除护栏两段式二次确认(2026-09-18第二版点评采纳, 默认关=现行单阈值中止):
    # 占比超max_pct但不超过hard_pct时, 不立即中止, 等待confirm_wait秒后强制
    # fresh重拉云端列表再次比对: 差集缩小到<=confirm_max_diff只(接口瞬时抖动)
    # 则放行继续同步, 否则维持中止。占比>hard_pct(疑似整表清空)一律立即中止,
    # 二次拉取失败也维持中止(保守, 绝不放行)。持仓股任何情况下不删除。
    delete_guard_confirm_enable: bool = False
    delete_guard_hard_pct: float = 0.60     # 超该硬顶不二次确认直接中止
    delete_guard_confirm_wait: float = 45.0 # 两次拉取间隔(秒), 等源站缓存刷新
    delete_guard_confirm_max_diff: int = 2  # fresh重拉差集<=该只数则放行


@dataclass
class HotListConfig:
    """同花顺热榜API与轮驱动节奏。"""
    top_n: int = 100
    fallback_file: str = "logs/hotlist_local.json"  # API失败兜底, 每轮成功后刷新
    sync_mode: str = "none"               # none=App列表人工同步(仅记diff)
    round_interval: float = 300.0         # auto_round轮间隔(秒), 一轮结束补足再开


@dataclass
class UniverseConfig:
    """交易标的过滤: 热榜拉取后、云同步前过滤, 不可交易股不进扫描列表。

    时效性: 过滤前置使手机端每轮只扫可交易股(少扫十几只, 省20-40s/轮);
    次新判定走日K根数(腾讯接口), 每日缓存一次(首轮并行约5s, 后续轮0成本)。
    持仓股不受过滤(保证卖出链路始终可扫)。
    """
    enable: bool = True
    main_board_only: bool = True       # 仅沪深主板: 60/00开头(排除688科创/300创业/8·4·920北交)
    exclude_st: bool = True            # 排除ST/*ST/SST
    exclude_subnew: bool = True        # 排除次新股
    subnew_min_bars: int = 250         # 上市交易日数<该值视为次新(250≈1年)
    cache_file: str = "logs/universe_cache.json"  # 次新判定每日缓存
    cache_ttl_hours: float = 20.0      # 缓存有效期(小时), 跨交易日自动刷新


@dataclass
class PositionsConfig:
    """持仓巡检: 持仓股优先级高于未持仓股。

    满仓(max_positions只)后全量检索持续进行不停止, 多头信号仅记录
    (时间+现价)不交易; 人工买卖后用 manual_audit 主动对账同步持仓。
    """
    file: str = "positions.json"          # 持仓唯一事实源(PC侧)
    recheck_interval: float = 90.0        # 主轮中持仓插扫间隔(秒)
    idle_recheck_interval: float = 120.0  # 轮间等待期持仓巡检间隔(秒, 0=关)
    max_positions: int = 4                # 最大持仓只数(分批仓位): 满仓后
                                          # 不开新仓, 全量检索继续, 多头信号
                                          # 仅记录时间+现价
    # 持仓浮亏只读告警(2026-09-18第二版点评采纳): 持仓巡检时用建仓价entry_price
    # 与现价比对, 浮亏达warn_pct记WARNING/达critical_pct记CRITICAL(每股每档
    # 当日去重, 只告警不自动卖——止损自动停已被用户否决)。现价查询失败跳过。
    loss_alert_enable: bool = True
    loss_warn_pct: float = -0.10          # 浮亏>=10% WARNING
    loss_critical_pct: float = -0.15      # 浮亏>=15% CRITICAL


@dataclass
class ExecutionConfig:
    """执行模式: paper=纸面(更新持仓+告警); auto=真实下单。
    channel(auto时): form=xiadan表单下单; hotkey=行情端快捷键闪电下单。"""
    mode: str = "paper"
    channel: str = "form"


@dataclass
class EasytraderConfig:
    """easytrader PC客户端下单配置。"""
    exe_path: str = ""                    # 同花顺xiadan.exe路径
    tesseract_cmd: str = ""               # Tesseract OCR路径(验证码识别, 可选)
    connect_timeout: float = 10.0
    client_type: str = "universal_client"  # universal_client / ths


@dataclass
class HotkeyConfig:
    """行情端快捷键下单 (execution.channel=hotkey时生效)。

    用户键位(2026-09-08同步, 同花顺模拟账户自动登录):
    F1=按最新价买入, 仓位=账户25%
    F2=按卖一价买入, 仓位=账户25% (极端情况备用)
    F3=按最新价卖出可用仓位100%(清仓)
    F4=按买一价卖出可用仓位100%(核卖清仓)
    """
    hexin_exe: str = ""                  # hexin.exe路径(空=按进程名自动连接)
    buy_latest_key: str = "{F1}"         # 买入: 最新价 25%仓位
    buy_ask1_key: str = "{F2}"           # 买入: 卖一价 25%仓位(极端)
    sell_latest_key: str = "{F3}"        # 卖出: 最新价 清仓
    sell_bid1_key: str = "{F4}"          # 卖出: 买一价 清仓(核卖)
    cancel_key: str = "{F5}"             # 秒撤: 撤销全部未成交委托(系统级异常用)
    cancel_single_key: str = "{F8}"      # 单只撤: 撤销当前股票的买卖挂单
    position_key: str = "{F6}"           # 查持仓浮层(用户自定义, 非系统F6)
    entrust_key: str = "{F7}"            # 查当日委托(弹交易页, 弃用)
    buy_variant: str = "latest"          # BUY信号用键: latest=F1 / ask1=F2
    sell_variant: str = "latest"         # SELL信号用键: latest=F3 / bid1=F4
                                         # 默认F3最新价卖(2026-09-12用户定):
                                         # 不核卖; 跌停排队卖走queue_only不撤单
    buy_precheck_f6: bool = True         # 买入前F6预检该股是否已有持仓
                                         # (防人工买入重复建仓, 多花约4s/笔)
    goto_settle_seconds: float = 2.0     # 回车切股票后等行情页加载
    f6_verify_enable: bool = True        # 下单后F6查个股持仓浮层验证成交
                                         # (行情端浮层, 不弹xiadan无验证码)
    f6_verify_timeout: float = 12.0      # SELL后F6确认窗口上限(秒)
    f6_first_check: float = 4.0          # 下单后首次F6查询等待(秒): 盘中市价单
                                         # 通常1-3s成交, 4s首查多数已成交
    f6_poll_interval: float = 5.0        # F6轮询间隔(秒): 未成交则复查直到
                                         # buy_quick_confirm/f6_verify_timeout上限;
                                         # 成交立即返回(盘中买单约8-10s/只)
    buy_quick_confirm: float = 30.0      # BUY后F6轮询窗口上限(秒): 超时判定挂单中,
                                         # 转后台挂单监控(到点未成交F8/F5撤单)
    buy_pending_wait: float = 120.0      # 常规时段挂单总等待(秒)=2分钟: 未成交则
                                         # F8/F5撤单(后台线程, 不阻塞扫描)
    opening_pending_wait: float = 300.0  # 开盘窗口(9:25-9:30)挂单总等待=5分钟:
                                         # 首轮扫描单多挂在全天最活跃时段, 特殊
                                         # 放宽不撤, 其余时段仍2分钟
    verify_enable: bool = False          # 旧xiadan委托回查(弹窗+验证码, 仅手
                                         # 动开启; f6_verify_enable优先)
    verify_timeout: float = 15.0         # 回查超时(秒)
    verify_interval: float = 2.0         # 回查轮询间隔(秒)
    sell_precheck_f6: bool = True        # 卖出前F6预检(与买入对称): 券商侧无
                                         # 持仓则拒卖; 检测异常fail-open放行
                                         # (券商端最终校验, 用户裁定2026-09-15)
    # 发键前价格守卫(2026-09-15用户裁定, 第一阶段只告警): 发键前重取现价与
    # 信号时价比对, 偏离超阈值只WARNING+写price_guard/exec_quality事件并照常
    # 发单(滑点台账); enforce=true才升级为拒单。queue_only(跌停排队卖/尾盘
    # 竞价结算单)一律跳过, 任何情况下不拦结算单。
    price_guard_enable: bool = True
    price_guard_enforce: bool = False    # 默认只告警不拦截(预留强制开关)
    price_guard_buy_pct: float = 0.008   # 买入现价高于信号价0.8%告警
    price_guard_sell_pct: float = 0.015  # 卖出现价低于信号价1.5%告警
    # F8撤单前后核对(2026-09-15用户裁定): 撤单前核对行情窗口代码==目标股
    # (明确不一致则不发F8, 防撤错股票); 撤单后F6验证挂单消失。任何一步
    # 失败只CRITICAL告警人工介入, 绝不自动补撤/双撤循环。
    cancel_verify_enable: bool = True
    # F6连续异常自动降级xiadan只读对账(2026-09-15裁定): 连续threshold次F6
    # 检测链路异常(非"未成交", 是截屏/模板链路不可用)才触发一次只读对账,
    # 复用manual_audit采集(券商成交/持仓/未完成委托), 只读不写不撤补,
    # 差异CRITICAL。cooldown秒内不重复弹xiadan。0=关闭。
    f6_fallback_enable: bool = True
    f6_fallback_threshold: int = 2
    f6_fallback_cooldown: float = 1800.0
    # 尾盘集合竞价(14:57-15:00)顶格报价热键(2026-09-16用户裁定): 尾盘结算单
    # 改发用户在同花顺面板自定义的"涨停价买入/跌停价卖出"键。集合竞价按
    # 收盘价统一撮合, 顶格只为拿撮合优先级而非按涨跌停价成交; 数量仍由客户端
    # 决定(F1=25%/F3=清仓语义不变, 不做系统侧股数计算)。键位须先在同花顺
    # 客户端绑定后填入; 留空=回退常规F1/F3并WARNING(不阻断结算)。封板物理
    # 例外不保证成交: 涨停封板买单不挂(跳过+开板告警), 跌停封板卖单仍排队。
    closing_limit_key_enable: bool = True
    closing_buy_limit_key: str = ""        # 涨停价买入自定义键(如"{F9}"); 空=回退F1
    closing_sell_limit_key: str = ""       # 跌停价卖出自定义键(如"{F10}"); 空=回退F3
    # 发键白名单(2026-09-18第二版点评采纳): 发单键必须在允许集合内, 白名单外
    # 的键拒绝发送+审计(防配置写错发出客户端不识别的键)。默认允许F1-F12(现行
    # 全部键位均在其中, 行为不变); allowed_keys可显式收窄(填形如["F1","F3"])。
    key_whitelist_enable: bool = True
    allowed_keys: List[str] = field(default_factory=list)  # 空=内置F1-F12
    # F8核对fail-open滑窗告警(2026-09-18采纳): F8撤单前窗口标题解析不出代码
    # (无法核对, fail-open照常撤)的情况在window_sec内累计达max次→告警(核对机制
    # 可能持续失效)。窗口过期自动清空计数。
    cancel_fail_alert_enable: bool = True
    cancel_fail_window_sec: float = 1800.0
    cancel_fail_max: int = 3
    # 收盘成交复查退避(2026-09-18采纳, 智谱A4): queue_only挂单15:00撮合后按
    # [5,600,1800]秒三次复查(15:00:05/15:10:05/15:30:05), 前两次F6检测链路异常
    # 只重试, 第三次仍失败才转unknown+CRITICAL(避免一次瞬时抖动把挂单悬置)。
    closing_recheck_delays: List[int] = field(
        default_factory=lambda: [5, 600, 1800])
    # 撤单竞态撤前F6终核(2026-09-18采纳, 智谱A2, 默认关): F8撤单前再做一次F6
    # 终核, 若持仓已是该委托的预期成交状态(BUY已有持仓/SELL已无持仓=撤单前已
    # 成交)则放弃F8+告警。检测异常fail-open照常撤。失败处置仍遵循第2项裁定
    # (只CRITICAL不自动补撤), 本开关仅在撤单前增加一道只读校验。
    cancel_race_check_enable: bool = False


@dataclass
class RiskConfig:
    """交易风控配置。"""
    enable: bool = True
    kill_switch_file: str = "logs/kill_switch.flag"
    max_orders_per_day: int = 20
    max_qty_per_order: int = 1000
    default_qty: int = 100
    per_stock_daily_buys: int = 1         # 每股每日限买次数(0=不限; 卖出
                                          # 不限, 清仓后无仓可卖)
    state_file: str = "logs/trade_state.json"
    sessions: list = field(default_factory=lambda: [
        ["09:25:00", "11:30:00"], ["13:00:00", "15:00:00"]
    ])
    # 大盘系统性风控: 指数跌破crash_pct当日自动触发"买侧熔断"(只禁买入;
    # 卖出/撤单照常——风控止损永不停), 跌破warn_pct仅告警一次。
    # 买侧熔断可被指数回升自动解除, 次日自动清除; kill_switch仍是人工
    # 全停开关(买卖全拦, 只能人工删flag)。指数代码带sh/sz前缀(腾讯行情)。
    market_guard_enable: bool = True
    market_index: str = "sh000001"          # 主监控指数(上证指数; 可换sh000300沪深300)
    market_index_2: str = "sz399006"        # 副监控指数(创业板指; 空字符串=不监控)
    market_warn_pct: float = -3.0
    market_crash_pct: float = -4.0
    buy_halt_file: str = "logs/kill_buy.flag"  # 买侧熔断标志(大盘crash自动写)
    market_crash_recovery_pct: float = 0.01    # 从熔断低点回升该比例自动解除
                                               # (0.01=1%; 0=当日不自动解除)
    # 买侧熔断解除冷却防抖(2026-09-18第二版点评采纳, 默认0=现行回升达阈立即解除):
    # 指数首次满足回升阈值后不立即解除, 持续观察cooldown_sec, 期间若再跌穿
    # 阈值则取消本次解除继续熔断; 持续站稳cooldown_sec才真正解除(防V型抖动中
    # 反复解除/重熔)。0=关闭防抖。
    buy_halt_recover_cooldown: float = 0.0
    # unknown挂单悬置超时升级(2026-09-18采纳): 台账中status=unknown的挂单,
    # 按attempt时间计悬置超critical_sec升CRITICAL(催人工核对), 超report_sec
    # 再做日报标记。同一升级档只触发一次(标记随attempt持久化)。
    unknown_stuck_critical_sec: float = 900.0   # 15分钟
    unknown_stuck_report_sec: float = 3600.0    # 60分钟
    # 尾盘新主动买入截止(2026-09-15用户裁定=14:55): 该时刻后不发新买单
    # (含二次挂买)。卖单/止损/F8撤单/14:57收盘集合竞价queue_only结算单
    # 全部不受影响(queue_only在pre_check旁路本门禁)。空字符串=关闭。
    buy_deadline_enable: bool = True
    buy_deadline: str = "14:55:00"
    # 单票每日重挂次数上限(2026-09-15用户裁定, 默认3; 首次挂单不计):
    # 上笔挂单canceled/unknown后的再次挂单计一次重挂, 买/卖分别计数,
    # 达上限拒绝新主动单(规则性拒单, risk_block静默); queue_only结算单
    # 旁路。信号本身寿命不限(不做信号时效衰减)。0=关闭。日20单总额度不变。
    max_requeue_per_day: int = 3


@dataclass
class NotifyConfig:
    """告警手机推送(全部留空=不推送, 只写本地alerts.jsonl)。

    群机器人(推荐, 免费无每日条数限制): 企业微信/飞书/钉钉群里添加
    "自定义机器人", 安全设置选"自定义关键词"填"告警"(消息标题统一含
    "告警"二字), 复制生成的完整webhook URL填入。
    Server酱(微信服务号): https://sct.ftqq.com 注册取SENDKEY, 免费版
    每天限5条, 仅作备选。

    安全: webhook等密钥优先从环境变量读取(QYWECHAT_WEBHOOK/FEISHU_WEBHOOK/
    DINGTALK_WEBHOOK/SERVERCHAN_KEY), 环境变量未设时才用config.yaml的值,
    避免密钥明文入库。
    """
    qywechat_webhook: str = ""
    feishu_webhook: str = ""
    dingtalk_webhook: str = ""
    serverchan_key: str = ""
    timeout: float = 8.0

    def __post_init__(self):
        import os
        self.qywechat_webhook = os.environ.get(
            "QYWECHAT_WEBHOOK", self.qywechat_webhook)
        self.feishu_webhook = os.environ.get(
            "FEISHU_WEBHOOK", self.feishu_webhook)
        self.dingtalk_webhook = os.environ.get(
            "DINGTALK_WEBHOOK", self.dingtalk_webhook)
        self.serverchan_key = os.environ.get(
            "SERVERCHAN_KEY", self.serverchan_key)


@dataclass
class MonitorConfig:
    """运行监控: 心跳/看门狗/扫描卡死/覆盖率审计/盘中日报。

    审计数据落盘 logs/events_YYYYMMDD.jsonl (JSONL, 每行一个事件):
    kind=scan(每只扫描时间线)/signal(信号生命周期)/trade(交易状态机)/
    coverage(扫描覆盖率)。回测与"系统vs券商持仓"对账均以此为准。
    """
    enable: bool = True
    heartbeat_interval: float = 30.0     # 心跳写盘间隔(秒)
    heartbeat_touch_stale: float = 90.0  # 主线程超该秒数未touch则心跳停写
                                         # (挂死检测: 进程活着但主线程卡死也会停跳)
    heartbeat_stale: float = 120.0       # 看门狗判定阈值: 心跳文件超过该秒数
                                         # 未更新 -> 推送"程序可能已死/卡死"告警
    scan_timeout: float = 5.0            # 单只股票切页等待上限(秒): 超时第1次
                                         # 重试, 第2次重建扫描页, 第3次告警
    coverage_min: float = 1.0            # 扫描覆盖率下限: <该值记异常(1.0=必须100%)
    lunch_report: bool = True            # 午盘休息(11:30-13:00)自动发盘中日报
    stuck_abort: int = 3                 # 连续N只切页失败则中止本轮(防整轮空转)
    stuck_policy: str = "abort"          # 连续切页失败处置(R5, 2026-09-15):
                                         # abort=达到stuck_abort中止整轮(默认
                                         # 现状); skip=跳过问题股继续扫下一只
    # 看门狗自动重启auto_round(2026-09-15用户裁定, 带护栏): 心跳判定主进程
    # 死亡后由看门狗自动拉起新auto_round。护栏: 每日次数上限/人工kill_switch
    # 熔断期间不重启/截止时刻后不重启/重启前先跑preflight(BLOCK则不重启)/
    # kill_buy买侧熔断不阻止重启/重启事件CRITICAL推送。
    watchdog_restart_enable: bool = True
    watchdog_restart_max_daily: int = 3
    watchdog_restart_deadline: str = "14:57:00"   # 此时刻后不再重启(让路收盘)
    watchdog_restart_state: str = "logs/watchdog_restart.json"
    watchdog_restart_config: str = ""    # 拉起时透传的config路径(空=用默认)
    # 时钟/行情时效守卫(零依赖只告警): 行情快照携带源时间戳, 源时间明显超前
    # 本机钟(疑似本机钟停/慢)或交易时段内行情陈旧时WARNING(边沿不刷屏)。
    clock_guard_enable: bool = True
    clock_skew_warn_sec: float = 30.0    # 源时间超前本机钟超该值告警
    clock_stale_warn_sec: float = 120.0  # 交易时段行情快照陈旧超该值告警
    # 运行资源守卫(每轮轮头, 零依赖只告警, 不改变交易): 磁盘不足会导致持仓
    # 原子写/截图失败, critical阈值与preflight阻断线一致并手机推送
    resource_check_enable: bool = True
    disk_warn_gb: float = 5.0            # 磁盘剩余<该值WARNING
    disk_critical_gb: float = 2.0        # 磁盘剩余<该值CRITICAL(与preflight一致)
    logs_size_warn_mb: float = 500.0     # logs目录体量超该值WARNING
    rss_warn_mb: float = 800.0           # 进程峰值内存超该值WARNING(疑似泄漏)


@dataclass
class CleanupConfig:
    """垃圾清理: TTL策略(无数据库, 文件直删)。状态文件永不清理。"""
    enable: bool = True
    screenshot_ttl_hours: int = 48      # screenshots/*.png 调试截图
    log_ttl_hours: int = 336            # scan_*.log 运行日志 (14天)
    debug_ttl_hours: int = 168          # 诊断png/ui_*.xml等 (7天)
    archive_ttl_hours: int = 2160       # 轮次results/summary档案 (90天)
    # 审计事件(信号生命周期/交易状态机/覆盖率/异常): 策略复盘与交易追责
    # 的核心数据, 每天约1MB, 保留1年(独立于90天轮次档案; 2026-09-11评估)
    event_ttl_hours: int = 8760


@dataclass
class SchemaConfig:
    """配置schema严格模式(2026-09-18第二版点评采纳, 默认关=现行warn-only)。

    strict_enable=true时, validate_config查出的、属于strict_sections段的越界/
    非法项由WARNING升级为BLOCK, main启动时拒绝运行(拼写错误的阈值会"看似生效
    实则默认值", 严格模式防带错配置开盘)。默认仍只告警不阻断。
    段名: risk=风控数值/时间越界; seal=热键守卫阈值; timing=时间格式。
    """
    strict_enable: bool = False
    strict_sections: List[str] = field(
        default_factory=lambda: ["risk", "seal", "timing"])


@dataclass
class BackupConfig:
    """每日关键数据备份(2026-09-18第二版点评采纳, 默认关)。

    auto_round每日首轮启动时打一个zip(含positions.json/trade_state.json/
    positions.snapshot.json/config.yaml/当日events/当日日报), 按retain_days
    滚动清理。备份为本地zip, 默认排除ths_cookie.txt凭据; config.yaml内含
    webhook URL, 请勿把备份目录外传。
    """
    enable: bool = False
    dir: str = "logs/backups"
    retain_days: int = 30


@dataclass
class AppConfig:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    regions: RegionsConfig = field(default_factory=RegionsConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)
    switch: SwitchConfig = field(default_factory=SwitchConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    goto_nav: GotoConfig = field(default_factory=GotoConfig)
    home_nav: HomeNavConfig = field(default_factory=HomeNavConfig)
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    hot_list: HotListConfig = field(default_factory=HotListConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    positions: PositionsConfig = field(default_factory=PositionsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    easytrader: EasytraderConfig = field(default_factory=EasytraderConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    schema: SchemaConfig = field(default_factory=SchemaConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    project_root: str = ""
    # R2 schema校验结果(2026-09-15): config.yaml里无法识别的键/非法取值在
    # 加载时收集到这里, 默认只warn不阻断(历史上未知键被静默丢弃, 拼写错误
    # 的配置项会"看似生效实则默认值"); 启动方(main.py)负责逐条打WARNING。
    schema_warnings: List[str] = field(default_factory=list)

    def resolve(self, relative: str) -> str:
        """将相对路径解析为相对于 config.yaml 所在目录的绝对路径。"""
        if os.path.isabs(relative):
            return relative
        return os.path.join(self.project_root, relative)


def _build(cls, data: dict, section: str, warnings: list):
    """按dataclass字段白名单构造配置; 未知键不丢弃而是收集warn(默认不阻断)。"""
    if not data:
        return cls()
    allowed = {f.name for f in fields(cls)}
    kwargs = {}
    for k, v in data.items():
        if k in allowed:
            kwargs[k] = v
        else:
            warnings.append(f"配置项 {section}.{k} 无法识别(拼写错误或已废弃),"
                            f"已忽略, 请检查config.yaml")
    try:
        return cls(**kwargs)
    except TypeError as e:
        warnings.append(f"配置段 {section} 取值类型错误: {e}(该段回退默认值)")
        return cls()


def _to_region(value) -> Region:
    if value and len(value) == 4:
        return tuple(int(x) for x in value)
    raise ValueError(f"区域坐标格式错误: {value}, 需要 [x1, y1, x2, y2]")


def _to_point(value) -> Point:
    if value and len(value) == 2:
        return tuple(int(x) for x in value)
    raise ValueError(f"按钮坐标格式错误: {value}, 需要 [x, y]")


_HHMMSS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def validate_config(cfg: AppConfig, tagged: bool = False):
    """R2 schema语义校验: 枚举/范围/时间格式, 默认返回问题字符串清单(warn-only)。

    只检查能离线判定的硬错误(非法枚举/越界/格式), 不改变任何运行行为;
    与未知键告警一同进入 cfg.schema_warnings, main启动时逐条WARNING。

    tagged=True: 返回 [(section, msg), ...], section∈risk/seal/timing/""(供
    严格模式 strict_violations 过滤; ""=不纳入严格阻断的枚举类问题)。
    """
    w = []

    def bad(msg, section=""):
        w.append((section, msg))

    if cfg.execution.mode not in ("paper", "auto"):
        bad(f"execution.mode={cfg.execution.mode!r}非法, 仅支持paper/auto")
    if cfg.execution.channel not in ("form", "hotkey"):
        bad(f"execution.channel={cfg.execution.channel!r}非法,"
            f"仅支持form/hotkey")
    if cfg.watchlist.sync_mode not in ("none", "cloud"):
        bad(f"watchlist.sync_mode={cfg.watchlist.sync_mode!r}非法,"
            f"仅支持none/cloud")
    if cfg.hot_list.sync_mode not in ("none", "cloud"):
        bad(f"hot_list.sync_mode={cfg.hot_list.sync_mode!r}非法,"
            f"仅支持none/cloud")
    if cfg.monitor.stuck_policy not in ("abort", "skip"):
        bad(f"monitor.stuck_policy={cfg.monitor.stuck_policy!r}非法,"
            f"仅支持abort/skip")
    if cfg.hotkey.buy_variant not in ("latest", "ask1"):
        bad(f"hotkey.buy_variant={cfg.hotkey.buy_variant!r}非法,"
            f"仅支持latest/ask1")
    if cfg.hotkey.sell_variant not in ("latest", "bid1"):
        bad(f"hotkey.sell_variant={cfg.hotkey.sell_variant!r}非法,"
            f"仅支持latest/bid1")
    r = cfg.risk
    if r.buy_deadline_enable and not _HHMMSS_RE.match(r.buy_deadline or ""):
        bad(f"risk.buy_deadline={r.buy_deadline!r}格式错误, 需HH:MM:SS",
            "timing")
    if not (0 <= r.max_requeue_per_day <= 20):
        bad(f"risk.max_requeue_per_day={r.max_requeue_per_day}越界(0-20)",
            "risk")
    for i, sess in enumerate(r.sessions or []):
        if (not isinstance(sess, (list, tuple)) or len(sess) != 2
                or not all(_HHMMSS_RE.match(str(x)) for x in sess)):
            bad(f"risk.sessions[{i}]={sess!r}格式错误, 需[HH:MM:SS,HH:MM:SS]",
                "timing")
    hk = cfg.hotkey
    for name, val in (("price_guard_buy_pct", hk.price_guard_buy_pct),
                      ("price_guard_sell_pct", hk.price_guard_sell_pct)):
        if not (0 < val < 0.2):
            bad(f"hotkey.{name}={val}越界(应在(0,0.2)之间)", "seal")
    if hk.f6_fallback_threshold < 0 or hk.f6_fallback_threshold > 10:
        bad(f"hotkey.f6_fallback_threshold={hk.f6_fallback_threshold}越界(0-10)",
            "seal")
    if hk.closing_limit_key_enable:
        for _kn, _kv in (("closing_buy_limit_key", hk.closing_buy_limit_key),
                         ("closing_sell_limit_key", hk.closing_sell_limit_key)):
            if _kv and not re.match(r"^\{[A-Za-z0-9+]+\}$", _kv):
                bad(f"hotkey.{_kn}={_kv!r}格式错误, 需形如{{F9}}的键位串",
                    "seal")
    if not (0 < cfg.watchlist.delete_guard_max_pct <= 1):
        bad(f"watchlist.delete_guard_max_pct="
            f"{cfg.watchlist.delete_guard_max_pct}越界(应在(0,1])", "risk")
    wc = cfg.watchlist
    if not (0 < wc.delete_guard_hard_pct <= 1):
        bad(f"watchlist.delete_guard_hard_pct={wc.delete_guard_hard_pct}"
            f"越界(应在(0,1])", "risk")
    elif wc.delete_guard_hard_pct < wc.delete_guard_max_pct:
        bad("watchlist.delete_guard_hard_pct不应小于delete_guard_max_pct",
            "risk")
    if not (0 < wc.delete_guard_confirm_wait <= 300):
        bad(f"watchlist.delete_guard_confirm_wait={wc.delete_guard_confirm_wait}"
            f"越界(应在(0,300]秒)", "risk")
    if not (0 <= wc.delete_guard_confirm_max_diff <= 20):
        bad(f"watchlist.delete_guard_confirm_max_diff="
            f"{wc.delete_guard_confirm_max_diff}越界(0-20)", "risk")
    pc = cfg.positions
    for _pn, _pv in (("loss_warn_pct", pc.loss_warn_pct),
                     ("loss_critical_pct", pc.loss_critical_pct)):
        if not (-0.9 < _pv < 0):
            bad(f"positions.{_pn}={_pv}越界(应在(-0.9,0)之间)", "risk")
    if pc.loss_warn_pct < pc.loss_critical_pct:
        bad("positions.loss_warn_pct应大于loss_critical_pct"
            "(如-0.10先于-0.15触发)", "risk")
    if hk.cancel_fail_window_sec <= 0:
        bad("hotkey.cancel_fail_window_sec必须为正数", "seal")
    if not (1 <= hk.cancel_fail_max <= 20):
        bad(f"hotkey.cancel_fail_max={hk.cancel_fail_max}越界(1-20)", "seal")
    crd = hk.closing_recheck_delays
    if (not isinstance(crd, (list, tuple)) or not crd
            or any((not isinstance(x, int) or x < 0) for x in crd)):
        bad("hotkey.closing_recheck_delays需为非空正整数秒列表", "seal")
    if not (0 <= r.buy_halt_recover_cooldown <= 3600):
        bad(f"risk.buy_halt_recover_cooldown={r.buy_halt_recover_cooldown}"
            f"越界(0-3600秒)", "risk")
    if r.unknown_stuck_critical_sec <= 0 or r.unknown_stuck_report_sec <= 0:
        bad("risk.unknown_stuck_*_sec必须为正数", "risk")
    elif r.unknown_stuck_critical_sec >= r.unknown_stuck_report_sec:
        bad("risk.unknown_stuck_critical_sec应小于report_sec", "risk")
    if not (1 <= cfg.backup.retain_days <= 3650):
        bad(f"backup.retain_days={cfg.backup.retain_days}越界(1-3650)", "risk")
    m = cfg.monitor
    if not _HHMMSS_RE.match(m.watchdog_restart_deadline or ""):
        bad(f"monitor.watchdog_restart_deadline="
            f"{m.watchdog_restart_deadline!r}格式错误, 需HH:MM:SS", "timing")
    if not (0 <= m.watchdog_restart_max_daily <= 20):
        bad(f"monitor.watchdog_restart_max_daily="
            f"{m.watchdog_restart_max_daily}越界(0-20)", "risk")
    if m.clock_skew_warn_sec <= 0 or m.clock_stale_warn_sec <= 0:
        bad("monitor.clock_*_warn_sec必须为正数", "seal")
    return w if tagged else [msg for _, msg in w]


def strict_violations(cfg: AppConfig) -> list:
    """严格模式下应升级为BLOCK的schema问题清单(2026-09-18采纳)。

    strict_enable=false时返回[]; 开启时仅返回段名命中strict_sections的问题。
    main启动据此拒绝运行, 默认关闭故现行warn-only行为不变。
    """
    sc = cfg.schema
    if not getattr(sc, "strict_enable", False):
        return []
    sections = set(sc.strict_sections or [])
    return [msg for sec, msg in validate_config(cfg, tagged=True)
            if sec in sections]


def load_config(config_path: str) -> AppConfig:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    warnings: list = []

    def B(cls, section):
        return _build(cls, raw.get(section), section, warnings)

    cfg = AppConfig(
        device=B(DeviceConfig, "device"),
        paths=B(PathsConfig, "paths"),
        buttons=B(ButtonsConfig, "buttons"),
        switch=B(SwitchConfig, "switch"),
        detection=B(DetectionConfig, "detection"),
        vision=B(VisionConfig, "vision"),
        ui=B(UIConfig, "ui"),
        goto_nav=B(GotoConfig, "goto_nav"),
        home_nav=B(HomeNavConfig, "home_nav"),
        watchlist=B(WatchlistConfig, "watchlist"),
        hot_list=B(HotListConfig, "hot_list"),
        universe=B(UniverseConfig, "universe"),
        positions=B(PositionsConfig, "positions"),
        execution=B(ExecutionConfig, "execution"),
        easytrader=B(EasytraderConfig, "easytrader"),
        hotkey=B(HotkeyConfig, "hotkey"),
        risk=B(RiskConfig, "risk"),
        notify=B(NotifyConfig, "notify"),
        monitor=B(MonitorConfig, "monitor"),
        cleanup=B(CleanupConfig, "cleanup"),
        schema=B(SchemaConfig, "schema"),
        backup=B(BackupConfig, "backup"),
        project_root=os.path.dirname(os.path.abspath(config_path)),
    )
    warnings.extend(validate_config(cfg))
    cfg.schema_warnings = warnings
    for msg in warnings:
        logging.getLogger("config").warning(msg)

    regions = _build(RegionsConfig, raw.get("regions"), "regions", warnings)
    regions.title_region = _to_region(regions.title_region)
    regions.kline_region = _to_region(regions.kline_region)
    # latest_k_region: auto/None = 从kline_region自动派生: y与图表完全一致(单一样本源),
    # x取右缘120px窄列(覆盖45px角标+归属带)。YAML中也可显式写死四元组覆盖。
    if regions.latest_k_region is None or regions.latest_k_region == "auto":
        kx1, ky1, kx2, ky2 = regions.kline_region
        regions.latest_k_region = (max(0, kx2 - 120), ky1, kx2, ky2)
    else:
        regions.latest_k_region = _to_region(regions.latest_k_region)
    regions.title_code_region = _to_region(regions.title_code_region)
    cfg.regions = regions

    buttons = cfg.buttons
    buttons.next_stock = _to_point(buttons.next_stock)
    buttons.prev_stock = _to_point(buttons.prev_stock)
    return cfg
