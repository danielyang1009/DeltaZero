<img src="web/static/DeltaZero.png" alt="DeltaZero" height="64"/>

ETF 期权 PCP 套利工具，采用四层流水线结构：

```
【第 1 层】数据采集层
  数据源（DDE）
       ↓
【第 2 层】数据总线层
  data_bus/bus.py          — ZMQ PUB（tcp://127.0.0.1:5555）+ 可选 Parquet 落盘
       ↓
【第 3 层】消费层（ZMQ SUB）
  web/market_cache.py      — CONFLATE=1 → LKV 快照 → compute 线程向量化 IV
       ↓
【第 4 层】展示层
  web/dashboard.py         — FastAPI 控制台 + WebSocket /ws/vol_smile 推送
```

| 层 | 模块 | 说明 |
|----|------|------|
| 数据采集 | `data_bus/bus.py` | 消费 DDE tick，写 Parquet 分片，ZMQ PUB 广播 |
| 数据总线 | ZMQ PUB 5555 | 统一消息格式：`OPT_` / `ETF_` 前缀；每 30 秒刷盘，15:10 日终合并 |
| 消费层 | `web/market_cache.py` | ZMQ SUB（CONFLATE=1）→ LKV → 每 100ms Brent 法 IV → WS 推送 |

## 策略架构：Alpha / Execution 分层

### 核心原则

1. **策略绝对无状态化**：`strategies/` 下所有策略类必须是纯函数式"数学大脑"，禁止在策略内维护字典、队列或历史状态。
2. **读写解耦**：策略只负责发现机会并输出 `ArbitrageSignal`，绝对不感知资金、持仓或撮合逻辑。
3. **单一数据真相**：实盘和回测必须且只能通过 `MarketSnapshot`（由 `TickAligner` 生成）与策略交互。

### 架构全景图

```
       【管线 A：实时实盘流】                      【管线 B：历史回测流】
    (Live Market Data: DDE/ZMQ)             (Historical Data: Parquet)
                 │                                        │
                 ▼                                        ▼
      [ web/market_cache.py ]               [ backtest/data_feed.py ]
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
                                 ▼ (MarketSnapshot)
              =========================================
              ||         [ strategies/base.py ]      ||
              ||         PCPArbitrageStrategy        || 纯数学大脑 (Stateless)
              =========================================
                                 │
                                 ▼ (ArbitrageSignal)
              ┌──────────────────┴──────────────────┐
              │                                     │
              ▼                                     ▼
【管线 A 终点：视觉展示】                  【管线 B 终点：执行与账务】
[ web/market_cache.py ]                [ backtest/broker.py + engine.py ]
- WebSocket 推送至浏览器               - 哨兵拦截 / 跨价撮合 / 容量限制
- /monitor 套利监控页                              │
- /vol_smile 波动率微笑                           ▼ (TradeRecord)
(等待人类手动下单)                     [ backtest/portfolio.py ]
                                       - 资金预检 / 保证金 / 盈亏曲线
```

## 快速启动

```bash
pip install -r requirements.txt
python console.py
```

默认页面：`http://127.0.0.1:8787`

## 日常流程（SOP）

1. 打开无限易
2. 在无限易中对所需合约选择导出 DDE（真实开门机制）
3. **9:14 前**启动 DataBus（完整捕获 9:15 集合竞价数据）
4. 浏览器访问 `/monitor` 查看套利信号
5. 收盘后执行"合并今日分片"并关闭进程

### DDE 启动前置步骤

1. 启动行情软件（确保 QD DDE 服务已激活）
2. 确认 `metadata/wxy_options.xlsx` 已就位（含 3 个 Sheet：`50etf` / `300etf` / `500etf`）
3. 在控制台启动 DDEBus（`--source dde`）

> 详细原理见 [docs/dde_tech_spec.md](docs/dde_tech_spec.md)

### DDE 监控 API

| API | 说明 |
|-----|------|
| `GET /api/dde/state` | DataBus 运行状态 + LKV 合约统计 |
| `GET /api/dde/poll` | 完整行情快照（STALE 超时 90s） |

## 回测

访问 `/backtest` 或点击控制台"PCP 套利回测"按钮。

```bash
python -m backtest.run   # 命令行模式（输出 JSON 汇总）
```

### 回测参数

| 参数 | 说明 |
|------|------|
| 品种 | 50ETF / 300ETF / 500ETF，可多选 |
| 日期范围 | 按交易日过滤 Parquet 数据 |
| 初始资金 | 回测起始本金 |
| 最小利润 | 开仓触发门槛（元/组） |
| 到期天数上限 | 仅开仓剩余天数 ≤ N 的合约 |
| 最小剩余天数 | `min_dte_for_open`，防末日轮新开仓 |
| ATM 上下各 N 档 | 限制扫描行权价范围 |
| 手续费 | ETF 费率 + 期权单边费 |

### 回测输出

- **权益曲线**：逐日净值折线图
- **信号明细**：每笔开/平仓记录，含成交价、手数、净利润
- **汇总指标**：总收益、胜率、盈亏比、最大回撤、Kelly 仓位建议
- **CSV 导出**：信号明细可导出为 CSV

### 撮合机制

- **FOK 语义**：开仓 / 平仓均为全成或全撤
- **跨价撮合**：ETF 用卖一价买入，期权用对应方向挂单价
- **容量限制**：成交量上限来自策略填入的 `max_qty`（盘口一档量）
- **保证金**：按上交所公式预检，资金不足则拒单

## 关键命令

```bash
# 抓取利率曲线
python -m data_engine.bond_termstructure_fetcher --kind all

# 启动 DataBus
python -m data_bus.bus --source dde
python -m data_bus.bus --source dde --no-persist   # 仅广播不落盘

# 回测（命令行）
python -m backtest.run
```

## 模块目录

| 目录 | 职责 |
|------|------|
| `data_bus/` | ZMQ PUB + Parquet 落盘；ctypes DDEML 直连行情软件 |
| `data_engine/` | `TickAligner` LKV 状态机；合约元数据；数据加载器 |
| `strategies/` | `BaseStrategy` 基类；`PCPArbitrageStrategy` 无状态策略 |
| `backtest/` | `Broker` 撮合校验；`Engine` 主循环；`Portfolio` 会计层 |
| `calculators/` | Black-76 IV 求解（Brent 法）；三次样条利率曲线 |
| `analysis/` | `PnLAnalyzer` 多态防腐层，信号类型分派结算 |
| `web/` | FastAPI 控制台；ZMQ LKV；WebSocket 推送；回测服务 |
| `monitors/` | 共享工具（`common.py`）；ZMQ 消息解析 |

## 数据目录约定

- DDE 路由表：`metadata/wxy_options.xlsx`（topic 唯一来源，禁止推算）
- 合约元数据：`metadata/wind_sse_optionchain.xlsx`
- 默认市场数据目录：`D:\MARKET_DATA`
- Parquet 分片、日合并、快照均写入该目录

> Schema 详情见 [docs/data_schema.md](docs/data_schema.md)

## 交易参数

套利净利润公式：

```
每股利润 = K - (S_ask + P_ask - C_bid)
净利润   = 每股利润 × 乘数 - ETF手续费 - 期权双边手续费
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `etf_fee_rate` | 0.00020 | ETF 现货单边规费（约万 2） |
| `option_round_trip_fee` | 3.0 | 期权双边固定手续费（元） |

## Monitor 辅助指标

套利监控页（`/monitor`）每行除净利润外，还展示以下辅助指标，用于判断能否实际成交：

| 列名 | 含义 | 计算方式 |
|------|------|----------|
| **Net_1T** | 单 tick 滑点后净利润（元） | 假设 ETF 滑 +0.001、Put +0.0001、Call −0.0001 后重算净利润（越大越好） |
| **Max_Qty** | 可成交组数上限（张） | `min(C_bid量, P_ask量, floor(S_ask量×100÷乘数))`（越大越好，必须 ≥ 1） |
| **TOL** | 容错空间（tick 倍数） | 净利润 ÷ (净利润−Net_1T)，即当前利润可承受多少个最坏 tick（越大越好，必须 ≥ 1） |
| **SPRD** | 盘口价差率（%） | `max((C_ask−C_bid)/C_mid, (P_ask−P_bid)/P_mid)`，取 Call/Put 较大值（越小越好） |
| **OBI_S** | ETF 卖一档成交支撑 | `S_ask量 ÷ (S_ask量+S_bid量)`，买 ETF 需卖一充足（越靠近 1.0） |
| **OBI_C** | Call 买一档成交支撑 | `C_bid量 ÷ (C_bid量+C_ask量)`，卖 Call 需买一支撑强（越靠近 1.0） |
| **OBI_P** | Put 卖一档成交支撑 | `P_ask量 ÷ (P_bid量+P_ask量)`，买 Put 需卖一充足（越靠近 1.0） |

**决策参考：**

- **Net_1T > 0**：即使滑一个 tick 仍盈利
- **Max_Qty ≥ 1**：基本有量可做
- **TOL > 2**：有较宽松的容错空间
- **SPRD < 5%**：价差合理，报价可信
- **OBI_S > 0.5、OBI_C > 0.5、OBI_P > 0.5**：买卖方向流动性支撑较强

## 最近变更

- **删除终端 Rich UI Monitor**：监控入口统一为浏览器 `/monitor` 页面（WebSocket 实时推送）
- **回测 Web UI 上线**：`/backtest` 页面支持多品种、多日期回测，含进度推送、信号明细、权益曲线、CSV 导出；新增 `min_dte_for_open` 末日轮防护参数
- **PnL 统计修复**：胜率改为仅基于已执行 CLOSE 信号，OPEN+CLOSE 合并现金流计算往返净利润，消除 ETF 本金回收虚高问题

## 技术文档索引

- [docs/dde_tech_spec.md](docs/dde_tech_spec.md)：DDE 技术说明（DDEML / XlTable 格式 / topic 寻址原理）
- [docs/data_schema.md](docs/data_schema.md)：Parquet 数据结构（期权 / ETF / 快照 Schema + 利率曲线用法）
- [docs/vol_smile_math.md](docs/vol_smile_math.md)：波动率微笑算法（Black-76 / Brent IV / GUARD 机制）
