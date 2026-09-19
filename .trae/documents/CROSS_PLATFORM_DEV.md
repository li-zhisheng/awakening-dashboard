# 跨平台开发约定

## 目标运行环境
本项目最终产物**只在 Windows 上运行实盘**（同花顺客户端下单）。
开发工作 70% 在 macOS、30% 在 Windows，代码必须能在两台机器无缝切换。

## 硬性规则（所有新代码必须遵守）

### 1. 平台专有依赖必须延迟导入
以下库只在 Windows 存在，**禁止在模块顶层 import**，必须下沉到函数内部：
- `pywinauto`（窗口/控件操作）
- `easytrader`（同花顺客户端下单）
- `win32gui` / `win32process` / `win32api` / `win32con` / `win32ui`（窗口枚举/鼠标）
- `ctypes.windll`（Windows API，注意 `import ctypes` 本身在 Mac 上可导入，但 `ctypes.windll` 不存在）

正确范式见 `keepawake.py` 的 `IS_WINDOWS` 守卫。

### 2. 平台守卫范式
任何调用 Windows 专有 API 的函数，入口必须先判断：
```python
import sys
IS_WINDOWS = sys.platform == "win32"

def some_windows_only_func():
    if not IS_WINDOWS:
        log.info("非 Windows 平台, 跳过")
        return None
    import ctypes  # 延迟导入
    ctypes.windll.kernel32.SomeApi(...)
```

### 3. 路径与外部命令
- 禁止硬编码 `C:/...` 或 `.exe` 路径，改用环境变量或 `config.yaml`
- 禁止直接调用 `tasklist` / `taskkill` 等 Windows 专有命令，必须先 `IS_WINDOWS` 判断
- 文件路径用 `os.path.join` 或 `pathlib`，不用反斜杠字面量

### 4. 测试方式
- **macOS 上**：`python main.py selftest` 跑 104 项回归（pywinauto 相关项已设计为不触发连接，只测纯逻辑）
- **Windows 上**：`selftest` + `python main.py auto_round` 实盘集成
- 改完任何逻辑层代码，Mac 上 selftest 全绿才能提交

### 5. 运行时状态文件不入 git
以下文件是机器本地状态，每台机器独立，已被 `.gitignore` 排除：
`positions.json`、`logs/*.json`、`logs/*.jsonl`、`logs/*.csv`、`logs/*.log`、
`screenshots/`、`ths_cookie.txt`、`__pycache__/`

### 6. 依赖管理
- `requirements.txt` = 跨平台基础依赖（Mac/Win 都装）
- `requirements-win.txt` = Windows 额外依赖（pywinauto/easytrader/pywin32）
- Mac 上只装 `requirements.txt` 即可起 `selftest`

## 不可在 Mac 验证、必须 Windows 验证的部分
- `HotkeyTrader` 真实下单（`_connect_hexin` 起的 pywinauto 链）
- F1-F8 热键生效、F6 持仓验证
- `xiadan.exe` 表单兜底
- `preflight` 真机检查（`tasklist`/hexin 进程/屏保）

这部分改了代码后，Mac 上 selftest 验证纯逻辑，Windows 上真机验证连接。

## 每次在 Mac 开新会话的第一句话
> 读 `.trae/documents/CROSS_PLATFORM_DEV.md`，这是项目的跨平台开发约定，本次会话所有改动必须遵守。
