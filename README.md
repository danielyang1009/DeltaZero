# DeltaZero

ETF 期权 PCP 套利工具，当前采用三层结构：
- 数据采集层：Wind / DDE
- 数据总线层：`data_bus`
- 消费层：`monitor`（仅 ZMQ）

## 快速启动

```bash
pip install -r requirements.txt
python console.py
```

默认页面：`http://127.0.0.1:8787`

## 日常流程

1. 打开 Wind 或交易软件（DDE）。
2. 在控制台执行“抓取今日期权链”。
3. 启动 DataBus（Wind 或 DDE）。
4. 启动 Monitor。
5. 收盘后执行“合并今日分片”并关闭进程。

## 关键命令

```bash
# 抓取合约链
python -m data_engine.optionchain_fetcher

# 启动 DataBus
python -m data_bus.bus --source wind
python -m data_bus.bus --source dde
python -m data_bus.bus --source dde --no-persist   # 仅广播不落盘

# 启动 Monitor（只读 ZMQ）
python -m monitors.monitor
python -m monitors.monitor --zmq-port 5555
```

## 模块命名（当前标准）

- `data_engine.optionchain_fetcher`
- `data_engine.contract_catalog`
- `data_engine.tick_data_loader`
- `data_engine.bar_data_loader`
- `data_engine.dde_adapter`
- `backtest.etf_price_simulator`

## 数据目录约定

- 默认市场数据目录固定为：`D:\MARKET_DATA`
- DataBus 的快照、分片、日合并文件均写入该目录：
  - `D:\MARKET_DATA\snapshot_latest.parquet`
  - `D:\MARKET_DATA\chunks\`
  - `D:\MARKET_DATA\options_YYYYMMDD.parquet`
  - `D:\MARKET_DATA\etf_YYYYMMDD.parquet`

不使用仓库根目录存储运行数据。

