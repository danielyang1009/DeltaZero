# DeltaZero

ETF 期权 PCP 套利工具，采用四层流水线结构：

```
【第 1 层】数据采集层
  数据源（Wind API / DDE）
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

| 层 | 模块 | 说明 |
|----|------|------|
| 数据采集 | `data_bus/bus.py` | 消费 `WindSubscriber` 或 `DDESubscriber` 的 tick，写 Parquet 分片，同时 ZMQ PUB 广播 |
| 数据总线 | ZMQ PUB 5555 | 统一消息格式：`OPT_` / `ETF_` 前缀；每 30 秒刷盘，15:10 自动日终合并 |
| 消费层 | `monitors/monitor.py` | ZMQ SUB → `PCPArbitrage.scan_pairs_for_display()` → Rich 终端表格 |
| 消费层 | `web/market_cache.py` | ZMQ SUB（CONFLATE=1）→ LKV → 每 100ms 向量化 IV → asyncio Queue → WS 推送 |

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

> `data_bus/dde_direct_client.py` 用 ctypes DDEML 直连通达信（无需 Excel 运行），service/topic 地址**仅**来自 `wxy_*.xlsx`，无对应 topic 的合约直接跳过。

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
- `data_bus.dde_direct_client`：纯 Python DDE 直连通达信（ctypes DDEML，Excel 无需运行，topic 来自 `wxy_*.xlsx`）
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

## DDE 技术说明

### 什么是 DDEML ADVISE 模式

**DDE（Dynamic Data Exchange）** 是 Windows 1987 年引入的 IPC 机制，至今仍被国内行情软件（通达信/QD 等）用于对外暴露实时行情。

Windows 提供两套 DDE API：
- **原始 DDE（WM_DDE_\*）**：直接收发 Windows 消息，极难用
- **DDEML**（DDE Management Library）：`user32.dll` 里的高层封装，本项目使用此套

本项目用 Python `ctypes` 直调 `user32.dll` 中的 DDEML 函数，等价于 C 代码调 Win32 API。

**ADVISE 模式（热链接）**：客户端注册订阅后，服务端有更新时**主动推送**，无需轮询。区别于 REQUEST（冷链接，问一次答一次）。

### 完整数据流

```
行情软件（QD）
   │  Windows 消息总线
   ▼
_DDEClient._dde_callback()      ← DDEML 在消息泵线程触发
   │  解析 XlTable 二进制流（type=0x0001 FLOAT 记录）
   ▼
_tick_buf                        ← 攒齐 5 个字段后触发回调
   ▼
DDEDirectSubscriber._on_tick() → tick_queue → DataBus ZMQ PUB
```

### 关键实现细节

| 步骤 | API | 说明 |
|------|-----|------|
| 初始化 | `DdeInitializeW` | 注册为纯客户端，传入回调函数指针 |
| 建连 | `DdeConnect` | service=`"QD"`，topic=xlsx 里的不透明数字（如 `"2206355670"`） |
| 订阅 | `DdeClientTransaction(XTYP_ADVSTART)` | 每个字段（LASTPRICE 等）单独注册 |
| 消息泵 | `PeekMessageW` 循环 | DDEML 回调通过 Windows 消息队列派发，**必须在同一线程内持续运行** |

**XlTable 二进制格式**（ADVISE 回调收到的数据）：
```
偏移 0: type=0x0010 (TABLE), size=4  → 容器头，跳过
偏移 8: type=0x0001 (FLOAT), size=8  → struct.unpack("<d") 读 IEEE 754 双精度浮点
```

### 为什么不用 pywin32

`pywin32.dde` 的 `ConnectTo()` 对 QD 服务握手方式不兼容，连接必定失败。ctypes DDEML 是唯一可靠路径。

### 为什么 topic 不能推算

topic 是行情软件内部的不透明数字字符串（如 `"2206355670"`），软件升级后可能改变，**只能从 `metadata/wxy_options.xlsx` 的 externalLink XML 中读取**，禁止用代码规则推算。

### wxy_options.xlsx 解析细节

xlsx 是 ZIP，`_load_topic_map()` 解析其中的 `xl/externalLinks/externalLink*.xml`：

| 字段 | XML 属性 | 实际值 |
|------|----------|--------|
| service | `ddeService` | `"QD"`（不是 `"TdxW"`，写错则全部连接失败） |
| topic | `ddeTopic` | 每个合约对应一个不透明数字（如 `"2206355670"`） |
| item | 列名 | `LASTPRICE`、`BIDPRICE1`、`ASKPRICE1`、`BIDVOLUME1`、`ASKVOLUME1` |

`_load_topic_map()` 在 DataBus 启动时读取一次，返回 `(code→topic dict, service_name)`。

### 禁止事项

- **禁止用 `pywin32 dde` 模块替换**：`ConnectTo()` 对 QD 服务连接必定失败，只有 ctypes DDEML 可用
- **禁止将 DDE 操作改为异步或多线程并发**：`DdeConnect` 依赖 Windows 消息泵，必须在单一线程内串行调用并在每次 connect 后立即 `_pump_messages()`
- **禁止从代码推算 service/topic**：所有地址信息来自 xlsx，软件升级后地址可能改变

### DDE 测试流程

1. 确认 `metadata/wxy_options.xlsx` 已放入 `metadata/`
2. 启动 DataBus：`python -m data_bus.bus --source dde`
3. 30 秒后查看自检日志：`DDE 自检(30s): 累计=N tick, 期权标的=[...]`

---

## 最近变更

- **DDE 死代码清理**：移除 `_code_to_topic()`、`_xls_read_external_links()`、`make_zmq_on_tick()` 等无用代码；默认 service 由 `TdxW` 修正为 `QD`；README 补充 DDEML ADVISE 模式技术说明
- **DataBus 独立运行**：通过 `CREATE_NEW_CONSOLE` 在独立窗口启动，关闭控制台不影响 DataBus 继续落盘；移除控制台内的日志流面板
- **Vol Smile 标准/调整合约分离**：`market_cache` 按 `(expiry_date, is_adjusted)` 双键分组，WS 推送新增 `adj_expiries` 字段；前端"调整合约"选项按当前选中到期日动态显隐
- **进程总览新增 VolSmile 计算条目**：实时显示 LKV 数量与 zmq/compute 线程状态（内置线程，无关闭按钮）
- **Monitor 参数集中管理**：`config/settings.py` 新增 `DEFAULT_MIN_PROFIT=36`、`DEFAULT_EXPIRY_DAYS=90`、`DEFAULT_N_EACH_SIDE=0`、`DEFAULT_REFRESH_SECS=3`；控制台通过 `/api/state` 下发默认值，输入框自动填充，修改参数只需改 `settings.py` 一处

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

#### HTTP 兜底路径：Brent 法

`/api/vol_smile` HTTP 端点调用 `calc_iv_black76()`（`calculators/iv_calculator.py`），使用 **Brent 法**（`scipy.optimize.brentq`）求解单合约 IV：

$$\sigma^* = \mathop{\text{RootFind}}_{\sigma \in [10^{-4},\, 5.0]} \bigl( \text{Black76}(F,K,T,r,\sigma) - \text{Price}_{mid} = 0 \bigr)$$

Brent 法（二分 + 割线法混合）要求搜索区间端点异号；若端点同号（深度虚值、价格违反无套利边界），直接返回 `nan`。相比 NR，Brent 在 Vega 极小场景下无发散风险，适合单合约 HTTP 调用场景。

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
| `calculators/iv_calculator.py` | `calc_implied_forward()` + `black76_price()` + `calc_iv_black76()`（Brent 法，HTTP 兜底） |
| `calculators/vectorized_pricer.py` | `VectorizedIVCalculator`（向量化 NR + Greeks，GUARD-1/2/3） |
| `web/market_cache.py` | ZMQ SUB 线程 + compute 线程 + `get_rich_snapshot()` |
| `web/dashboard.py` | `/ws/vol_smile` WS endpoint + `_ws_broadcaster` + `/api/vol_smile` HTTP 端点 |
| `web/templates/vol_smile.html` | WS 客户端 + rAF 增量渲染 + IV 表格 + 阈值告警 |

### 无风险利率

优先从当日中债国债收益率曲线（`cgb_yieldcurve_YYYYMMDD.csv`）按实际剩余期限取值，7 日内无文件则回退固定 2%。
