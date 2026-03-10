# DeltaZero

ETF 期权 PCP 套利工具，采用四层结构：
- 数据采集层：Wind / DDE（纯 Python 直连，无需 Excel）
- 数据总线层：`data_bus`
- 消费层：`monitor`（ZMQ）、`web`（FastAPI + WebSocket）
- 计算层：向量化 Black-76 IV 求解器

## 快速启动

```bash
pip install -r requirements.txt
python console.py
```

默认页面：`http://127.0.0.1:8787`

## 日常流程

1. 打开 Wind 或通达信。
2. 在控制台执行"抓取今日期权链"。
3. 启动 DataBus（Wind 或 DDE）。
4. 启动 Monitor。
5. 收盘后执行"合并今日分片"并关闭进程。

### DDE 启动前置步骤

1. 启动通达信（确保 TdxW DDE Server 已激活）
2. 确认 `metadata/wxy_options.xlsx` 已就位（含 3 个 Sheet：`50etf` / `300etf` / `500etf`）——DDE topic 地址从此文件解析
3. 在控制台启动 DDEBus（`--source dde`）

> `data_bus/dde_direct_client.py` 用 pywin32 直连通达信（无需 Excel 运行），topic 地址从 `wxy_*.xlsx` 读取，未找到时回退 `{交易所}{代码}` 推算。

### DDE 监控页面（`/dde`）

纯监控视图，读取 market_cache LKV 快照展示实时数据，不提供启停控制。

| API | 说明 |
|-----|------|
| `GET /api/dde/state` | DataBus 运行状态 + LKV 合约统计（期权/ETF 数量） |
| `GET /api/dde/poll` | 完整行情快照，含健康状态（STALE 超时 90s） |

## 关键命令

```bash
# 抓取合约链
python -m data_engine.optionchain_fetcher

# 抓取 Shibor + 中债国债收益率曲线
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
- `data_engine.bond_termstructure_fetcher`：从 Shibor 官网与中债官网爬取当日期限结构，横表落盘
- `data_engine.contract_catalog`
- `data_engine.tick_data_loader`
- `data_engine.bar_data_loader`
- `data_engine.dde_adapter`
- `data_bus.dde_direct_client`：纯 Python DDE 直连通达信（pywin32，Excel 无需运行，topic 仍来自 `wxy_*.xlsx`）
- `calculators.vectorized_pricer`：向量化 Black-76 IV 求解器（NumPy NR）
- `backtest.etf_price_simulator`

## 数据目录约定

- DDE 路由表（旧版）：`metadata/wxy_options.xlsx`，含 3 个 Sheet（`50etf` / `300etf` / `500etf`）
- 合约元数据：`metadata/wind_sse_optionchain.xlsx`（新版 DDE 直连的 topic 也从此文件推算）
- 默认市场数据目录固定为：`D:\MARKET_DATA`
- DataBus 的快照、分片、日合并文件均写入该目录（按品种子目录存储）：
  - `D:\MARKET_DATA\snapshot_latest.parquet`（全量，Monitor 冷启动用）
  - `D:\MARKET_DATA\chunks\{510050|510300|510500}\options_YYYYMMDD_HHmmss.parquet`
  - `D:\MARKET_DATA\{510050|510300|510500}\options_YYYYMMDD.parquet`（日终合并）
  - `D:\MARKET_DATA\{510050|510300|510500}\etf_YYYYMMDD.parquet`
- Parquet 压缩：zstd；options/snapshot 的 askv1/bidv1 为 int16，ETF 保持 int32
- 宏观期限结构（`bond_termstructure_fetcher`）：
  - `D:\MARKET_DATA\macro\shibor\shibor_yieldcurve_YYYYMMDD.csv`（8 个 Shibor 期限，横表）
  - `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`（17 个中债国债期限：0.0y～50y，横表）

### 无风险利率曲线

- 利率构建类：`calculators.yield_curve.BoundedCubicSplineRate`
- 默认从当天的中债国债收益率曲线文件加载，路径：
  - `D:\MARKET_DATA\macro\cgb_yield\cgb_yieldcurve_YYYYMMDD.csv`
- 关键用法示例：

```python
from datetime import date
from calculators.yield_curve import BoundedCubicSplineRate

# 使用"今天"曲线；当日文件不存在时自动回退至 7 日内最新文件
curve_today = BoundedCubicSplineRate.from_cgb_daily()

# 显式指定某一天的曲线
curve_20260305 = BoundedCubicSplineRate.from_cgb_daily(target_date=date(2026, 3, 5))
```

> `from_cgb_daily` 优先加载当日文件；若不存在，自动回退至 7 个自然日内最新文件（回退时发出 Warning）；7 日内均无文件则抛 `FileNotFoundError`。

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

## 波动率微笑（Vol Smile）

访问 `http://127.0.0.1:8787/vol_smile`，实时展示 50ETF / 300ETF / 500ETF 期权的隐含波动率微笑曲线与 IV 数据表格。

### 实时推送架构（单进程四分离）

```
ZMQ SUB（market-cache-zmq 线程）
      ↓ CONFLATE=1，只保最新消息
market_cache._lkv（内存 LKV 快照）
      ↓ 每 100ms 微批次
market_cache._compute_loop（market-cache-compute 线程）
      ↓ 向量化 NR → loop.call_soon_threadsafe → asyncio.Queue
dashboard._ws_broadcaster（FastAPI 事件循环）
      ↓ WebSocket 推送
vol_smile.html：requestAnimationFrame 增量渲染
```

页面通过 WebSocket `/ws/vol_smile` 接收推送，断线 2s 自动重连，无需手动刷新。

### 核心算法：向量化 Black-76 + 隐含远期

为规避 A 股融券成本高昂及股息率难以估计的问题，弃用标准 Black-Scholes 的现货 $S$ 与股息率 $q$，改用**隐含远期 + Black-76** 框架。

#### Step 1：倒算隐含远期价格 $F$

从同一行权价的认购、认沽中间价出发，利用 Put-Call Parity 反推：

$$F = K_{atm} + (C_{mid} - P_{mid}) \cdot e^{rT}$$

其中 $K_{atm}$ 为满足 $\arg\min |C_{mid} - P_{mid}|$ 的行权价（市场隐含平值点）。

#### Step 2：向量化 Newton-Raphson 求解 IV

`calculators/vectorized_pricer.py` 的 `VectorizedIVCalculator` 对整个到期日所有合约一次性向量化迭代（无 Python for 循环），含三条金工容错机制：

| 保护 | 机制 |
|------|------|
| **[GUARD-1]** 边界违规过滤 | Call/Put 价格低于无套利下界时直接输出 `nan`，不进入迭代 |
| **[GUARD-2]** Vega 坍缩保护 | Vega 安全地板 `1e-8` + 步长截断 `[-0.5, 0.5]`，防深度虚值发散 |
| **[GUARD-3]** T 精度 | `time.time()` 毫秒 Unix 时间戳，消除 Windows `datetime.now()` 分辨率损失 |

### IV 数据表格

页面下方表格实时显示各行权价的：

| 列 | 说明 |
|----|------|
| Call IV / Put IV | 中间价对应 IV |
| Call/Put Bid/Ask IV | 买卖价对应 IV |
| IV Skew (C−P) | 同行权价 Call IV 减 Put IV |
| PCP 偏差 | `C_mid + K·disc − P_mid − F·disc`（偏离 0 表示 PCP 套利机会） |

行级告警：超过 PCP 阈值（默认 0.003）黄色高亮，超过 Skew 阈值（默认 0.02）红色高亮，ATM 行蓝色高亮。

### 实现文件

| 文件 | 说明 |
|------|------|
| `calculators/iv_calculator.py` | `calc_implied_forward()` + `black76_price()` + `calc_iv_black76()` |
| `calculators/vectorized_pricer.py` | `VectorizedIVCalculator`（向量化 NR + Greeks，GUARD-1/2/3） |
| `web/market_cache.py` | ZMQ SUB 线程 + compute 线程 + `get_rich_snapshot()` |
| `web/dashboard.py` | `/ws/vol_smile` WS endpoint + `_ws_broadcaster` + `/api/vol_smile` HTTP 端点 |
| `web/templates/vol_smile.html` | WS 客户端 + rAF 增量渲染 + IV 表格 + 阈值告警 |

### 无风险利率

优先从当日中债国债收益率曲线（`cgb_yieldcurve_YYYYMMDD.csv`）按实际剩余期限取值，7 日内无文件则回退固定 2%。
