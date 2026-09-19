# 第二阶段：PC端模拟炒股自动化交易

## Context

第一阶段已完成：手机ADB采集多空信号 + 云自选同步 + Cookie保活。

用户决策：**手机只做信号识别，交易走PC端同花顺客户端**。两者通过股票代码关联，互不干扰——手机扫描不中断，PC端独立下单。

## 你需要提前准备的东西

1. **安装同花顺PC客户端**（官网 https://www.10jqka.com.cn/ 下载"同花顺免费版"）
2. **登录同花顺账号**（和手机App同一个账号）
3. **进入模拟炒股**：菜单栏"委托"→"模拟炒股"→开通（免费，初始20万模拟金）
4. **手动试一次买入和卖出**，确认模拟交易功能正常
5. **关键设置**（否则自动化会出问题）：
   - 系统设置→界面设置：界面不操作超时时间设为**0**
   - 系统设置→交易设置：默认买入价格/数量/卖出价格/数量都设为**空**
6. **安装Tesseract OCR**（用于登录验证码识别，可选但推荐）：
   - 下载 https://github.com/UB-Mannheim/tesseract/releases
   - 安装到默认路径，确保命令行 `tesseract` 可用

完成后告诉我，我帮你验证环境。

## 技术方案

### 架构

```
手机ADB(信号采集) ──→ positions.json ──→ PC端同花顺客户端(下单)
     ↑ 不中断扫描         ↑ 唯一事实源         ↑ easytrader库
                                          ↓
                                    trade_state.json(风控状态)
```

- 手机：只做K线信号采集，不碰交易页面，扫描不中断
- PC端：用 `easytrader` 库操作同花顺客户端窗口完成下单
- 关联：通过 `positions.json` 的股票代码关联两端

### 交易流程

```
[扫描中] 手机检测到股票X信号=LONG → 写入StockResult
  ↓
[决策] DecisionEngine.decide(X) → Action(BUY, X)
  ↓
[执行] DecisionEngine.execute(action):
  - mode=auto → EasytraderClient.execute_order(X, BUY, price, qty)
  - 风控预检(kill_switch/时段/日限额/冷却)           [PC侧, <0.1s]
  - easytrader.buy(X, price=卖一价, amount=100)       [~3-5s]
  - 读委托回执: entrust_no / 成交价
  - 成交才建仓: positions.add(X, entry_price=成交价)
  ↓
[继续扫描] 扫描从未中断
```

**关键优势**：交易完全在PC端完成，不占手机，扫描零中断。单次交易3-5s（vs 手机方案47s）。

### 新增/修改文件

| 文件 | 改动 |
|---|---|
| `trader/easytrader_client.py` | **新建** EasytraderClient: 封装easytrader，实现execute_order接口 |
| `trader/risk_control.py` | **新建** RiskController: kill_switch/时段/限额/冷却 |
| `decision/decision.py` | **改** execute(): auto模式先下单成功再改positions；Action加qty字段 |
| `scanner/scanner.py` | **改** scan_loop() 加 on_signal 回调，主轮信号触发时同步调决策 |
| `scanner/scheduler.py` | **改** run_round() 传 on_signal 回调 |
| `config.py` + `config.yaml` | **改** 新增 EasytraderConfig/RiskConfig 配置段 |
| `main.py` | **改** auto模式装配 EasytraderClient + RiskController |

### EasytraderClient 接口

```python
class EasytraderClient:
    """通过easytrader操作同花顺PC客户端完成模拟炒股下单。"""
    
    def __init__(self, exe_path, tesseract_cmd=None):
        self.user = easytrader.use('universal_client')
        self.user.connect(exe_path)
    
    def execute_order(self, code, action, price=0.0, qty=100) -> dict:
        """下单。返回 {ok, filled_price, entrust_no, error}。
        price=0时用当前价（easytrader自动取五档卖一价）。
        """
        if action == "BUY":
            r = self.user.buy(code, price=price, amount=qty)
        elif action == "SELL":
            r = self.user.sell(code, price=price, amount=qty)
        return {"ok": True, "entrust_no": r.get("entrust_no"),
                "filled_price": price, "mode": "easytrader"}
    
    def get_position(self) -> list:
        """查询当前持仓（easytrader.balance/position）。"""
        return self.user.position
    
    def cancel(self, entrust_no) -> bool:
        """撤单。"""
        self.user.cancel_entrust(entrust_no)
        return True
```

### 风控检查清单

1. kill_switch（`logs/kill_switch.flag` 存在即熔断）
2. 交易时段（工作日 9:30-11:30 / 13:00-15:00）
3. 当日下单数（≤20）
4. 同股冷却（300秒内不重复下单）
5. 涨跌停校验（用easytrader.balance或持仓文件校验）
6. 委托回执确认（entrust_no非空）

### 失败降级

**宁可漏单不可错建仓/错清仓**：
- 任何环节失败 → ALERT告警 + 不改positions
- BUY失败：下轮再遇LONG重试（受冷却约束）
- SELL失败：持仓保留，下轮SHORT重试

### scan_loop 补 on_signal 回调

当前架构缺口：主轮scan_loop只收集结果不触发决策。auto模式需补上：

```python
# scanner.py scan_loop 签名扩展
def scan_loop(self, max_stocks, ..., on_signal=None):
    ...
    r = self._detect_on_frame(...)
    if on_signal and r.signal in (LONG, SHORT):
        on_signal(r)  # 同步执行决策+交易，扫描自然暂停（PC端交易3-5s）
```

### config 新增

```yaml
easytrader:
  exe_path: ""              # 同花顺xiadan.exe路径，如 "C:/同花顺/xiadan.exe"
  tesseract_cmd: ""         # Tesseract OCR路径（验证码识别，可选）
  connect_timeout: 10

risk:
  enable: true
  kill_switch_file: logs/kill_switch.flag
  max_orders_per_day: 20
  default_qty: 100
  cooldown_seconds: 300
  state_file: logs/trade_state.json
  sessions:
    - ["09:30:00", "11:30:00"]
    - ["13:00:00", "15:00:00"]

execution:
  mode: paper  # paper / auto(auto需easytrader.exe_path配置)
```

### 实施步骤

1. **环境验证**：你准备好后，我验证easytrader能否连接同花顺客户端、能否下单
2. **EasytraderClient + RiskController**：实现下单接口和风控
3. **decision.py改造**：execute() auto模式先下单后改positions
4. **scan_loop集成**：加on_signal回调
5. **真机冒烟**：auto模式跑一轮，观察单次BUY
6. **降级演练**：kill_switch/非时段验证
