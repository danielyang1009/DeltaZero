# DeltaZero — 项目架构全览

```
D:\DeltaZero\
├── console.py                  ★ 主入口：FastAPI Web 控制台启动器（http://127.0.0.1:8787）
│
│── 【第1层】数据采集
│   data_bus/
│   ├── dde_direct_client.py    ★ DDE DDEML ctypes 实现，从本地交易软件（QD）拉取实时 tick
│   ├── bus.py                  ★ 数据记录主进程：消费 tick → Parquet 落盘 + ZMQ 广播
│   ├── parquet_writer.py         分片写入 / 日终合并 / snapshot_latest.parquet
│   └── zmq_publisher.py          ZMQ PUB，OPT_/ETF_ 前缀消息广播（tcp://127.0.0.1:5555）
│
│── 【第2层】合约 & 数据元信息
│   data_engine/
│   ├── contract_catalog.py     ★ ContractInfoManager：从 metadata/ 加载全量合约信息
│   ├── tick_data_loader.py       从 Parquet 文件加载历史 tick
│   ├── bar_data_loader.py        Bar 数据加载（K线聚合）
│   └── bond_termstructure_fetcher.py  国债收益率曲线数据抓取（CGb yield）
│
│   metadata/
│   ├── wind_sse_optionchain.xlsx   Wind 导出全量期权合约信息（合约元数据来源）
│   └── wxy_options.xlsx            DDE 路由表（service/topic 唯一来源）
│
│── 【第3层】计算 & 定价
│   calculators/
│   ├── vectorized_pricer.py    ★ VectorizedIVCalculator：Black-76 Brent 法批量求 IV
│   ├── iv_calculator.py          标量 IV 计算（HTTP 端点兼容）
│   ├── vix_engine.py             VIX 计算引擎（利率曲线 + 到期日加权）
│   └── yield_curve.py            BoundedCubicSplineRate 利率曲线拟合
│
│   core/
│   └── pricing.py                Black-Scholes 定价 + 完整 Greeks（Delta/Gamma/Vega/Theta/Rho）
│
│   models.py                   ★ 核心数据模型：TickData / ETFTickData / TickPacket dataclass
│
│── 【第4层】策略
│   strategies/
│   └── pcp_arbitrage.py        ★ PCP 套利扫描：TickAligner LKV + 净利润/Max_Qty/SPRD/OBI/Net_1T/TOL
│
│   risk/
│   └── margin.py                 上交所期权保证金计算（卖出开仓/维持保证金）
│
│── 【第5层】消费层（ZMQ SUB）
│   monitors/
│   ├── monitor.py              ★ Rich 终端 UI：订阅 ZMQ → PCP 套利信号实时刷新
│   └── common.py                 共享逻辑：合约加载、快照恢复、消息解析、Windows 编码修复
│
│   web/
│   ├── dashboard.py            ★ FastAPI 控制台 + API 端点 + WebSocket /ws/vol_smile
│   ├── market_cache.py         ★ market-cache-zmq（CONFLATE） + market-cache-compute（向量化IV）
│   ├── process_manager.py        子进程启停管理（DataBus / Monitor）
│   ├── data_stats.py             Parquet 数据统计接口
│   └── templates/
│       ├── index.html            主控台页面（进程管理 + 状态面板）
│       ├── dde.html              DDE 状态面板
│       ├── monitor.html          实时信号监控表格（WebSocket 驱动）
│       └── vol_smile.html        波动率微笑页面（WS 增量渲染 + IV 表格 + 告警）
│
│── 【第6层】回测 & 分析
│   backtest/
│   ├── engine.py               ★ 事件驱动回测引擎（含费率/滑点/保证金模拟）
│   ├── run.py                    编排层：加载数据 → 执行回测 → 输出报告
│   └── etf_price_simulator.py    ETF 价格仿真（回测用）
│
│   analysis/
│   └── pnl.py                    盈亏归因 + 绩效指标 + Greeks 归因（Matplotlib 图表）
│
│── 【工具 & 配置】
│   config/
│   └── settings.py             ★ 全局配置：UNDERLYINGS/端口/目录/费率/合并时间
│
│   utils/
│   └── time_utils.py             bj_now_naive() / 交易日判断 / 到期日计算
│
│   scripts/
│   ├── test_dde_connect.py       DDE 连接诊断脚本
│   ├── analyze_etf_parquet.py    ETF Parquet 数据分析
│   └── dump_50etf_vix_data.py    导出 50ETF VIX 历史数据
│
│   docs/
│   ├── dde_dataflow.md           DDE 数据流文档
│   └── dde_no_excel_research.md  DDE 无 xlsx 方案研究
│
│   sample_data/                  回测用样本数据（历史 Parquet）
└── requirements.txt
```

---

## 数据流向（实时链路）

```
DDE（QD服务）
  → dde_direct_client.py（ctypes DDEML pump）
  → Queue[TickPacket]
  → bus.py 主循环
      ├── ParquetWriter → D:\MARKET_DATA\chunks\ → 日终合并 → options_YYYYMMDD.parquet
      └── ZMQPublisher → tcp://127.0.0.1:5555
              ├── monitor.py（Rich 终端，PCP 套利）
              └── market_cache.py
                    ├── Thread-1: ZMQ SUB CONFLATE → _lkv（LKV快照）
                    └── Thread-2: 每100ms → VectorizedIVCalculator → asyncio Queue
                                    → _ws_broadcaster → WebSocket /ws/vol_smile
                                    → vol_smile.html 前端
```

---

## 功能状态

### ✅ 已实现

- DDE 实时数据采集（QD 服务，ctypes DDEML）
- Parquet 分片落盘 + 日终合并 + snapshot 冷启动恢复
- ZMQ 广播总线（OPT_ / ETF_ 前缀）
- PCP 套利实时扫描（Rich 终端 UI）
- 向量化 Black-76 IV 求解（GUARD-1/2/3 三重防护）
- Vol Smile WebSocket 实时推送
- VIX 计算（国债利率曲线插值）
- 保证金计算（上交所规则）
- 事件驱动历史回测引擎（含费率/滑点/保证金模拟）
- 盈亏归因 & Greeks 分析（Matplotlib 图表）
- Web 控制台（进程管理 + 状态监控）

### ⬜ 待完善 / 未接通

- `backtest/` 仅有 `sample_data`，尚无历史 Parquet 数据的完整接入流程
- `analysis/pnl.py` 的 Matplotlib 图表输出未集成进 Web 页面
- `risk/margin.py` 保证金计算未与 PCP 策略实时联动
- `core/pricing.py`（BS Greeks）与实时链路解耦，仅供离线/HTTP 调用
- Vol Smile 页面告警通知（声音/邮件）未实现
- 多账户/多策略管理层（暂无）
- 自动交易下单接口（暂无）
