# DeltaZero 数据源替换指南

## 当前架构中 Wind 的耦合情况

系统中 Wind 依赖分布在**两条独立的数据管线**上。

### 管线 1：合约信息（静态数据，开盘前一次性获取）

| 环节 | 文件 | Wind 依赖程度 |
|------|------|-------------|
| 抓取期权链 | `data_engine/fetch_optionchain.py` | **强依赖** `w.wset("optionchain")` |
| 解析合约信息 | `data_engine/contract_info.py` | **无依赖**，只读 CSV 文件 |

`ContractInfoManager` 只认 CSV 文件（`metadata/YYYY-MM-DD_optionchain.csv`），不关心 CSV 是怎么来的。只要能用别的方式生成同格式的 CSV，这一层就能完全脱离 Wind。

所需的 CSV 列：

```
option_code, option_name, us_code, strike_price, month, call_put,
first_tradedate, last_tradedate, multiplier
```

### 管线 2：实时行情（核心运行时数据）

Wind 的介入有**两种模式**，各自独立：

| 模式 | 入口 | Wind 使用方式 | 下游消费者 |
|------|------|-------------|-----------|
| **Wind 直连** | `monitors/monitor.py` → `poll_snapshot()` | 同步 `w.wsq()` 批量拉 | `strategy.on_etf_tick()` / `on_option_tick()` |
| **DataBus + ZMQ** | `data_bus/wind_subscriber.py` | Push 回调 `w.wsq(func=cb)` | tick 入队 → Parquet 写盘 + ZMQ 广播 |

系统已内置完整的数据源解耦层——**ZMQ 模式**：

```
[数据源] → TickPacket/JSON → ZMQ PUB → monitor
                                        ↓
                              parse_zmq_message() → TickData / ETFTickData
                                        ↓
                              strategy.on_option_tick() / on_etf_tick()
```

策略层 (`PCPArbitrage`) 和显示层 (`monitor.py`) 只依赖两个标准数据模型 `TickData` 和 `ETFTickData`，**完全不知道数据来自哪里**。

---

## 替换 Wind 需要做什么

### 1. 实时行情（影响最大，但架构已支持）

只需写一个新的 Subscriber 替代 `WindSubscriber`，让它：

- 从新数据源（CTP / 东方财富 / 同花顺 iFinD / Tushare Pro / QMT 等）获取实时行情
- 将数据转换为 `TickData` / `ETFTickData`
- 放入同一个 `Queue`（走 Recorder 模式）或直接发 ZMQ 消息

ZMQ 消息格式（已在 `monitors/common.py` 的 `parse_zmq_message` 中定义）：

```json
// 期权 tick
{
  "code": "10000001.SH",
  "ts": 1709640000000,
  "type": "option",
  "last": 0.1721,
  "ask1": 0.1735,
  "bid1": 0.1721,
  "vol": 100,
  "oi": 5000,
  "high": 0.1750,
  "low": 0.1700
}

// ETF tick
{
  "code": "510050.SH",
  "ts": 1709640000000,
  "type": "etf",
  "last": 3.063,
  "ask1": 3.064,
  "bid1": 3.063
}
```

只要新数据源能产出这种 JSON 并发到 ZMQ，现有 monitor **零修改**就能工作。

### 2. 合约信息（简单）

`fetch_optionchain.py` 需要替换数据源，但 `ContractInfoManager` 只读 CSV。可选方案：

- 从交易所官网下载期权合约列表，转换为同格式 CSV
- 用 Tushare / AKShare / iFinD 等接口获取期权链
- 手动维护（合约信息变化很少，一天更新一次即可）

### 3. Monitor 模式

`monitor.py` 仅保留 ZMQ 消费模式，与 Wind 直连已解耦。

---

## 替换成本一览

| 组件 | 文件 | Wind 依赖 | 替换难度 | 替换方案 |
|------|------|----------|---------|---------|
| PCP 套利策略 | `strategies/pcp_arbitrage.py` | 无 | 无需替换 | — |
| Monitor 显示 | `monitors/monitor.py` | 无（ZMQ 模式） | 无需替换 | `python -m monitors.monitor` |
| 数据模型 | `models.py` | 无 | 无需替换 | — |
| 合约管理器 | `data_engine/contract_info.py` | 无 | 无需替换 | — |
| 期权链抓取 | `data_engine/fetch_optionchain.py` | **强** | 低 | 换数据源生成同格式 CSV |
| 实时行情订阅 | `data_bus/wind_subscriber.py` | **强** | 中 | 写新 Subscriber，输出到同一 Queue |
| Wind 直连轮询 | — | 无 | 已移除 | 统一用 ZMQ 模式 |
| Wind 适配器 | `data_engine/wind_adapter.py` | **强** | 低 | 已有 Mock 降级，回测不受影响 |
| Wind 工具函数 | `utils/wind_helpers.py` | **强** | 低 | 仅被 Wind 相关模块调用 |

---

## 新数据源接入步骤

### 步骤 1：实现新的 Subscriber

在 `data_bus/` 下创建新文件（如 `ctp_subscriber.py`），实现与 `WindSubscriber` 相同的接口：

```python
class NewSubscriber:
    def __init__(self, products: List[str], tick_queue: Queue, ...):
        ...

    def start(self) -> bool:
        """连接数据源，注册行情回调"""
        ...

    def stop(self) -> None:
        """断开连接"""
        ...
```

回调中将行情数据封装为 `TickPacket` 放入 `tick_queue`，后续 Parquet 写盘和 ZMQ 广播流程无需修改。

### 步骤 2：替换期权链获取

编写新的 `fetch_optionchain_from_xxx()` 函数，输出与现有 `fetch_optionchain_from_wind()` 相同格式的 DataFrame，保存为 `metadata/YYYY-MM-DD_optionchain.csv`。

### 步骤 3：修改 Recorder 入口

在 `data_bus/bus.py` 中将 `WindSubscriber` 替换为新的 Subscriber 类。

### 步骤 4：运行 Monitor

```bash
# ZMQ 模式（推荐，与数据源完全解耦）
python -m monitors.monitor
```

---

## 可选替代数据源参考

| 数据源 | 实时行情 | 期权链 | 费用 | 备注 |
|--------|---------|--------|------|------|
| CTP（期货公司柜台） | 支持 | 需额外获取 | 免费（开户） | 最稳定，需期货账号 |
| 同花顺 iFinD | 支持 | 支持 | 付费 | Wind 的主要替代品 |
| QMT（迅投） | 支持 | 支持 | 券商提供 | 部分券商免费 |
| Tushare Pro | 有延迟 | 支持 | 积分制 | 适合合约信息，实时行情有限 |
| AKShare | 有延迟 | 部分支持 | 免费 | 适合合约信息补充 |
| 东方财富 EMQuant | 支持 | 支持 | 付费 | API 风格类似 Wind |
