# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
# Web 控制台（主入口，http://127.0.0.1:8787）
python console.py

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

## 架构：三层流水线

```
数据源（Wind API / DDE）
       ↓
data_bus/bus.py          — ZMQ PUB（tcp://127.0.0.1:5555）+ 可选 Parquet 落盘
       ↓
monitors/monitor.py      — ZMQ SUB，Rich 终端 UI 实时刷新
web/dashboard.py         — FastAPI 控制台，负责启停子进程
```

**DataBus（`data_bus/bus.py`）**：消费来自 `WindSubscriber` 或 `DDESubscriber` 的 tick，写入 Parquet 分片（`D:\MARKET_DATA\chunks\`），同时通过 `ZMQPublisher` 广播 `OPT_` / `ETF_` 前缀消息。每 30 秒刷盘，15:10 自动触发日终合并为 `options_YYYYMMDD.parquet` / `etf_YYYYMMDD.parquet`，并维护 `snapshot_latest.parquet` 供 Monitor 冷启动恢复。

**Monitor（`monitors/monitor.py`）**：订阅 ZMQ，调用 `PCPArbitrage.scan_pairs_for_display()` 计算信号，用 `rich.Live` 渲染终端表格。共享逻辑（合约加载、快照恢复、消息解析）在 `monitors/common.py`。

**Web 控制台（`web/dashboard.py`）**：FastAPI + 单页 HTML（`web/templates/index.html`），通过 `spawn_module()` 以子进程方式启停 DataBus 和 Monitor，前端轮询 `/api/status`。

## 核心数据流

1. **合约信息**：`data_engine/contract_catalog.py` 的 `ContractInfoManager` 从 `metadata/` 加载 optionchain 文件（优先当日 CSV，无则回退 `*optionchain*.xlsx`）。支持 xlsx（Wind 导出格式，按列位置解析）。
2. **Tick 数据**：统一 `TickData` / `ETFTickData` dataclass（`models.py`），代码一律用 `.SH` 后缀（`normalize_code()` 负责标准化）。
3. **套利计算**：`strategies/pcp_arbitrage.py` 的 `PCPArbitrage` 维护 `TickAligner`（LKV 快照），`_compute_forward_metrics()` 计算净利润及辅助指标（Max_Qty、SPRD、OBI、Net_1T、TOL）。
4. **VIX**：`calculators/vix_engine.py`，利率曲线来自 `calculators/yield_curve.BoundedCubicSplineRate`，读取 `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`，文件不存在时回退固定利率 2%。

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
