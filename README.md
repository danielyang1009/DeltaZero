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

# 抓取 Shibor + 中债国债收益率曲线（从官网/Excel 原样爬取，横表 CSV）
python -m data_engine.bond_termstructure_fetcher --kind all
python -m data_engine.bond_termstructure_fetcher --kind shibor --date 2026-03-05
python -m data_engine.bond_termstructure_fetcher --kind cgb

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
- `data_engine.bond_termstructure_fetcher`：从 Shibor 官网与中债官网爬取当日期限结构，并横表落盘
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
- 宏观期限结构（`bond_termstructure_fetcher`）：
  - `D:\MARKET_DATA\macro\shibor\shibor_yieldcurve_YYYYMMDD.csv`（8 个 Shibor 期限，横表）
  - `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`（17 个中债国债期限：0.0y～50y，横表）

### 无风险利率曲线（CBOE VIX 用）

- 利率构建类：`calculators.yield_curve.BoundedCubicSplineRate`
- 默认从当天的中债国债收益率曲线文件加载，路径：
  - `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`
- 关键用法示例：

```python
from datetime import date
from calculators.yield_curve import BoundedCubicSplineRate

# 使用“今天”曲线，文件不存在会直接报错
curve_today = BoundedCubicSplineRate.from_cgb_daily()

# 显式指定某一天的曲线
curve_20260305 = BoundedCubicSplineRate.from_cgb_daily(target_date=date(2026, 3, 5))
```

> `from_cgb_daily` 会校验文件内 `date` 列与指定日期一致；不一致或缺失时抛出异常，避免用错曲线。

## 交易参数（Monitor 净利润计算）

Monitor 展示的**净利润**为正向套利（买 ETF + 买 Put + 卖 Call）的预估利润，公式：

```
每股利润 = K - (S_ask + P_ask - C_bid)
净利润   = 每股利润 × 乘数 - ETF手续费 - 期权双边手续费
```

| 符号 | 含义 |
|------|------|
| K | 行权价 |
| S_ask | ETF 卖一价（买 ETF 的成交价） |
| P_ask | Put 卖一价（买 Put 的成交价） |
| C_bid | Call 买一价（卖 Call 的成交价） |
| 乘数 | 合约单位（标准 10000，调整型如 10265） |

### 费用扣除

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `etf_fee_rate` | 0.00020 | ETF 现货单边规费（含佣金+过户费，约万 2） |
| `option_round_trip_fee` | 3.0 | 期权双边固定手续费（元） |

配置位于 `config/settings.py` 的 `FeeConfig`，策略在 `strategies/pcp_arbitrage.py` 的 `_compute_forward_metrics` 中计算。

## Monitor 辅助指标说明

Monitor 每行除净利润外，还展示以下辅助指标，用于判断能否实际成交：

| 列名 | 含义 | 计算方式 |
|------|------|----------|
| **Max_Qty** | 可成交组数上限（张） | `min(C_bid量, P_ask量, floor(S_ask量×100÷乘数))` |
| **SPRD** | 盘口价差率（%） | `max((C_ask−C_bid)/C_mid, (P_ask−P_bid)/P_mid)`，取 Call/Put 较大值 |
| **OBI_C** | Call 买一档成交支撑 | `C_bid量 ÷ (C_bid量+C_ask量)`，卖 Call 需买一支撑强（靠近 1.0） |
| **OBI_S** | ETF 卖一档成交支撑 | `S_ask量 ÷ (S_ask量+S_bid量)`，买 ETF 需卖一充足（靠近 1.0） |
| **OBI_P** | Put 卖一档成交支撑 | `P_ask量 ÷ (P_bid量+P_ask量)`，买 Put 需卖一充足（靠近 1.0） |
| **Net_1T** | 单 tick 滑点后净利润（元） | 假设 ETF 滑 +0.001、Put +0.0001、Call −0.0001 后重算净利润 |
| **TOL** | 容错空间（tick 倍数） | `净利润 ÷ (净利润−Net_1T)`，即当前利润可承受多少个最坏 tick |

**决策参考：**
- `Max_Qty ≥ 1`：基本有量可做
- `SPRD < 5%`：价差合理，报价可信
- `OBI_C > 0.5`、`OBI_S > 0.5`、`OBI_P > 0.5`：买卖方向流动性支撑较强
- `Net_1T > 0`：即使滑一个 tick 仍盈利
- `TOL > 2`：有较宽松的容错空间

不使用仓库根目录存储运行数据。

