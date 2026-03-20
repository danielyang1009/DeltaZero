# Parquet 数据结构

## 期权分片 / 日文件（`options_*.parquet`）

| 列名 | 类型 | 说明 |
|------|------|------|
| `ts` | int64 | Unix 时间戳（毫秒） |
| `code` | string | 合约代码，如 `10006217.SH` |
| `underlying` | string | 标的代码，如 `510050.SH` |
| `last` | float32 | 最新价 |
| `ask1` | float32 | 卖一价 |
| `bid1` | float32 | 买一价 |
| `askv1` | int16 | 卖一量（手） |
| `bidv1` | int16 | 买一量（手） |
| `oi` | int32 | 持仓量 |
| `vol` | int32 | 成交量 |
| `high` | float32 | 当日最高价 |
| `low` | float32 | 当日最低价 |
| `is_adjusted` | bool | 是否分红调整型合约 |
| `multiplier` | int32 | 合约乘数（标准 10000，调整型如 10265） |

## ETF 分片 / 日文件（`etf_*.parquet`）

| 列名 | 类型 | 说明 |
|------|------|------|
| `ts` | int64 | Unix 时间戳（毫秒） |
| `code` | string | ETF 代码，如 `510050.SH` |
| `last` | float32 | 最新价 |
| `ask1` | float32 | 卖一价 |
| `bid1` | float32 | 买一价 |
| `askv1` | int32 | 卖一量（股，量级大故用 int32） |
| `bidv1` | int32 | 买一量（股） |

## 快照文件（`snapshot_latest.parquet`）

期权 + ETF 合并，每个合约只保留最新一条，Schema 为上述两表的超集，额外含 `type`（`"option"` / `"etf"`）列。

## 文件路径约定

| 文件 | 路径 |
|------|------|
| 全量快照（Monitor 冷启动） | `D:\MARKET_DATA\snapshot_latest.parquet` |
| 期权分片 | `D:\MARKET_DATA\chunks\{510050\|510300\|510500}\options_YYYYMMDD_HHmmss.parquet` |
| 期权日文件（日终合并） | `D:\MARKET_DATA\{510050\|510300\|510500}\options_YYYYMMDD.parquet` |
| ETF 日文件 | `D:\MARKET_DATA\{510050\|510300\|510500}\etf_YYYYMMDD.parquet` |

Parquet 压缩：zstd；options/snapshot 的 askv1/bidv1 为 int16，ETF 保持 int32。

## 宏观期限结构文件

| 类型 | 路径 |
|------|------|
| Shibor 曲线 | `D:\MARKET_DATA\macro\shibor\shibor_yieldcurve_YYYYMMDD.csv`（8 个期限，横表） |
| 中债国债曲线 | `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`（17 个期限：0.0y～50y，横表） |

## 无风险利率曲线用法

利率构建类：`calculators.yield_curve.BoundedCubicSplineRate`

```python
from datetime import date
from calculators.yield_curve import BoundedCubicSplineRate

# 使用"今天"曲线；当日文件不存在时自动回退至 7 日内最新文件
curve_today = BoundedCubicSplineRate.from_cgb_daily()

# 显式指定某一天的曲线
curve_20260305 = BoundedCubicSplineRate.from_cgb_daily(target_date=date(2026, 3, 5))
```

> `from_cgb_daily` 优先加载当日文件；若不存在，自动回退至 7 个自然日内最新文件（回退时发出 Warning）；7 日内均无文件则抛 `FileNotFoundError`。
