# 中国 ETF 期权 PCP 套利框架

> **Put-Call Parity Arbitrage Engine for Chinese ETF Options**  
> 实盘信号监控 + Tick 级历史回测 | Python 3.10+ | Wind API

---

## 目录

- [项目概述](#一项目概述)
- [目录结构](#二目录结构)
- [核心模块说明](#三核心模块说明)
- [模块交互流程](#四模块交互流程)
- [套利计算方法](#五套利计算方法)
- [快速开始](#六快速开始)
- [实盘监控使用指南](#七实盘监控使用指南)
- [回测使用指南](#八回测使用指南)
- [配置说明](#九配置说明)
- [数据说明](#十数据说明)
- [已知限制](#十一已知限制)

---

## 一、项目概述

本框架专为**中国上交所 ETF 期权**的 Put-Call Parity（认沽认购平价）套利设计，提供两个核心功能：

| 功能 | 工具 | 描述 |
|------|------|------|
| **实盘监控** | `monitor_live.py` / `monitor_live.ipynb` | 连接 Wind API，实时扫描套利机会，富文本表格输出，供人工下单参考 |
| **历史回测** | `main.py --mode backtest` | 加载本地历史 Tick 数据，Tick 级精确回测，输出 P&L、Sharpe、权益曲线 |

**支持品种**（上交所）：

| 品种 | ETF 代码 | 期权代码前缀 |
|------|----------|------------|
| 50ETF 期权 | 510050.SH | 10XXXX |
| 300ETF 期权（华泰） | 510300.SH | 10XXXX |
| 500ETF 期权（南方） | 510500.SH | 10XXXX |
| 科创 50 期权 | 588000.SH | 10XXXX |
| 科创板 50 期权 | 588050.SH | 10XXXX |

---

## 二、目录结构

```
d:\Option_Arbitrage_Engine\
│
├── monitor_live.py          ★ 实盘监控主程序（终端彩色表格，Rich）
├── monitor_live.ipynb       ★ 实盘监控 Jupyter 版（HTML 刷新）
├── main.py                    历史回测入口（也含旧版监控骨架）
├── models.py                  全局数据模型定义
├── requirements.txt           依赖列表
├── STATE.md                   项目状态交接文档
├── README.md                  本文件
│
├── config/
│   └── settings.py            全局配置（费率/滑点/保证金/阈值）
│
├── data_engine/
│   ├── tick_loader.py         CSV Tick 加载器（向量化，支持日期过滤）
│   ├── contract_info.py       合约信息管理（.SH/.XSHG 标准化，Call/Put 配对）
│   ├── wind_adapter.py        Wind API 适配器（wsq 订阅，无 Wind 时 Mock 降级）
│   └── etf_simulator.py       ETF 价格模拟器（回测用，GBM + PCP 隐含锚点）
│
├── core/
│   └── pricing.py             Black-Scholes 定价 + Newton-Raphson 隐含波动率求解
│
├── strategies/
│   └── pcp_arbitrage.py       PCP 套利策略 + TickAligner 多合约时间对齐器
│
├── risk/
│   └── margin.py              上交所卖方保证金计算（认购/认沽公式）
│
├── backtest/
│   └── engine.py              Tick-by-Tick 回测引擎 + Account 账户管理
│
├── analysis/
│   └── pnl.py                 P&L 分析：回撤/Sharpe/胜率/Greeks 归因/权益曲线图
│
├── info_data/
│   ├── 上交所期权基本信息.csv  11,102 条合约记录（行权价/类型/到期日）
│   └── etf_option_info.md     品种上市时间参考
│
└── sample_data/               小样本数据（快速功能验证）
    ├── 华夏上证50ETF期权/
    ├── 华泰柏瑞沪深300ETF期权/
    └── 南方中证500ETF期权/
```

---

## 三、核心模块说明

### 3.1 `models.py` — 全局数据模型

所有模块共享的数据结构，无外部依赖。

| 类 | 说明 |
|----|------|
| `TickData` | 期权 Tick 快照：最新价、买卖1～5档价格与数量、时间戳 |
| `ETFTickData` | ETF Tick 快照：etf_code、最新价、买一/卖一 |
| `ContractInfo` | 合约静态信息：代码、行权价、到期日、类型、标的代码 |
| `TradeSignal` | 套利信号：方向（正向/反向）、Call/Put 代码、净利润估算、置信度 |
| `normalize_code()` | `.XSHG` ↔ `.SH` 代码标准化工具函数 |

### 3.2 `config/settings.py` — 全局配置

```python
TradingConfig
├── FeeConfig        # 期权手续费 1.7元/张、ETF 佣金万0.6
├── SlippageConfig   # 期权滑点 1 跳(0.0001)、ETF 滑点 1 跳(0.001)
├── MarginConfig     # 认购/认沽保证金比例 12%/7%（上交所标准）
└── 信号过滤         # min_profit_threshold = 50元/组（默认）
```

### 3.3 `data_engine/contract_info.py` — 合约信息管理

- 从 CSV 加载 11,102 条合约记录，自动处理 UTF-8-BOM 编码
- `find_call_put_pairs(underlying, expiry)` — 匹配同行权价的 Call/Put 对
- `get_active_contracts(days)` — 过滤 N 天内到期的活跃合约
- 自动处理 `.SH` / `.XSHG` 代码互转

### 3.4 `data_engine/tick_loader.py` — 历史 Tick 加载器

- 向量化 pandas 解析，10万条约 1.3 秒
- 时间戳解析：支持 17 位整型（`YYYYMMDDHHMMSSmmm`）和科学计数法格式
- 按文件名自动过滤日期范围（`--start-date` / `--end-date`）
- 自动识别 1 档 / 5 档盘口格式

### 3.5 `data_engine/wind_adapter.py` — Wind API 适配器

- 封装 `WindPy.wsq` 实时行情订阅
- 无 Wind 终端时自动降级为 Mock 模式（不抛异常）
- 注意：需要 WindPy x64 版本（`C:\Wind\Wind.NET.Client\WindNET\x64`）

### 3.6 `strategies/pcp_arbitrage.py` — 核心策略

包含两个类：

**`TickAligner`**：多合约报价快照管理器
- 维护每个期权合约的最新 Tick（Last-Known-Value 机制）
- 按 `etf_code` 分品种存储 ETF 行情，避免多品种互相覆盖

**`PCPArbitrage`**：PCP 套利信号生成器
- `on_option_tick()` / `on_etf_tick()`：接收行情更新
- `scan_opportunities(pairs)`：遍历所有 Call/Put 对，计算 PCP 偏离，返回信号列表
- `_estimate_costs()`：估算每组交易成本（手续费 + 佣金 + 滑点）

### 3.7 `core/pricing.py` — 期权定价

- Black-Scholes 公式（欧式期权，适用于 ETF 期权）
- Newton-Raphson 隐含波动率（IV）求解
- 用于回测中的 Greeks 计算（Delta/Gamma/Theta/Vega）

### 3.8 `backtest/engine.py` — 回测引擎

- `MergedTick`：将期权 Tick 流与 ETF Tick 流合并为统一时间线
- `Account`：账户管理，持仓记录，保证金检查，T+1 约束
- 撮合逻辑：使用 bid/ask 价格而非 last 价，更真实反映成交成本
- 返回 `trade_history`、`signals`、`equity_curve` 字典

### 3.9 `risk/margin.py` — 保证金计算

上交所卖方保证金公式：
```
卖出认购 = 权利金 + max(12% × 标的价格 - 虚值额, 7% × 标的价格)
卖出认沽 = 权利金 + max(12% × 标的价格 - 虚值额, 7% × 行权价格)
```

### 3.10 `analysis/pnl.py` — 绩效分析

- 总收益、年化收益、最大回撤、Sharpe 比率、胜率
- Greeks 归因（Delta/Gamma/Theta/Vega PnL 拆分，当前为骨架实现）
- `plot_equity_curve()` — matplotlib 权益曲线图

### 3.11 `monitor_live.py` — 实盘监控主程序

- Windows 终端 UTF-8 编码修复（`ctypes.SetConsoleOutputCP(65001)`）
- 使用 `rich.Live` 实现动态刷新彩色表格
- Wind API 订阅：每次轮询 `cancelRequest(0)` 清除旧订阅再重新请求
- ATM 过滤：`--atm-range` 参数控制行权价偏离范围，过滤深度虚值
- 分批请求：单次 wsq ≤600 数据点限制（194合约 × 3字段 = 582点）

---

## 四、模块交互流程

### 实盘监控流程

```
monitor_live.py
    │
    ├── ContractInfoManager.load_from_csv()
    │       └── info_data/上交所期权基本信息.csv
    │
    ├── WindPy.wsq(etf_codes + option_codes, "rt_last,rt_ask1,rt_bid1")
    │       └── 每 N 秒轮询一次快照（cancelRequest → wsq → 解析）
    │
    ├── ETFTickData / TickData → TickAligner.update_etf() / update_option()
    │
    ├── PCPArbitrage.scan_opportunities(call_put_pairs)
    │       ├── _evaluate_pair() × N 对
    │       │       ├── 计算 theoretical_spread = S - K·e^{-rT}
    │       │       ├── 计算 forward_profit（正向套利）
    │       │       ├── 计算 reverse_profit（反向套利，A股通常不可执行）
    │       │       └── _estimate_costs()（手续费 + 滑点）
    │       └── 按利润降序排列信号
    │
    └── rich.Live → build_display(signals) → 终端彩色表格刷新
```

### 历史回测流程

```
main.py --mode backtest
    │
    ├── ContractInfoManager.load_from_csv()
    ├── TickLoader.load_directory()           ← 本地 CSV Tick 数据
    ├── ETFSimulator.simulate_from_option_ticks()  ← GBM 模拟 ETF（回测专用）
    │
    ├── BacktestEngine.run()
    │       ├── 合并期权 + ETF Tick 为统一时间线
    │       ├── 逐 Tick 调用 strategy_callback()
    │       │       └── PCPArbitrage.scan_opportunities()
    │       ├── 信号触发 → Account 开仓 / 平仓
    │       └── 记录 trade_history + equity_curve
    │
    └── PnLAnalyzer.analyze() → print_report() + plot_equity_curve()
```

---

## 五、套利计算方法

### Put-Call Parity 公式

```
理论：C - P = S - K·e^{-rT}

正向套利（Conversion）—— 仅此方向在A股可执行：
  条件：C_bid - P_ask > S_ask - K·e^{-rT} + 成本
  操作：卖出 Call + 买入 Put + 买入 ETF
  利润：(C_bid - P_ask - (S_ask - K·e^{-rT}) - 成本) × 10000

反向套利（Reversal）—— A股现货T+1限制，通常无法执行：
  条件：P_bid - C_ask > K·e^{-rT} - S_bid + 成本
  操作：买入 Call + 卖出 Put + 卖出 ETF（受限）
```

### 成本构成（每组，合约单位 10000 份）

| 成本项 | 计算方式 | 约合（3元ETF）|
|--------|---------|--------------|
| 期权手续费 | 1.7元/张 × 2张 | 3.4 元 |
| ETF 佣金 | 标的价×10000×万0.6 | ~18 元 |
| 期权滑点 | 1跳×0.0001×2腿×10000 | 2 元 |
| ETF 滑点 | 1跳×0.001×10000 | 10 元 |
| **合计** | | **≈ 33 元/组** |

### 净利润字段含义

`net_profit_estimate`（单位：元/组）= PCP 偏差收益 × 10000 - 上述成本

**操作建议阈值**：

| 净利润显示值 | 操作建议 |
|-------------|---------|
| < 100 元 | 滑点风险高，谨慎 |
| 100 ~ 200 元 | 可考虑，需确认盘口价差不过宽 |
| > 200 元 | 信号较强，优先操作 |

---

## 六、快速开始

### 安装依赖

```bash
# 标准依赖（回测 + 分析）
pip install -r requirements.txt

# 实盘监控额外依赖
pip install rich>=13.0

# WindPy（需要 Wind 金融终端）
# 见下方 WindPy 安装说明
```

### WindPy 安装（x64 Python 环境）

```
1. 确认 Wind 终端已安装并登录
2. 找到 x64 版 WindPy：C:\Wind\Wind.NET.Client\WindNET\x64\
3. 在 Python 的 site-packages 目录创建 WindPy.pth，内容：
   C:\Wind\Wind.NET.Client\WindNET\x64
4. 将 x64 目录下的 WindPy.py 复制到 site-packages
5. 测试：python -c "from WindPy import w; print('OK')"
```

---

## 七、实盘监控使用指南

```bash
# 默认参数启动（显示净利润 ≥30 元的机会，90天内到期，每5秒刷新）
python monitor_live.py

# 推荐生产参数（过滤噪音）
python monitor_live.py --min-profit 150 --expiry-days 45

# 参数说明
python monitor_live.py --min-profit 100   # 最小净利润阈值（元/组）
python monitor_live.py --expiry-days 30   # 只看30天内到期合约
python monitor_live.py --refresh 3        # 每3秒刷新
python monitor_live.py --atm-range 0.10  # 只看 ±10% 行权价（过滤深度虚值）

# Jupyter 版（适合记录历史信号）
# 打开 monitor_live.ipynb → Run All → 按 ■ 停止
```

### 输出表格列说明

| 列名 | 含义 | 操作参考 |
|------|------|---------|
| 方向 | 正向 / 反向 | **只操作"正向"** |
| 品种 | ETF 名称 | 50ETF / 300ETF / 500ETF / 科创50 |
| 行权价 | Strike | — |
| 到期 | 到期日 | 临近到期流动性可能变差 |
| Call卖/Put买 | 实时盘口价 | 下单参考价 |
| ETF买入 | 实时 Ask | 下单参考价 |
| **PCP偏差** | 实际价差 - 理论价差 | 正数 = Call 被高估 |
| **净利润(元)** | 扣除手续费和滑点后 | **核心决策指标** |
| 置信度 | 0~1 综合评分 | ≥0.5 信号更可靠（注：挂单量数据受权限限制，仅供参考） |

### 正向套利三腿下单顺序建议

```
① 先挂 Call 腿卖出（流动性最差，先挂）
② 再挂 Put 腿买入
③ 最后市价买入 ETF（流动性最好）
注意：三腿无法原子执行，存在腿差风险
```

---

## 八、回测使用指南

```bash
# 单月回测（推荐入门）
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" \
               --start-date 2024-01 --end-date 2024-01

# 半年回测 + 图表输出
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" \
               --start-date 2024-01 --end-date 2024-06 \
               --output-chart equity.png

# 调高利润阈值（减少噪音信号）
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" \
               --start-date 2024-01 --end-date 2024-03 \
               --min-profit 200 --capital 2000000
```

### 回测输出指标说明

| 指标 | 说明 |
|------|------|
| 总收益率 | 期末权益 / 初始资金 - 1 |
| 年化收益率 | 按实际交易天数折算 |
| 最大回撤 | 权益曲线峰值到谷底的最大跌幅 |
| Sharpe 比率 | (年化收益 - 无风险利率) / 年化波动率 |
| 胜率 | 盈利交易笔数 / 总交易笔数 |
| 平均持仓时间 | 从开仓到平仓的平均 Tick 数 |

> ⚠️ **注意**：回测中 ETF 价格使用 GBM 模拟（非真实数据），回测结果仅供参考，实际交易表现可能有较大偏差。

---

## 九、配置说明

所有参数集中在 `config/settings.py`，无需修改源码：

```python
# 调整费率（适配你的券商）
config = get_default_config()
config.fee.option_commission_per_contract = 2.0   # 元/张
config.fee.etf_commission_rate = 0.00003          # 万0.3

# 调整滑点（深度虚值期权建议加大）
config.slippage.option_slippage_ticks = 2         # 2跳滑点

# 调整无风险利率
config.risk_free_rate = 0.015                     # 1.5%

# 调整信号阈值
config.min_profit_threshold = 150.0               # 只看≥150元的机会
```

---

## 十、数据说明

### 本地 Tick 数据（不在仓库中）

```
D:\TICK_DATA\上交所\
├── 华夏上证50ETF期权/      129个月度CSV（2015-02 ~ 2025-10）
├── 华泰柏瑞沪深300ETF期权/  73个月度CSV（2019-12 ~ 2025-12）
├── 南方中证500ETF期权/      40个月度CSV（2022-09 ~ 2025-12）
├── 科创50期权/              31个月度CSV（2023-06 ~ 2025-12）
└── 科创板50期权/            31个月度CSV（2023-06 ~ 2025-12）
```

**文件命名格式**：`{品种名}_option_ticks_{YYYY-MM}.csv`

### Tick 数据字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `time` | int64 | 时间戳，17位整型 `YYYYMMDDHHMMSSmmm` |
| `current` | float | 最新价 |
| `a1_p` ~ `a5_p` | float | 卖1~卖5价（50ETF有5档，其余1档）|
| `b1_p` ~ `b5_p` | float | 买1~买5价 |
| `contract_code` | str | 合约代码（`.XSHG` 后缀，框架自动转 `.SH`）|

---

## 十一、已知限制

| 限制 | 说明 | 影响范围 |
|------|------|---------|
| ETF 数据为模拟 | 回测中用 GBM 模拟 ETF 价格，非真实数据 | 回测结果失真 |
| Wind Level 2 权限 | 挂单量字段需要 Level 2，当前默认 100 | 置信度评分不准确 |
| Wind wsq 数据点限制 | 单次 ≤600 数据点（194合约×3字段=582点） | 超大品种需分批 |
| 三腿非原子执行 | A股无组合指令，存在腿差风险 | 实盘净利润可能低于预估 |
| 反向套利不可执行 | ETF 现货 T+1，无法做空 | 反向信号仅供参考 |
| 回测重复信号 | 同 Tick 时刻可能出现重复信号（待修复）| 回测统计偏高 |
