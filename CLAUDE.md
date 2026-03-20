# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
# Web 控制台（主入口，http://127.0.0.1:8787）
python console.py

# 一键启动 DDE 完整链路（DataBus + Monitor + Web）
python console.py --start-dde-pipeline

# 数据总线
python -m data_bus.bus --source dde
python -m data_bus.bus --source dde --no-persist   # 仅广播不落盘

# 实时 Monitor（终端 UI）
python -m monitors.monitor
python -m monitors.monitor --min-profit 100 --expiry-days 30 --n-each-side 10

# 数据抓取
python -m data_engine.bond_termstructure_fetcher --kind all
python -m data_engine.bond_termstructure_fetcher --kind cgb

# 回测
python -m backtest.run
```

## 架构：四层流水线

```
【第 1 层】数据采集层
  数据源（DDE）
       ↓
【第 2 层】数据总线层
  data_bus/bus.py          — ZMQ PUB（tcp://127.0.0.1:5555）+ 可选 Parquet 落盘
       ↓
【第 3 层】消费层（ZMQ SUB）
  monitors/monitor.py      — Rich 终端 UI 实时刷新（PCP 套利信号）
  web/market_cache.py      — CONFLATE=1 → LKV 快照 → compute 线程向量化 IV
       ↓
【第 4 层】展示层
  web/dashboard.py         — FastAPI 控制台 + WebSocket /ws/vol_smile 推送
```

**DataBus（`data_bus/bus.py`）**：消费来自 `DDEDirectSubscriber` 的 tick，写入 Parquet 分片（`D:\MARKET_DATA\chunks\`），同时通过 `ZMQPublisher` 广播 `OPT_` / `ETF_` 前缀消息。每 30 秒刷盘，15:10 自动触发日终合并为 `options_YYYYMMDD.parquet` / `etf_YYYYMMDD.parquet`，并维护 `snapshot_latest.parquet` 供 Monitor 冷启动恢复。

**Monitor（`monitors/monitor.py`）**：订阅 ZMQ，通过 `TickAligner` 维护 LKV 状态，调用 `PCPArbitrageStrategy.scan_pairs_for_display(snapshot, pairs)` 计算信号，用 `rich.Live` 渲染终端表格。共享逻辑（合约加载、快照恢复、消息解析）在 `monitors/common.py`。

---

## 策略架构：Alpha / Execution 分层

### ⚠️ 核心架构原则（修改前必读）

任何对此代码库的修改、重构或功能添加，**必须**遵循以下原则：

1. **策略绝对无状态化**：`strategies/` 下的所有策略类必须是纯函数式"数学大脑"，**绝对禁止**在策略内部维护字典、队列或存储历史 Tick 状态。
2. **读写解耦**：策略只负责发现机会并输出 `ArbitrageSignal`。策略**绝对不感知**真实资金、持仓或撮合逻辑——将 Signal 转化为 Order 并执行，是执行层（`Broker`/`BacktestEngine`）的专属职责。
3. **单一数据真相**：实盘和回测必须且只能通过 `MarketSnapshot`（由 `TickAligner` 生成）这一唯一载体与策略交互。**【红线约束】策略的 `generate_signals` 接口绝对禁止接收单个的 `OptionTickData` 或 `ETFTickData`。**

### 架构全景图

```
       【管线 A：实时实盘流】                      【管线 B：历史回测流】
    (Live Market Data: DDE/ZMQ)             (Historical Data: Parquet)
                 │                                        │
                 ▼                                        ▼
      [ monitors/monitor.py ]               [ backtest/data_feed.py ]
      (ZMQ Subscriber)                      (HistoricalFeed/TickLoader)
                 │                                        │
                 └───────────────┬────────────────────────┘
                                 │ (OptionTickData / ETFTickData)
                                 ▼
              =========================================
              ||     [ data_engine/tick_aligner.py ] || 状态机 (Stateful)
              ||             TickAligner             || 拼装最新已知值 (LKV)
              =========================================
                                 │
                                 ▼ (MarketSnapshot)  <-- 统一接口，隔绝实盘与回测的差异
              =========================================
              ||         [ strategies/base.py ]      ||
              ||         PCPArbitrageStrategy        || 纯数学大脑 (Stateless)
              =========================================
                                 │
                                 ▼ (ArbitrageSignal) <-- 纯粹的机会观察，无执行意图
              ┌──────────────────┴──────────────────┐
              │                                     │
              ▼                                     ▼
【管线 A 终点：视觉展示】                  【管线 B 终点：执行与账务】
[ monitors/monitor.py ]                [ backtest/engine.py & broker.py ]
[ web/market_cache.py ]                - 哨兵拦截 (999999.0 拒单)
- Rich UI 终端渲染                     - 买卖价差撮合 (跨价撮合)
- WebSocket 推送                       - 容量限制裁剪 (Volume Constraint)
(等待人类手动下单)                                   │
                                                    ▼ (TradeRecord)
                                       [ backtest/portfolio.py ]
                                       - 资金预检 (Cash Check)
                                       - 调用 risk/margin.py 算保证金
                                       - 记录盈亏与资金曲线
```

### 目录职责与边界规范

在未来扩展系统时，请将新代码严格放入对应的功能区域：

**`models/`（数据与契约层）**
- 职责：定义跨模块流通的数据结构（纯 `dataclass`），绝对不写任何业务逻辑、计算方法或状态维护代码
- `data.py`：市场数据载体（`OptionTickData`, `ETFTickData`, `MarketSnapshot`）
- `order.py`：交易相关载体（`ArbitrageSignal`, `Order`, `TradeRecord`, `AccountState`）

**`data_engine/`（数据流转与状态对齐层）**
- 职责：处理脏数据、对齐时间戳、维护市场截面 LKV；将零散 Tick 拼装为 `MarketSnapshot` 的所有脏活必须在此完成，绝不许泄露给策略层
- 核心模块：`TickAligner`

**`strategies/`（Alpha 逻辑层）**
- 职责：寻找定价偏差，生成套利信号；继承 `BaseStrategy`，必须实现 `generate_signals(self, snapshot: MarketSnapshot) -> List[ArbitrageSignal]`
- 规则：只放数学公式和逻辑判断，**绝对禁止**在策略内部维护任何状态；`generate_signals` 是唯一的对外信号入口，内部逻辑（开仓/平仓分支等）通过私有方法实现

**`backtest/`（执行引擎与会计层）**
- `Broker`（`backtest/broker.py`）：`execute_signal` 入口按 `signal.action` 分派：OPEN 走四条微观机制校验（哨兵拦截 / 跨价撮合 / 容量限制 / 保证金前置校验）；CLOSE 走 `_execute_close`（哨兵拦截 / **盘口容量限制** / 滑点方向反转 / 无保证金校验，`margin_reserved=0`），均为 FOK 语义。CLOSE 的容量上限来自 `signal.max_qty`，由策略层在生成信号时填入，**禁止在 Broker 内部读取盘口数据**。
- `Engine`（`backtest/engine.py`）：主循环按 `signal.action` 区分 OPEN/CLOSE 路径；CLOSE 路径调 `_get_closeable_sets(signal, current_date)` 从 Portfolio 持仓计算可平组数（含 ETF T+1 冻结检查），绕过 `_calc_max_sets`。
- `Portfolio`（`backtest/portfolio.py`）：纯会计层，管钱（更新 cash）、管持仓、冻结保证金；**`process_trades` 处理买入 Call 时，必须在 `_update_position` 之前按比例释放 `margin_occupied`**（否则平仓后保证金无法归零）；**禁止直接看盘口撮合**。

**`monitors/` & `web/`（表现层）**
- 职责：通过 ZMQ 获取数据，驱动 `TickAligner` 和 Strategy，将 Signal 渲染给用户
- 规则：只读，**绝对禁止**在表现层直接修改行情数据或策略参数

**`calculators/` & `risk/`（工具库）**
- 职责：提供独立的无状态金融计算（Black-76 定价、Greeks、波动率微笑插值、上交所保证金公式）
- 规则：作为纯函数被策略或 `Portfolio` 调用，自身不保存任何业务流程状态

### 关键类型边界

| 类型 | 所在模块 | 职责 |
|------|----------|------|
| `OptionTickData` / `ETFTickData` | `models/data.py` | 原始 tick，含盘口五档 |
| `MarketSnapshot` | `models/data.py` | 某时刻所有合约的 LKV 截面，由 `TickAligner` 生成 |
| `BaseSignal` | `models/order.py` | 所有交易信号的抽象基类（含 `ts`, `action: SignalAction`, `direction: int`） |
| `ArbitrageSignal` | `models/order.py` | 继承自 `BaseSignal`，多腿套利策略的专用输出，含净利润/盘口价格/辅助指标 |
| `SignalAction` | `models/order.py` | 枚举：`OPEN` / `CLOSE`，区分开平仓意图 |
| `TickAligner` | `data_engine/tick_aligner.py` | 有状态 LKV 容器，实盘/回测共用 |
| `PCPArbitrageStrategy` | `strategies/pcp_arbitrage.py` | 无状态策略；`generate_signals(snapshot)` 为统一信号入口（内部合并 OPEN + CLOSE），`scan_pairs_for_display(snapshot, pairs)` 为展示专用公开接口 |

**Web 控制台（`web/dashboard.py`）**：FastAPI + 单页 HTML（`web/templates/index.html`），通过 `spawn_module()` 以子进程方式启停 DataBus 和 Monitor，前端轮询 `/api/status`。WebSocket `/ws/vol_smile` 由 `_ws_broadcaster` 协程从 `asyncio.Queue` 读取计算结果并推送。

**market_cache 计算线程**：
- `market-cache-zmq`（Thread-1）：ZMQ SUB（CONFLATE=1）→ `_lkv`
- `market-cache-compute`（Thread-2）：每 100ms 读 `_lkv`，调用 `VectorizedIVCalculator` Brent 法求全品种 IV，通过 `loop.call_soon_threadsafe(_try_put, queue, result)` 安全推送至 asyncio Queue
- `_try_put` 吞掉 `QueueFull`（队列满时丢弃，不打印异常）

## Vol Smile 计算层

| 文件 | 说明 |
|------|------|
| `calculators/vectorized_pricer.py` | `VectorizedIVCalculator`：Black-76 Brent 法 IV 求解（GUARD-1/2） |
| `calculators/iv_calculator.py` | `calc_implied_forward()`、`calc_iv_black76()`（标量版，HTTP 端点兼容） |
| `web/market_cache.py` | ZMQ SUB + compute 线程 + `get_rich_snapshot()` |
| `web/templates/vol_smile.html` | WS 客户端 + rAF 增量渲染 + IV 表格 + 阈值告警 |

**三条 GUARD 机制**：
- `[GUARD-1]` 无套利边界布尔掩码（`vectorized_pricer.py`）：price≤0 / not finite / K≤0 / price<intrinsic-1e-4 → 直接 `nan`，跳过 brentq
- `[GUARD-2]` T 精度（`vectorized_pricer.py`）：`time.time()` Unix 时间戳，`calc_T()` 返回 `max(T, 1e-6)`
- `[GUARD-3]` 微观流动性防线（`market_cache.py`，GUARD-1 上游）：mid < 10 Tick（0.001 元）或价差 > max(20 Tick, mid×30%) → mid/bid/ask 三路同步置 `nan`，让 GUARD-1 跳过

## DDE 链路（核心模块，修改前必读）

DDE（Dynamic Data Exchange）从本地交易软件实时拉取行情，基于古老的 Windows 消息机制。**修改前必须完整阅读 `data_bus/dde_direct_client.py`**，该文件包含所有 DDE 逻辑。

### 实现架构

- `_DDEClient`：底层 ctypes DDEML（`DdeInitializeW` + `XTYP_ADVSTART`，纯 ADVISE 模式）
- `DDEDirectSubscriber(DataProvider)`：上层接口，供 `bus.py` 调用
- DDE 所有操作（`DdeInitializeW`、`DdeConnect`、`XTYP_ADVSTART`）在专用 pump 线程中串行执行

### 文件结构

| 文件 | 说明 |
|------|------|
| `metadata/wind_sse_optionchain.xlsx` | Wind 导出的全 SSE 期权合约信息 |
| `metadata/wxy_options.xlsx` | 交易软件导出的 DDE 数据表（**DDE 寻址的唯一来源**） |

### wxy_options.xlsx 是 DDE 寻址的唯一来源

xlsx 是 ZIP，解析 `xl/externalLinks/externalLink*.xml` 获得全部地址信息：
- **service**：`ddeService` 属性，实际值为 `"QD"`（不是 `"TdxW"`，用错则全部 `DdeConnect` 失败）
- **topic**：`ddeTopic` 属性，每个合约对应一个不透明数字字符串（如 `"2206355670"`），不可推算
- **item 名称**：英文字段 `LASTPRICE`、`BIDPRICE1`、`ASKPRICE1`、`BIDVOLUME1`、`ASKVOLUME1`

`_load_topic_map()` 在 DataBus 启动时读取一次，返回 `(code→topic dict, service_name)`。**禁止用 `_code_to_topic()` 推算 topic**（推算结果对 QD 服务无效）。

### XlTable 二进制响应解析

DDE ADVISE 回调中 `DdeGetData` 返回 XlTable 二进制流（不是字符串）。格式为连续的 `(type:u16, size:u16, data[size])` 记录：

```
偏移 0: type=0x0010 (TABLE), size=4  → 容器头，跳过其 4 字节数据体
偏移 8: type=0x0001 (FLOAT),  size=8 → IEEE 754 double，即报价
```

正确解析：从 `off=0` 开始流式处理，`off += rsize` 跳过记录体，遇到 `type==0x0001 and size==8` 时用 `struct.unpack_from("<d", raw, off)` 读取浮点值。**若从 `off=4` 开始则跳过了 FLOAT 记录，永远取不到数据。**

### 禁止事项

- **禁止用 `pywin32 dde` 模块替换**：`ConnectTo()` 对 QD 服务连接必定失败，只有 ctypes DDEML 可用
- **禁止将 DDE 操作改为异步或多线程并发**：`DdeConnect` 依赖 Windows 消息泵，必须在单一线程内串行调用并在每次 connect 后立即 `_pump_messages()`
- **禁止从代码推算 service/topic**：所有地址信息来自 xlsx，软件升级后地址可能改变

### DDE 测试流程

1. 确认 `metadata/wxy_options.xlsx` 已放入 `metadata/`
2. 启动 DataBus：`python -m data_bus.bus --source dde`
3. 30 秒后看自检日志：`DDE 自检(30s): 累计=N tick, 期权标的=[...]`

## 核心数据流

1. **合约信息**：`data_engine/contract_catalog.py` 的 `ContractInfoManager` 从 `metadata/` 加载 optionchain 文件（优先当日 CSV，无则回退 `*optionchain*.xlsx`）。支持 xlsx（Wind 导出格式，按列位置解析）。
2. **Tick 数据**：`OptionTickData` / `ETFTickData` dataclass（`models/data.py`），代码一律用 `.SH` 后缀（`normalize_code()` 负责标准化）。Tick 经 `TickAligner.update_tick()` 写入 LKV，`snapshot()` 返回 `MarketSnapshot`。
3. **套利计算**：`strategies/pcp_arbitrage.py` 的 `PCPArbitrageStrategy`（无状态）接收 `MarketSnapshot`；`generate_signals(snapshot)` 为统一信号入口，内部依次调用私有方法 `_scan_opportunities` / `_scan_close_opportunities`，合并 OPEN + CLOSE 信号后返回，Engine 按 `signal.action` 自动分派；`scan_pairs_for_display(snapshot, pairs)` 为展示专用公开接口，供监控无阈值全量展示；`_calc_forward_metrics()` / `_calc_close_metrics()` 为模块级纯函数，分别计算开仓/平仓净利润。
4. **VIX**：`calculators/vix_engine.py`，利率曲线来自 `calculators/yield_curve.BoundedCubicSplineRate`，读取 `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`，文件不存在时回退固定利率 2%。

## Web API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | 主控台状态（进程、metadata 文件 mtime） |
| `/api/processes/recorder/start` | POST `{"source":"dde"}` | 启动 DataBus |
| `/api/processes/recorder/stop` | POST | 停止 DataBus |
| `/api/processes/monitor/start` | POST | 启动 Monitor |
| `/api/dde/state` | GET | DDE 面板状态（running、route_count、4个文件 mtime） |
| `/api/dde/start` | POST `{"interval":3}` | 启动 DDE 直连模式 |
| `/api/dde/stop` | POST | 停止 DDE |
| `/api/dde/poll` | GET | 拉取 DDE 最新行情快照 |
| `/ws/vol_smile` | WebSocket | 向量化 IV 计算结果实时推送（每 100ms 微批次） |
| `/api/vol_smile/expiries` | GET | 指定品种的可用到期日列表 |
| `/api/vol_smile` | GET | 指定品种+到期日的 IV 数据（HTTP 兜底，非实时） |

`/api/state` 的 `metadata_files` 字段包含 2 个键：`wind_sse_optionchain`（合约元数据）、`wxy_options`（DDE 路由表），各含 `mtime_ago` 字符串。

## Monitor 显示规则

### 净利润三档配色（以 `--min-profit` 为阈值，默认 30 元）

| 净利润 | 方向列 | 净利润列 |
|--------|--------|---------|
| ≥ min_profit | **正向**（绿色粗体） | **绿色粗体** |
| ≥ 0 且 < min_profit | 正向（白色常规） | 白色常规 |
| < 0 | 不显示 | 灰色（dim） |

### 表格布局

- box 样式：`SIMPLE`（轻量分隔线）
- Panel padding：`(0, 0)`（无左右内边距）
- 按 `(到期日, 乘数)` 分组，每组前置 Rule 横幅标题（含自然日/交易日/乘数）
- 全局列名顶置，组内数据表 `show_header=False`

## 回测平仓闭环（Phase 6）

### BaseSignal.action 字段语义

`action: SignalAction = SignalAction.OPEN`（或 `CLOSE`）区分开仓与平仓意图，对于套利信号，**价格字段在 CLOSE 信号中含义翻转**：

| 字段 | OPEN 语义 | CLOSE 语义 |
|------|-----------|-----------|
| `spot_ask` | ETF 卖一价（买入 ETF） | ETF 买一价（卖出 ETF） |
| `put_ask` | Put 卖一价（买入 Put） | Put 买一价（卖出 Put） |
| `call_bid` | Call 买一价（卖出 Call）| Call 卖一价（买入 Call）|

Broker 基于 `signal.action` 自动选择滑点方向，**调用方无需关心字段复用细节**。

### 平仓信号生产（策略层）

`PCPArbitrageStrategy._scan_close_opportunities(snapshot, pairs)`（私有）扫描全量配对，计算平仓净利润；由 `generate_signals` 内部调用，外部不应直接使用：
```
close_per_share = S_bid + P_bid - C_ask - K
close_net = close_per_share × mult − S_bid × mult × etf_fee_rate − option_rt_fee
```
策略**不感知持仓**，只要 `close_net ≥ close_profit_threshold` 就输出 CLOSE 信号，由 Engine 过滤。

构造 `PCPArbitrageStrategy` 时可传入 `close_profit_threshold`（默认 0.0）控制平仓触发门槛。

**CLOSE 信号的 `max_qty` 字段（盘口流动性上限）**：策略在 `_evaluate_pair_for_close` 中从 tick 对象提取一档量，调用 `_calc_close_metrics` 计算并填入 `signal.max_qty`，公式与开仓对称：
```
平仓方向：卖 ETF/Put（看买方深度），买 Call（看卖方深度）
s_contracts = floor(etf_bid_volume × 100 / mult)   # ETF 手→组
max_qty     = min(call_ask_vol[0], put_bid_vol[0], s_contracts)
```
若任一档量为 0，`max_qty=None`（不裁剪）。**新增类似 Close 策略时必须同样填充 `max_qty`，否则 Broker 无法做容量校验。**

### 平仓可执行性校验（引擎层）

`BacktestEngine._get_closeable_sets(signal, current_date)` 规则：
1. 三腿持仓齐备（Call 空头 + Put 多头 + ETF 多头），缺任一返回 0
2. ETF T+1 冻结：`_etf_buy_dates[underlying] == current_date` 时返回 0
3. 可平组数 = `min(|Call空头|, Put多头, ETF多头 // unit)`

### 保证金释放时序（⚠️ 必须先释放后更新持仓）

`Portfolio.process_trades` 对每笔 Call 买入（direction=+1）做保证金释放：
```python
# 必须在 _update_position 之前执行（此时 pos.quantity 仍是平仓前的负值）
release_ratio = close_qty / abs(pos.quantity)
released = pos.margin_occupied * release_ratio
pos.margin_occupied -= released
self.total_margin   -= released
```
**若调换顺序**（先更新持仓再释放），持仓已归零，比例计算失效，保证金永远无法归零。

### run.py 回调模板

```python
# set_pairs 在回测初始化时调用一次（pairs 为静态配置）
pcp_strategy.set_pairs(all_pairs)

# strategy_callback 内只需单行：
return pcp_strategy.generate_signals(snapshot)  # 内部合并 OPEN + CLOSE，Engine 自动分派
```

---

## 关键约定

- **数据目录固定**：`D:\MARKET_DATA`（`config/settings.py` 的 `DEFAULT_MARKET_DATA_DIR`）
- **合约代码后缀**：统一 `.SH`（内部），`.XSHG` 仅出现在外部 xlsx（optionchain），进入系统前通过 `normalize_code()` 转换
- **乘数**：标准合约 10000，分红调整型合约（`is_adjusted=True`）乘数可能为 10265 等，来自 optionchain 文件
- **费用参数**（Monitor 实时公式）：`TradingConfig.etf_fee_rate`（默认万 2）和 `option_round_trip_fee`（默认 3.0 元/组）；回测引擎使用更细分的 `FeeConfig`
- **Windows 编码**：所有入口点首行调用 `monitors.common.fix_windows_encoding()`，必须在 `rich` 之前执行
- **ZMQ 端口**：DataBus PUB 默认 5555，Monitor SUB 连接相同端口；端口占用报 errno 10048，用 psutil 查找并终止旧进程

## 品种配置

监控品种在 `config/settings.py` 的 `UNDERLYINGS` 列表：
- `510050.SH`（50ETF）
- `510300.SH`（300ETF）
- `510500.SH`（500ETF）

新增品种需同时更新 `UNDERLYINGS`、`ETF_CODE_TO_NAME`，以及 optionchain 文件。

## 开发注意事项

- **命名一致性**：同类概念的类、变量、函数命名风格必须统一。例如期权 tick 数据统一用 `OptionTickData`，ETF tick 数据统一用 `ETFTickData`；新增同类概念时须与已有命名对齐，不得自创风格（如 `OptTick`、`option_data` 等）。
- **后台服务代码禁裸 `print()`**：`data_bus/`、`data_engine/`、`strategies/` 等非 UI 模块禁用裸 `print()`，统一走 `logging.getLogger(__name__)`。Rich 终端 UI（`monitors/`）不受此约束。
- **`.py` 改动需重启 `console.py`** 才能生效（uvicorn 在进程启动时加载模块）
- **`.html` 改动无需重启**（每次请求重新读取文件）
- **curl 走代理问题**：环境变量 `http_proxy=http://127.0.0.1:7897`，curl 调本地 API 会 502。改用 Python `urllib` 并设 `ProxyHandler({})` 绕过代理
- **index.html 与 dde.html 相互独立**：两个页面各自维护状态显示逻辑，修改 API 响应字段时两处都要同步更新
- **WS 推送线程安全**：`market-cache-compute` 线程向 asyncio Queue 写数据必须通过 `loop.call_soon_threadsafe(_try_put, queue, result)`，禁止直接调用 `queue.put_nowait()`；`_ws_broadcaster` 中修改 `_ws_clients` 集合用 `.difference_update()` 原地操作，禁止 `-=` 赋值（会触发 `UnboundLocalError`）
- **策略异常预警只对正数触发**：`strategies/pcp_arbitrage.py` 中净利润异常检测必须用 `if profit > 2000`，**严禁用 `abs(profit) > 2000`**。非交易时段盘口价差拉大，负利润可达 -4000 以上，这是正常的"无机会"状态，不是计算错误。只有异常的大正利润才代表公式或数据出现问题。
- **PnL 统计模块（多态防腐层）**：`analysis/pnl.py` 已实现信号类型分派架构，规范如下：
  - `SignalPnLResult` 是所有信号结算结果的统一 DTO；`analyze` / `calc_greeks_attribution` 签名一律接收 `List[BaseSignal]`，**禁止收窄回 `List[ArbitrageSignal]`**。
  - 新增信号类型时，**必须**在 `_dispatch_signal_pnls` 中添加对应的 `isinstance` 分支，并实现 `_process_<类型>` 方法；**禁止**在 `_dispatch_signal_pnls` 外部直接访问任何信号专属字段。
  - `_process_arbitrage`（及未来的专职处理器）的资金流公式为 `cash_flow = Σ price × qty × multiplier × (BUY?1:-1)`，**期权腿必须绝对乘以 `multiplier`（通常为 10000）**，最终净利润公式严格定死为 `net_pnl = -(cash_flow + commission + slippage_cost)`。历史上曾因漏乘导致期权腿盈亏严重失真，此代数结构已作为系统红线，**绝对不得回退或篡改**。
  - 信号无成交记录时，`net_pnl` **必须返回 0.0**，**严禁**回退到 `signal.net_profit`（会产生未成交的"利润幻觉"）。
  - 未来引入 `DirectionalSignal` 时，在 `_process_directional` 中实现，当前为 `NotImplementedError` 占位，**不得删除骨架**。

## 🛠️ 新策略开发 SOP (Standard Operating Procedure)

当被要求开发新策略时，请严格遵循以下模板在 `strategies/` 目录下创建新文件：
1. **继承基类**：必须 `from strategies.base import BaseStrategy`。
2. **拒绝状态**：类的 `__init__` 只能接收静态超参数，严禁初始化任何状态字典（如 `self.cache = {}`）。
3. **计算入口**：核心计算必须全部在 `def generate_signals(self, snapshot: MarketSnapshot) -> List[BaseSignal]` 中完成（具体实现时可注解为具体的子类如 `List[ArbitrageSignal]`）；内部可拆分私有方法（如 `_scan_opportunities` / `_scan_close_opportunities`），但对外只暴露 `generate_signals`。
4. **输出限制**：返回的必须是 `ArbitrageSignal`（或其同级 Signal 类），绝不能直接输出执行层的 `Order`。
5. **信号输出规范**：策略生成的任何信号必须是继承自 `BaseSignal` 的子类（如 `ArbitrageSignal` 或自定义的 `DirectionalSignal`）。严禁策略直接输出 `Order` 或绕过基类自创信号结构。
6. **`max_qty` 填充义务**：策略在构造任何信号时，**必须**根据对应方向的盘口量计算并填入 `max_qty`（Broker 容量限制的唯一数据来源）。开仓信号：`min(call_bid_vol, put_ask_vol, etf_ask_contracts)`；平仓信号：`min(call_ask_vol, put_bid_vol, etf_bid_contracts)`。若盘口量不可用则置 `None`（Broker 跳过裁剪），但不得省略计算逻辑。
7. **新增单边/做市策略的预留位置**：`models/order.py` 已定义 `DirectionalSignal(BaseSignal)` 骨架（含 `contract_code`, `target_price`）。未来引入单边投机或做市策略时，需联动修改以下四层：
   - **信号层**：在 `DirectionalSignal` 上扩展特有字段（如方向、目标价位）。
   - **执行层**：在 `backtest/broker.py` 中新增 `execute_directional_signal` 方法，**严禁复用专属三腿套利的 `execute_signal`**。
   - **调度层**：在 `backtest/engine.py` 中增加对新 Signal 类型的路由分派（基于 `isinstance` 判定，分发至对应的 Broker 方法）。
   - **账务层**：在 `backtest/portfolio.py` 的 `process_trades` 中，补充单边策略特有的保证金计算与释放逻辑（单边期权的保证金规则与 PCP 套利完全不同）。

## 📝 README.md 维护规范 (面向人类阅读)

`README.md` 的唯一受众是**人类所有者**，其核心目的是"提供全局视野和快速操作指南"，而不是"沉淀底层技术细节"。在更新 `README.md` 时，必须严格遵守以下原则：

1. **极简主义与高信噪比**：
   - **保留**：系统全局拓扑图（ASCII 架构图）、核心层级说明、日常启动流程（SOP）、关键 CLI 命令清单。
   - **禁止**：严禁在 README 中堆砌代码级别的实现细节（如具体的 Parquet Schema 表格、长篇的底层 API 调用逻辑、复杂的数学公式推导）。
2. **细节下沉到 docs/**：
   - 凡是超过 200 字的技术原理解析（如 DDE 的 ADVISE 模式原理、Black-76 Brent 法求 IV 的推导等），**必须**移出 README，写入 `docs/` 目录下的专属 Markdown 文件中（如 `docs/dde_tech_spec.md`, `docs/vol_smile_math.md`），并在 README 中仅留一行超链接跳转。
3. **变更日志（Changelog）的克制**：
   - `README.md` 中的"最近变更"板块**最多只保留最近的 3 到 5 条核心架构/功能级更新**。
   - 每次 commit 前同步改动时，必须是"高度概括的一句话总结"，绝不能把长篇大论的代码重构细节堆砌进去。历史陈旧的变更记录应定期清理或转移至独立的 `CHANGELOG.md`。
4. **目录结构说明要"薄"**：
   - 描述模块命名时，一句话说清模块职责即可，不要把类名、内部函数名全抄上去。

## 协作偏好（Claude 参考）

- 用户倾向于**自己执行命令**，不喜欢 Claude 直接运行脚本（尤其涉及进程启停）
- **git commit/push 需用户明确要求**才执行
- **每次 commit 前**，先将本次改动的重点精简同步到 `README.md`（功能变更、API 变化、架构调整等），README 更新与代码改动同属同一个 commit，README 内容只写关键变化，不堆砌细节
- **执行代码修改前**，先给出简要修改计划（涉及哪些文件、改什么），确认用户同意后再动手
- 沟通风格：中文，简洁直接，抓住重点，精简解释
- 涉及破坏性或影响共享状态的操作（push、merge、删除文件等），先确认再执行
