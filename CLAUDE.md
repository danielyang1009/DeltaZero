# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
# Web 控制台（主入口，http://127.0.0.1:8787）
python console.py

# 一键启动 DDE 完整链路（DataBus + Monitor + Web）
python console.py --start-dde-pipeline

# 数据总线
python -m data_bus.bus --source wind
python -m data_bus.bus --source dde
python -m data_bus.bus --source dde --no-persist   # 仅广播不落盘

# 实时 Monitor（终端 UI）
python -m monitors.monitor
python -m monitors.monitor --min-profit 100 --expiry-days 30 --n-each-side 10

# 数据抓取
python -m data_engine.optionchain_fetcher
python -m data_engine.bond_termstructure_fetcher --kind all
python -m data_engine.bond_termstructure_fetcher --kind cgb

# 回测
python -m backtest.run
```

## 架构：四层流水线

```
数据源（Wind API / DDE）
       ↓
data_bus/bus.py          — ZMQ PUB（tcp://127.0.0.1:5555）+ 可选 Parquet 落盘
       ↓
monitors/monitor.py      — ZMQ SUB，Rich 终端 UI 实时刷新
web/market_cache.py      — ZMQ SUB（CONFLATE=1）→ LKV 快照 → compute 线程向量化计算
web/dashboard.py         — FastAPI 控制台 + WebSocket /ws/vol_smile 推送
```

**DataBus（`data_bus/bus.py`）**：消费来自 `WindSubscriber` 或 `DDESubscriber` 的 tick，写入 Parquet 分片（`D:\MARKET_DATA\chunks\`），同时通过 `ZMQPublisher` 广播 `OPT_` / `ETF_` 前缀消息。每 30 秒刷盘，15:10 自动触发日终合并为 `options_YYYYMMDD.parquet` / `etf_YYYYMMDD.parquet`，并维护 `snapshot_latest.parquet` 供 Monitor 冷启动恢复。

**Monitor（`monitors/monitor.py`）**：订阅 ZMQ，调用 `PCPArbitrage.scan_pairs_for_display()` 计算信号，用 `rich.Live` 渲染终端表格。共享逻辑（合约加载、快照恢复、消息解析）在 `monitors/common.py`。

**Web 控制台（`web/dashboard.py`）**：FastAPI + 单页 HTML（`web/templates/index.html`），通过 `spawn_module()` 以子进程方式启停 DataBus 和 Monitor，前端轮询 `/api/status`。WebSocket `/ws/vol_smile` 由 `_ws_broadcaster` 协程从 `asyncio.Queue` 读取计算结果并推送。

**market_cache 计算线程**：
- `market-cache-zmq`（Thread-1）：ZMQ SUB（CONFLATE=1）→ `_lkv`
- `market-cache-compute`（Thread-2）：每 100ms 读 `_lkv`，调用 `VectorizedIVCalculator` 向量化 NR 求全品种 IV，通过 `loop.call_soon_threadsafe(_try_put, queue, result)` 安全推送至 asyncio Queue
- `_try_put` 吞掉 `QueueFull`（队列满时丢弃，不打印异常）

## Vol Smile 计算层

| 文件 | 说明 |
|------|------|
| `calculators/vectorized_pricer.py` | `VectorizedIVCalculator`：100% 向量化 Black-76 NR（GUARD-1/2/3） |
| `calculators/iv_calculator.py` | `calc_implied_forward()`、`calc_iv_black76()`（标量版，HTTP 端点兼容） |
| `web/market_cache.py` | ZMQ SUB + compute 线程 + `get_rich_snapshot()` |
| `web/templates/vol_smile.html` | WS 客户端 + rAF 增量渲染 + IV 表格 + 阈值告警 |

**三条 GUARD 机制**（`VectorizedIVCalculator`）：
- `[GUARD-1]` 无套利边界布尔掩码：价格低于下界直接输出 `nan`，不进 NR
- `[GUARD-2]` Vega 坍缩保护：`np.maximum(vega, 1e-8)` + `np.clip(step, -0.5, 0.5)`
- `[GUARD-3]` T 精度：`time.time()` Unix 时间戳，`calc_T()` 返回 `max(T, 1e-6)`

## DDE 链路

DDE（Dynamic Data Exchange）从交易软件（通达信等）实时拉取行情。

**两种 DDE 实现**：
- `data_bus/dde_subscriber.py`：旧版，依赖 `DDERouteParser` 解析 `wxy_*.xlsx`，通过旧 ctypes DDEML
- `data_bus/dde_direct_client.py`：新版，`TongdaxinDDEClient`（pywin32 ADVISE 模式，无需 Excel 运行），topic 地址仍从 `wxy_*.xlsx` 解析后通过 `topic_map` 参数传入，未提供时 fallback 到 `_code_to_topic()` 推算

### 文件结构

| 文件 | 说明 |
|------|------|
| `metadata/wind_sse_optionchain.xlsx` | Wind 导出的全 SSE 期权合约信息（原名 wind_50etf_optionchain，已改） |
| `metadata/wxy_50etf.xlsx` | 交易软件导出的 50ETF 期权 DDE 数据表 |
| `metadata/wxy_300etf.xlsx` | 交易软件导出的 300ETF 期权 DDE 数据表 |
| `metadata/wxy_500etf.xlsx` | 交易软件导出的 500ETF 期权 DDE 数据表（暂未配置时显示"未找到"） |

### DDESubscriber 关键参数

- `staleness_timeout = 90.0`（秒）：期权合约超过 90s 无变化标记 STALE。ADVISE 模式下 DDE 服务端仅在值变化时推送，深度 OTM 合约可能数分钟无更新，30s 阈值过紧。
- glob 路径：`metadata/wind_sse_optionchain.xlsx`（精确匹配，不用通配符）

### DDE 测试流程

1. 确认 wxy_*.xlsx 文件已放入 `metadata/`
2. 离线解析测试：`python -c "from data_bus.dde_subscriber import DDERouteParser; ..."`
3. 启动 DataBus：`POST /api/processes/recorder/start {"source":"dde"}`
4. 查看 `/api/state` 确认 `recorder_running: true` 及快照合约数

## 核心数据流

1. **合约信息**：`data_engine/contract_catalog.py` 的 `ContractInfoManager` 从 `metadata/` 加载 optionchain 文件（优先当日 CSV，无则回退 `*optionchain*.xlsx`）。支持 xlsx（Wind 导出格式，按列位置解析）。
2. **Tick 数据**：统一 `TickData` / `ETFTickData` dataclass（`models.py`），代码一律用 `.SH` 后缀（`normalize_code()` 负责标准化）。
3. **套利计算**：`strategies/pcp_arbitrage.py` 的 `PCPArbitrage` 维护 `TickAligner`（LKV 快照），`_compute_forward_metrics()` 计算净利润及辅助指标（Max_Qty、SPRD、OBI、Net_1T、TOL）。
4. **VIX**：`calculators/vix_engine.py`，利率曲线来自 `calculators/yield_curve.BoundedCubicSplineRate`，读取 `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`，文件不存在时回退固定利率 2%。

## Web API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | 主控台状态（进程、metadata 文件 mtime） |
| `/api/processes/recorder/start` | POST `{"source":"dde"\|"wind"}` | 启动 DataBus |
| `/api/processes/recorder/stop` | POST | 停止 DataBus |
| `/api/processes/monitor/start` | POST | 启动 Monitor |
| `/api/dde/state` | GET | DDE 面板状态（running、route_count、4个文件 mtime） |
| `/api/dde/start` | POST `{"interval":3}` | 启动 DDE 直连模式 |
| `/api/dde/stop` | POST | 停止 DDE |
| `/api/dde/poll` | GET | 拉取 DDE 最新行情快照 |
| `/ws/vol_smile` | WebSocket | 向量化 IV 计算结果实时推送（每 100ms 微批次） |
| `/api/vol_smile/expiries` | GET | 指定品种的可用到期日列表 |
| `/api/vol_smile` | GET | 指定品种+到期日的 IV 数据（HTTP 兜底，非实时） |

`/api/state` 的 `metadata_files` 字段包含 4 个键：`wind_sse_optionchain`、`wxy_50etf`、`wxy_300etf`、`wxy_500etf`，各含 `mtime_ago` 字符串。

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

## 关键约定

- **数据目录固定**：`D:\MARKET_DATA`（`config/settings.py` 的 `DEFAULT_MARKET_DATA_DIR`）
- **合约代码后缀**：统一 `.SH`（内部），`.XSHG` 仅出现在 Wind 原始数据，进入系统前通过 `normalize_code()` 转换
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

- **后台服务代码禁裸 `print()`**：`data_bus/`、`data_engine/`、`strategies/` 等非 UI 模块禁用裸 `print()`，统一走 `logging.getLogger(__name__)`。Rich 终端 UI（`monitors/`）不受此约束。
- **`.py` 改动需重启 `console.py`** 才能生效（uvicorn 在进程启动时加载模块）
- **`.html` 改动无需重启**（每次请求重新读取文件）
- **curl 走代理问题**：环境变量 `http_proxy=http://127.0.0.1:7897`，curl 调本地 API 会 502。改用 Python `urllib` 并设 `ProxyHandler({})` 绕过代理
- **index.html 与 dde.html 相互独立**：两个页面各自维护状态显示逻辑，修改 API 响应字段时两处都要同步更新
- **WS 推送线程安全**：`market-cache-compute` 线程向 asyncio Queue 写数据必须通过 `loop.call_soon_threadsafe(_try_put, queue, result)`，禁止直接调用 `queue.put_nowait()`；`_ws_broadcaster` 中修改 `_ws_clients` 集合用 `.difference_update()` 原地操作，禁止 `-=` 赋值（会触发 `UnboundLocalError`）

## 协作偏好（Claude 参考）

- 用户倾向于**自己执行命令**，不喜欢 Claude 直接运行脚本（尤其涉及进程启停）
- **git commit/push 需用户明确要求**才执行
- 沟通风格：中文，简洁直接，抓住重点，精简解释
- 涉及破坏性或影响共享状态的操作（push、merge、删除文件等），先确认再执行
