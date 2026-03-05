# STATE

最后更新：2026-03-05

## 当前架构

- 采集层：`data_bus/wind_subscriber.py`、`data_bus/dde_subscriber.py`
- 总线层：`data_bus/bus.py`（ZMQ 广播 + 可选落盘）
- 消费层：`monitors/monitor.py`（仅 ZMQ）

## 关键约束

- 默认数据目录：`D:\MARKET_DATA`
- Monitor 不再直连 Wind，仅消费 ZMQ。
- DataBus 支持 `--no-persist`（仅广播，不写磁盘）。

## 入口

- 控制台：`python console.py`
- DataBus：`python -m data_bus.bus`
- Monitor：`python -m monitors.monitor`

## 标准模块（已切换完成）

- `data_engine.optionchain_fetcher`
- `data_engine.contract_catalog`
- `data_engine.tick_data_loader`
- `data_engine.bar_data_loader`
- `data_engine.dde_adapter`
- `backtest.etf_price_simulator`

