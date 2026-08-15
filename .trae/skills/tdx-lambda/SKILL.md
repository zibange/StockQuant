---
name: 通达信Lambda
description: "通达信Lambda。用户用自然语言描述策略意图，自动生成符合通达信量化云平台的函数式 pylambda.api 策略代码（只写 init/handle_bar 等回调、不写 import、不写 BACKTEST_CONFIG、不落文件），并通过本机 17709 HTTP 服务调用 send_source 接口发送策略源码，通达信客户端收到消息后自动打开量化云回测平台并将代码注入编辑区；用户手动在客户端点『运行回测』。基于 tdx-backtest-strategy 的策略生成规范与 tdx-tq-local 的客户端唤起机制。"
agent_created: true
version: "1.1.0"
category: "量化"
tags: ["通达信", "云回测", "策略生成", "量化", "pylambda.api", "send_source", "代码注入", "函数式策略", "因子选股"]
triggers:
  - "在云回测里写一个策略"
  - "生成一个云回测策略"
  - "帮我写个策略发到云回测"
  - "把策略代码发到通达信"
  - "云回测平台策略"
  - "直接生成到云回测"
  - "帮我做个云回测"
---

# 通达信Lambda v1.1.0

## 这个 Skill 做什么

用户用大白话描述一个交易策略（如"5日线上穿20日线买入、下穿卖出，标的贵州茅台"），本 Skill：

1. **解析意图** → 生成函数式 pylambda 策略代码；
2. **前置检查** → 确认通达信客户端已就绪（复用 tdx-tq-local 的四步检查）；
3. **发送代码** → 用 `send_source` 把策略源码通过 17709 HTTP 服务发给通达信客户端，**客户端收到消息后自动打开量化云回测平台并将代码注入编辑区**（**不生成任何本地文件、不需要手动打开版面**）；
4. **等待用户** → 用户自己在客户端点「运行回测」并查看结果。

> 与 `tdx-backtest-strategy` 的关键区别：
> - **不写 `BACKTEST_CONFIG`**：回测参数（标的、区间、频率、初始资金）由用户在云回测平台 UI 表单里填，不是写进代码。
> - **不落文件**：代码通过 `send_source` 的 `py_code` 字段以源码文本形式直接传过去，不是写成 `.py` 再 `run_remote_backtest.py` 提交。
> - **不自动回测**：`send_source` 用 `handle_type=0` 仅传输，回测由用户手动触发。
> - **不需要 exec_to_tdx 打开版面**：`send_source` 发送后客户端自动唤起云回测平台，无需额外调用 `exec_to_tdx(padcode_...)`。

> 与 `tdx-tq-local` / `tdx-quant` 的关系：复用它们的「本机 17709 HTTP JSON-RPC 服务」与四步前置检查；但本 Skill 的目标接口只有 `send_source`（发送策略源码，客户端自动打开云回测平台并注入编辑区），不需要 `exec_to_tdx` 或其他页面唤起操作。

---

## 执行前强制检查流程（必须按顺序完整执行，不可跳过）

完全复用 `tdx-tq-local` v1.0.13 的四步检查。任意一步不通过，**不得继续生成与发送**。

### 第零步：检查操作系统是否为 Windows

该 Skill 仅支持 Windows（依赖通达信 Windows 客户端与 17709 本地服务）。

```python
import platform
print(platform.system())  # 期望 "Windows"
```

非 Windows → 立即终止，提示用户切换到 Windows。

### 第一步：检查通达信是否已安装（需较新、支持 TQ 策略的版本）

用 Python + `winreg` 依次检查以下注册表键，**找到至少一个即视为已安装**：

```
HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64
HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信专业版
HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(量化模拟)
HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(测试)
```

未安装 → 用 `urllib.request.urlretrieve` 从 `https://data.tdx.com.cn/level2/new_tdx64.exe` 下载到当前 workspace，**提示用户手动运行安装**，安装完成前不得继续。

### 第二步：检查 `TdxW.exe` 是否在运行

```bash
tasklist 2>/dev/null | grep -i "TdxW"
```

未运行 → 提示用户先启动通达信客户端并登录到主界面。

### 第三步：验证 17709 HTTP 服务连通性

```bash
curl -s --connect-timeout 3 -X POST "http://127.0.0.1:17709/" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"id":1,"method":"get_match_stkinfo","params":{"key_word":"茅台"}}'
```

连接失败 → 提示用户确认通达信已完全启动并登录到主界面（服务由客户端在本机 17709 端口监听）。

> **注意**：`send_source` 接口需要较新的客户端构建（v1.0.13 文档新增）。若返回 `-32601 MCP不支持该tqcenter方法名`，说明当前监听 17709 的客户端版本不支持该接口——请确认运行的是支持 `send_source` 的最新构建（旧测试版可能缺此接口）。

---

## 核心规则（策略代码生成）

生成的函数式 pylambda 代码必须遵守以下规则（依据《通达信云回测平台API客户使用说明》v1.22/v1.23 与 `tdx-backtest-strategy` v5.3.0）：

1. **不要写任何 `import`** —— 云平台编辑器自动注入 `pylambda.api` 全部 API 与常用库（`datetime`/`np`/`pd`/`math`/`json`/`re`/`collections` 等）。不要写 `from pylambda.api import *`、不要写 `from ... import ...`、不要写 `import types`。
2. **不要写 `BACKTEST_CONFIG`** —— 这是本 Skill 与 `tdx-backtest-strategy` 的根本区别。回测参数（标的/区间/频率/初始资金）由用户在云回测平台 UI 表单配置，**不写进代码**。
3. **不要写完整脚本包装** —— 不写 `load_lambda(...)` / `run_backtest(...)` / `run_signal(...)` / `types.ModuleType(...)` / `if __name__ == "__main__":`。这些都由云平台 runner 自动处理。
4. **只写回调 + 可选全局变量** —— `init(context)` / `before_trading(context)` / `handle_bar(context, bar_dict)` / `after_trading(context)` / `on_strategy_end(context)` / `on_order` / `on_trade`。
5. **标的写法**：
   - **用户说了具体股票（代码或名称，如"贵州茅台"）**：**直接把代码写进策略**，例如 `context.stock = "600519.SH"`，不要写 `context.universe[0]` 这类占位、也不要依赖界面填标的——代码自身完整可跑。
   - **全市场 / 板块 / 指数成分（如"沪深300"、"中证500"、"全市场"）**：用 `context.universe` 遍历（`for stock in context.universe:`），平台注入成分，无需我处理界面参数。
   - **不写 `BACKTEST_CONFIG`、不操心界面侧的标的范围 / 区间 / 频率 / 初始资金**——这些由云回测平台界面控制，我只在代码里把"用户说清楚的部分"写对即可。
6. **代码格式大写带后缀** —— `000002.SZ` / `600000.SH` / `000300.SH` / `430047.BJ`。
7. **UTF-8 编码** —— 策略源码通过 JSON 的 `py_code` 字段传输，必须是标准 UTF-8 文本。
8. **原生指标直接用** —— `MA`/`EMA`/`MACD`/`CROSS`/`REF`/`HHV`/`LLV` 等 C++ 原生指标在干净策略里由 runner 自动注入，推荐直接用（如 `CROSS(MA(CLOSE,5), MA(CLOSE,20))`），无需 pandas 重写。
9. **禁用的内置/能力** —— 不要使用 `open`/`input`/`eval`/`exec`/`compile`/`__import__`/`globals`/`locals`/`getattr`/`setattr`/`breakpoint`；不要读写本地文件、访问网络、启进程；不要 `pd.read_*`/`to_csv`/`to_excel`、不要 `np.load`/`save` 等 IO（纯内存 `rolling`/`ewm`/`DataFrame` 正常可用）。调试用 `print(..., flush=True)`。

---

## 策略代码生成规范

### 自然语言 → 代码 映射表

| 用户意图 | 生成要点 |
|---|---|
| "标的 600519.SH / 贵州茅台" | 代码直接写 `context.stock = "600519.SH"`（单标的写死，不占位） |
| "全市场 / 沪深300 / 中证500" | 代码用 `context.universe` 遍历（平台注入成分，不操心界面参数） |
| "5日上穿20日买入" | `golden = CROSS(MA(CLOSE,5), MA(CLOSE,20))` → `order_target_percent(stock, 0.95)` |
| "5日下穿20日卖出" | `death = CROSS(MA(CLOSE,20), MA(CLOSE,5))` → `order_target(stock, 0)`（需先有持仓） |
| "MACD 金叉" | `DIF`/`DEA` 由 `MACD(...)` 取，或 pandas 算 `ema12-ema26`/`dea`，金叉 `dif_now>dea_now and dif_prev<=dea_prev` |
| "RSI 低于30买、高于70卖" | pandas 算 `rsi`，阈值分支下单 |
| "布林带下轨买入、上轨卖出" | `ma±2*std` 上下轨，价格突破分支 |
| "等权/按权重调仓" | `equal_weight(selected)` / `order_target_weights(weights, close_missing=True)` |
| "以沪深300为基准" | `set_benchmark("000300.SH")` |
| "回测区间/频率/初始资金" | **不写进代码**（由云回测平台界面配置，本 Skill 不管） |
| "排除ST / 停牌" | `bar.is_st` / `bar.paused` 过滤（在 `handle_bar` 里对 `bar_dict[stock]` 判断） |
| "成交额大于X" | `bar.turnover`（成交额，元）过滤 |

### 最小代码模板（单标的，供生成时参照）

```python
def init(context):
    # 主标的：用户指定的单只股票，直接写死代码
    context.stock = "600519.SH"   # 贵州茅台
    set_benchmark("000300.SH")
    set_commission(open_tax=0.0, open_commission=0.0003,
                   close_commission=0.0003, close_tax=0.001, min_commission=5.0)
    set_execution("next_open")   # 市价单次 bar 开盘成交（Lambda 默认）


def handle_bar(context, bar_dict):
    stock = context.stock
    if stock not in bar_dict:
        return
    bar = bar_dict[stock]
    if bar.is_st or bar.paused:
        return
    hist = attribute_history(stock, 30, "1d", ["close"])
    if len(hist) < 20:
        return
    close = hist["close"]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma5_prev = close.rolling(5).mean().iloc[-2]
    ma20_prev = close.rolling(20).mean().iloc[-2]
    has_position = stock in context.portfolio.positions
    if ma5 > ma20 and ma5_prev <= ma20_prev:
        order_target_percent(stock, 0.95)
    elif ma5 < ma20 and ma5_prev >= ma20_prev and has_position:
        order_target(stock, 0)
```

### 多标的 / 全市场模板（遍历 `context.universe`）

```python
def init(context):
    context.stocks = context.universe   # 全市场/板块：平台注入成分，遍历即可
    set_benchmark("000300.SH")


def handle_bar(context, bar_dict):
    for stock in context.stocks:
        if stock not in bar_dict:
            continue
        bar = bar_dict[stock]
        if bar.is_st or bar.paused:
            continue
        hist = attribute_history(stock, 30, "1d", ["close"])
        if len(hist) < 20:
            continue
        close = hist["close"]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        if ma5 > ma20:
            order_target_percent(stock, 0.1)
        else:
            order_target(stock, 0)
```

### 常用策略模式（均不写 BACKTEST_CONFIG / import）

→ 均线交叉、RSI 超买超卖、MACD 金叉死叉、布林带突破、因子选股（`calc_factor_batch` + `equal_weight` + `order_target_weights`）、分钟线（VWAP 偏离回归）等完整模板，直接复用 `tdx-backtest-strategy` v5.3.0 §「常见策略模板」的实现逻辑，只做两处改动：
1. **删除顶部 `BACKTEST_CONFIG = {...}` 整块**；
2. **单标的策略保留/写死用户指定的代码**（如 `context.stock = "600519.SH"`），不要改成 `context.universe[0]` 占位；全市场/板块策略才用 `context.universe` 遍历。

例如「原 MACD 模板」改为：

```python
def init(context):
    context.stock = "600519.SH"
    set_benchmark("000300.SH")


def handle_bar(context, bar_dict):
    stock = context.stock
    if stock not in bar_dict:
        return
    hist = attribute_history(stock, 60, "1d", ["close"])
    if len(hist) < 35:
        return
    close = hist["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    dif_now, dea_now = float(dif.iloc[-1]), float(dea.iloc[-1])
    dif_prev, dea_prev = float(dif.iloc[-2]), float(dea.iloc[-2])
    has_position = stock in context.portfolio.positions
    if dif_now > dea_now and dif_prev <= dea_prev:
        order_target_percent(stock, 0.95)
    elif dif_now < dea_now and dif_prev >= dea_prev and has_position:
        order_target(stock, 0)
    record(dif=dif_now, dea=dea_now)
```

---

## 发送策略代码到云回测平台

本 Skill 发送环节**只有一步**：通过 `send_source` 发送策略源码。通达信客户端收到消息后会**自动打开量化云回测平台并将代码注入编辑区**，无需额外调用 `exec_to_tdx` 或其他页面唤起操作。

### 注入策略代码（send_source，唯一必须步骤）

**使用 `send_source` 发送策略源码**，不要用 `send_file` / `send_bt_data` / `exec_to_tdx` 代替（`tdx-tq-local` v1.0.13 §`send_source` 明确要求）。

```json
{
  "id": 1,
  "method": "send_source",
  "params": {
    "py_code": "<此处放完整 Python 策略源码文本，不是文件路径>",
    "handle_type": 0
  }
}
```

参数说明：
- `py_code`（必填，str）：**完整 Python 源码文本**，即上一步生成的函数式策略代码全文。
- `handle_type`（选填，int）：`0` 仅传输 / `1` 传输后提交回测 / `2` 传输后提交执行。**本 Skill 固定用 `0`**——只把代码注入编辑区，由用户手动点「运行回测」。

> 客户端行为：收到 `send_source` 消息后，通达信客户端会自动打开「量化云回测平台」并将 `py_code` 内容填入代码编辑区。**不需要也不应该再调用 `exec_to_tdx(padcode_...)` 手动打开版面。**

> 源码按 UTF-8 编码后受传输上限约束；代码过大时拆分或精简后再传。

### 调用示例

**curl：**

```bash
curl -s -X POST "http://127.0.0.1:17709/" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"id":1,"method":"send_source","params":{"py_code":"def init(context):\n    context.stock = "600519.SH"\n    set_benchmark(\"000300.SH\")\n\ndef handle_bar(context, bar_dict):\n    pass","handle_type":0}}'
```

**Python（推荐，便于把完整源码塞进 JSON，且自动 ensure_ascii=False 保中文）：**

```python
import json
import urllib.request

strategy_code = """def init(context):
    context.stock = "600519.SH"
    set_benchmark("000300.SH")
    set_commission(open_tax=0.0, open_commission=0.0003,
                   close_commission=0.0003, close_tax=0.001, min_commission=5.0)

def handle_bar(context, bar_dict):
    stock = context.stock
    if stock not in bar_dict:
        return
    hist = attribute_history(stock, 30, "1d", ["close"])
    if len(hist) < 20:
        return
    close = hist["close"]
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma5_prev = close.rolling(5).mean().iloc[-2]
    ma20_prev = close.rolling(20).mean().iloc[-2]
    has_position = stock in context.portfolio.positions
    if ma5 > ma20 and ma5_prev <= ma20_prev:
        order_target_percent(stock, 0.95)
    elif ma5 < ma20 and ma5_prev >= ma20_prev and has_position:
        order_target(stock, 0)
"""

payload = {
    "id": 1,
    "method": "send_source",
    "params": {"py_code": strategy_code, "handle_type": 0},
}
req = urllib.request.Request(
    "http://127.0.0.1:17709/",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    result = json.loads(resp.read().decode("utf-8"))
print(result)
```

### 返回处理

- 正常：`result.ErrorId == "0"`，`result.Value` 含云回测平台接收结果。
- 异常：`result.ErrorId != "0"` 或 `result.error` 有内容 → 把错误信息原样反馈给用户，不要猜测；常见为源码含 `import` / 禁用内置 / 编码非 UTF-8。

---

## 端到端工作流

### Step 1：解析用户意图
- 买卖条件（指标、阈值、交叉）
- 标的（具体股票直接写进代码，如 `context.stock = "600519.SH"`；全市场/板块则遍历 `context.universe`）
- 技术流派（均线/MACD/RSI/布林/因子）
- 回测区间/频率/初始资金（**不写进代码**，由云回测平台界面配置，本 Skill 不管）

### Step 2：生成函数式策略代码
按「核心规则」与「常见策略模式」生成纯回调代码：不写 import、不写 BACKTEST_CONFIG；单标的直接写股票代码，全市场/板块用 `context.universe` 遍历。

### Step 3：前置检查
执行「执行前强制检查流程」五步，全部通过才继续。

### Step 4：发送代码（send_source）
- 调 `send_source(py_code=源码, handle_type=0)` 发送策略源码；
- **客户端收到消息后自动打开量化云回测平台并将代码注入编辑区**，无需额外操作。

### Step 5：提示用户手动回测
回复用户：
1. 策略代码已注入云回测平台编辑区；
2. 回测区间 / 频率 / 初始资金等由用户在云回测平台界面自行配置（本 Skill 不处理界面参数）；
3. 在客户端点「运行回测」查看结果；
4. 附上生成的完整源码供用户核对。

---

## 官方 API 参考（函数式模型，自动提供，不要 import）

以下均直接调用。详细字段见 `tdx-backtest-strategy` v5.3.0 §6，要点速记：

- **生命周期**：`init(context)` / `before_trading(context)` / `handle_bar(context, bar_dict)`（推荐）/ `after_trading(context)` / `on_strategy_end(context)`；`run_daily(func, time_rule="every_bar")` / `run_weekly` / `run_monthly`。
- **上下文**：`context.current_dt` / `context.universe`（股票池）/ `context.portfolio.positions` / `context.portfolio.available_cash` / `context.params`（平台表单参数，只读）。
- **账户/持仓**：`context.portfolio.total_value` / `positions[symbol].amount` / `.avg_cost` / `.last_price`。
- **当前 Bar**：`bar_dict[symbol].close/open/high/low/volume/turnover`(成交额)/`high_limit`/`low_limit`/`paused`/`is_st`。
- **设置**：`set_benchmark` / `set_commission` / `set_slippage` / `set_execution("close"|"next_open")` / `set_volume_limit` / `set_option("avoid_future_data", True)` / `set_holding_stocks`。
- **行情**：`attribute_history(security, count, unit, fields)`（最常用）/ `get_current([symbol])[symbol]` / `get_stock_list(market=5)` / `get_stock_list_in_sector` / `get_index_stocks` / `get_industry_stocks`(中文行业名) / `get_concept_stocks`(中文概念名) / `calc_factor_batch`。
- **原生指标**：`MA`/`EMA`/`MACD`/`CROSS`/`REF`/`HHV`/`LLV`/`SMA`/`WMA`/`CLOSE`/`OPEN`/`HIGH`/`LOW`/`VOL`/`AMO`（runner 自动注入，直接用）。
- **交易**：`order(symbol, amount)` / `order_value` / `order_target(symbol, amount)`（0 清仓）/ `order_percent` / `order_target_percent(symbol, pct)` / `order_target_weights(weights, close_missing=True)` / `equal_weight` / `normalize_weights` / `cancel_order_all`。
- **记录/日志**：`record(**kwargs)` / `log.info(...)` / `g`（全局变量）/ `print(..., flush=True)`。

> 注意：`get_index_weight` 与 `handle_tick` 暂不支持（抛 `NotImplementedError`）。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `imports are not allowed` | 代码里写了 `import` | 删掉所有 import，用平台自动提供的名字 |
| `forbidden builtin` | 用了禁用内置（如 `eval`/`exec`） | 改普通逻辑 |
| `证券不存在或未加载` | UI 标的池没包含策略查询的证券 | 提醒用户在云回测 UI 把该标的加入股票池 |
| `回测股票池为空` | UI 标的表单为空且策略无有效池 | 提醒用户在 UI 填标的/板块 |
| `send_source` 返回非 0 ErrorId | 源码非 UTF-8 或含禁用语法 | 确保 UTF-8、去 import/禁用内置、重试 |
| `send_source` 返回 `ErrorId:-32601`/`MCP不支持该tqcenter方法名` | **多个 TdxW 进程并存、旧构建占用 17709 端口**（请求打到了缺该接口的旧客户端），或当前客户端版本不支持 `send_source` | 用 `netstat -ano \| findstr 17709` 找监听 PID → `Get-CimInstance Win32_Process -Filter "ProcessId=该PID"` 看其 `ExecutablePath`；**关闭占用端口的旧通达信客户端**或升级到支持 `send_source` 的最新构建，让该构建独占 17709 后再重试 |
| 回测不生效 | 用户在 UI 没点「运行回测」 | 本 Skill 只发送代码，`handle_type=0` 不自动回测，需用户手动点 |

---

## 与用户交互（完成后告知）

1. 策略代码已通过 `send_source` 注入云回测平台编辑区（未落本地文件）；
2. 回测区间 / 频率 / 初始资金等由用户在云回测平台界面自行配置；
3. 在客户端点「运行回测」查看绩效；
4. 附上本次生成的完整源码，供核对；
5. 如需修改策略，直接描述新意图，本 Skill 重新生成并再次 `send_source` 注入即可（旧代码会被覆盖）。
