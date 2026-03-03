# 项目状态交接文档 (STATE.md)

> 最后更新：2026-03-03  
> 用于新对话快速恢复上下文，请将本文件内容粘贴给新的 AI 会话。

---

## 一、项目概述

**项目名称**：中国 ETF 期权 PCP 套利回测与交易预警框架  
**项目路径**：`d:\Option_Arbitrage_Engine`  
**开发语言**：Python 3.10+  
**当前版本**：v0.2（实盘监控已上线，回测框架完整，ETF 真实数据待接入）

**核心功能**：
1. 实盘监控（Wind API）：检测 Put-Call Parity 套利机会，输出终端彩色警报，手动下单
2. 历史回测：Tick-by-Tick 精确回测，含盈亏分析和 Greeks 归因

---

## 二、目录结构

```
d:\Option_Arbitrage_Engine\
│
├── monitor_live.py          ★ 实盘监控主程序（终端 Rich 彩色表格）
├── monitor_live.ipynb       ★ 实盘监控 Jupyter 版（HTML 刷新）
├── main.py                    历史回测入口（兼含旧版监控骨架）
├── models.py                  全局数据模型（TickData/ContractInfo/TradeSignal 等）
├── requirements.txt           依赖：pandas/numpy/scipy/tabulate/matplotlib/rich
├── README.md                  完整使用文档（架构/模块/方法/配置）
├── STATE.md                   本文件（AI 上下文快速恢复用）
│
├── config/
│   └── settings.py            全局配置（费率/滑点/保证金/数据路径）
│
├── data_engine/
│   ├── tick_loader.py         CSV Tick 加载器（向量化，支持日期过滤）
│   ├── contract_info.py       合约信息管理（CSV加载 + .SH/.XSHG 标准化）
│   ├── wind_adapter.py        Wind API 适配器（wsq/wsd，Mock降级）
│   └── etf_simulator.py       标的 ETF 价格模拟器（GBM + PCP隐含锚点）
│
├── core/
│   └── pricing.py             Black-Scholes 定价 + Newton-Raphson IV 求解
│
├── strategies/
│   └── pcp_arbitrage.py       PCP 套利策略 + TickAligner 时间对齐器
│
├── risk/
│   └── margin.py              上交所卖方保证金计算
│
├── backtest/
│   └── engine.py              Tick-by-Tick 回测引擎 + Account 账户管理
│
├── analysis/
│   └── pnl.py                 P&L/回撤/Sharpe/Greeks 归因 + matplotlib 图表
│
├── info_data/
│   ├── 上交所期权基本信息.csv  11,102 条合约记录（行权价/类型/到期日）
│   └── etf_option_info.md     品种上市时间参考
│
└── sample_data/               小样本数据（用于快速功能验证）
    ├── 华夏上证50ETF期权/
    ├── 华泰柏瑞沪深300ETF期权/
    └── 南方中证500ETF期权/
```

---

## 三、数据资产

### 3.1 完整 Tick 数据（不在仓库中，本地路径）

```
D:\TICK_DATA\上交所\
├── 华夏上证50ETF期权/      129 个月度CSV（2015-02 ~ 2025-10）
├── 华泰柏瑞沪深300ETF期权/  73 个月度CSV（2019-12 ~ 2025-12）
├── 南方中证500ETF期权/      40 个月度CSV（2022-09 ~ 2025-12）
├── 科创50期权/              31 个月度CSV（2023-06 ~ 2025-12）
└── 科创板50期权/            31 个月度CSV（2023-06 ~ 2025-12）
```

**文件命名规律**：`{品种名}_option_ticks_{YYYY-MM}.csv`

### 3.2 Tick 数据 Schema

| 字段 | 类型 | 说明 |
|------|------|------|
| `time` | int64 | 时间戳，格式 `YYYYMMDDHHMMSSmmm`（17位，毫秒精度）|
| `current` | float | 最新价 |
| `volume` | int | 累计成交量 |
| `high` / `low` | float | 最高/最低价 |
| `money` | float | 成交额 |
| `position` | int | 持仓量 |
| `a1_p` ~ `a5_p` | float | 卖1~卖5价（50ETF有5档，300/500ETF仅1档）|
| `b1_p` ~ `b5_p` | float | 买1~买5价 |
| `contract_code` | str | 合约代码，后缀 `.XSHG`（加载时自动转为 `.SH`）|

### 3.3 合约信息文件

**路径**：`info_data/上交所期权基本信息.csv`  
**编码**：UTF-8-BOM  
**字段**：`证券代码,证券简称,起始交易日期,最后交易日期,交割月份,行权价格,期权类型`  
**记录数**：11,102 条（认购 5,551 + 认沽 5,551）  
**代码后缀**：`.SH`（与 Tick 数据的 `.XSHG` 不同，框架已自动处理）

---

## 四、使用方法

### 安装依赖
```bash
pip install -r requirements.txt
```

### 实盘监控模式（需要 Wind 终端）

```bash
# 推荐生产参数
python monitor_live.py --min-profit 150 --expiry-days 45

# 调试/查看参数
python monitor_live.py --min-profit 30   # 低阈值看更多信号
python monitor_live.py --expiry-days 30  # 只看近月合约
python monitor_live.py --refresh 3       # 每3秒刷新
python monitor_live.py --atm-range 0.10  # 只看 ±10% 行权价

# Jupyter Notebook 版：打开 monitor_live.ipynb，Run All，按 ■ 停止
```

### 历史回测模式

```bash
# 单月回测（推荐从这里开始，耗时约 20 秒）
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" --start-date 2024-01 --end-date 2024-01

# 季度回测 + 图表
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" --start-date 2024-01 --end-date 2024-03 --output-chart equity.png

# 自定义资金和利润阈值
python main.py --data-dir "D:\TICK_DATA\上交所\华夏上证50ETF期权" --start-date 2024-01 --end-date 2024-01 --capital 2000000 --min-profit 200
```

### 命令行参数（main.py 回测）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | `backtest` | `backtest` 或 `monitor`（旧版骨架）|
| `--data-dir` | `sample_data` | Tick 数据目录 |
| `--start-date` | 最早 | 回测起始月份，格式 `YYYY-MM` |
| `--end-date` | 最新 | 回测结束月份，格式 `YYYY-MM` |
| `--capital` | 1,000,000 | 初始资金（元）|
| `--min-profit` | 50 | 最小利润阈值（元/组）|
| `--output-chart` | 无 | 权益曲线图保存路径 |
| `--verbose` | False | 输出详细日志 |

---

## 五、已验证的功能

| 模块 | 状态 | 备注 |
|------|------|------|
| 合约信息加载 | ✅ | 11,102 条正常解析，.SH/.XSHG 互转正常 |
| Tick 数据加载 | ✅ | 向量化，104,511 条/1.3秒；自动识别1档/5档盘口 |
| 时间戳解析 | ✅ | 精确到毫秒，支持17位整型和科学计数法 |
| 日期范围过滤 | ✅ | `--start-date` / `--end-date` 按文件名过滤 |
| Black-Scholes 定价 | ✅ | ATM Call=0.1270，PCP等价关系精确验证 |
| IV 求解（Newton-Raphson）| ✅ | 从BS价格反推，收敛至 σ=0.200000 |
| PCP 套利信号扫描 | ✅ | 正向/反向套利，含费用和滑点估算 |
| 保证金计算 | ✅ | 上交所卖方公式，支持认购/认沽 |
| 回测引擎 | ✅ | Tick-by-Tick 撮合，T+0/T+1 约束，资金管理 |
| 盈亏分析 | ✅ | P&L/回撤/Sharpe/胜率，权益曲线图 |
| Wind 适配器 | ✅ | 无 Wind 时自动 Mock 降级 |
| 实盘 Wind 监控 | ✅ | monitor_live.py 上线，彩色表格实时刷新 |
| 终端编码修复 | ✅ | ctypes SetConsoleOutputCP(65001)，PowerShell/Cursor 均正常 |

---

## 六、当前已知问题与待办

### ✅ 已完成（2026-03-03）

- **实盘 Wind 监控上线**
  - `monitor_live.py`：终端彩色 Rich 表格，每 N 秒刷新套利信号
  - `monitor_live.ipynb`：Jupyter 版，HTML 样式刷新
  - 调试过程解决的关键问题：
    - WindPy x64 安装路径配置（`D:\veighna_studio\Lib\site-packages\WindPy.pth`）
    - Wind wsq 字段权限：`rt_ask_vol1/rt_bid_vol1` 需 Level 2，只用 `rt_last,rt_ask1,rt_bid1`
    - wsq 数据量限制：单次 ≤600 数据点（194代码×3字段=582点可行）
    - `TickAligner` 多品种 Bug：原先只存一个 `latest_etf_quote`，修复为按 `etf_code` 字典存储

- **终端 GBK/UTF-8 编码修复**
  - 用 `ctypes.windll.kernel32.SetConsoleOutputCP(65001)` 改变当前进程代码页
  - `sys.stdout.reconfigure(encoding='utf-8')` 同步 Python 流
  - `Console(legacy_windows=False)` 禁用旧式 Windows Console API

### 🔴 高优先级

1. **ETF 模拟数据失真**（回测用，不影响实盘监控）
   - **现象**：用 PCP 反推的隐含 ETF 价格作为模拟锚点，期权上市初期大量错误定价导致回测严重亏损
   - **解决方案**：在 `ETFSimulator` 中增加"真实 ETF 数据加载通道"，同目录有 ETF Tick 数据时自动使用，否则降级为模拟
   - **ETF 代码与路径映射**：510050.SH → 50ETF，510300.SH → 300ETF，510500.SH → 500ETF

2. **回测引擎重复信号问题**
   - **现象**：同一 Tick 时刻扫描了重复信号（日志中相同时间戳、相同 Strike 的多条信号）
   - **解决方案**：在 `PCPArbitrage.scan_opportunities()` 中加入信号去重，同一合约对同一时间点只保留利润最高的一条

### 🟡 中优先级

3. **月度/批量回测脚本**：当前需手动指定日期范围，建议增加 `--batch-by-month` 模式
4. **Greeks 归因完善**：当前 `calc_greeks_attribution()` 是骨架实现（固定比例拆分），需逐 Tick 计算
5. **回测结果持久化**：目前只打印控制台，建议保存 CSV 或 JSON

### 🟢 低优先级

6. 实盘监控信号质量优化：添加最大盘口价差过滤（深度虚值期权 bid-ask spread 过宽导致虚假高利润信号）
7. 声音/弹窗警报功能（当前只有控制台/Jupyter 输出）
8. 多品种同时回测

---

## 七、关键设计决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| 代码后缀标准 | 统一使用 `.SH` | Tick 数据用 `.XSHG`，CSV 用 `.SH`，框架内统一转换 |
| 时间戳解析 | 向量化整型运算 | 17位整型可直接 int64 整除提取时间分量，比逐行 Decimal 快20x |
| ETF 价格模拟 | PCP 隐含锚点 + GBM 插值 | 比纯随机 GBM 更与期权市场价格一致 |
| 现货做空 | 仅记录，不执行 | A股 ETF T+1 限制，反向套利实际不可执行 |
| 保证金比例 | 认购/认沽各 12%/7% | 上交所标准参数，可在 `config/settings.py` 覆盖 |
| Wind wsq 字段 | 只用3字段（last/ask1/bid1）| 194合约×3=582点 < 600点上限；Level 2 字段无权限 |
| 挂单量默认值 | 硬编码 100 | Wind 权限限制无法获取真实值，置信度评分仅供参考 |
| 终端编码 | ctypes SetConsoleOutputCP | os.system('chcp') 只改子进程，ctypes 改当前进程 |

---

## 八、开发环境

```
OS: Windows 10/11
Python: 3.10+（使用 D:\veighna_studio 环境）
关键依赖版本（实测可用）:
  pandas >= 2.0
  numpy >= 1.24
  scipy >= 1.10
  tabulate >= 0.9
  matplotlib >= 3.7
  rich >= 13.0
可选: WindPy（需要 Wind 金融终端授权，x64 版本）
WindPy 路径: C:\Wind\Wind.NET.Client\WindNET\x64\
```

---

## 九、继续开发建议（给新对话的提示词）

```
项目在 d:\Option_Arbitrage_Engine，是一个中国ETF期权PCP套利框架。
请先读取 STATE.md 了解全貌，再读取相关源码文件后开始修改。
完整文档参见 README.md。

当前最重要的任务：[在此填写具体需求，例如：]
- "在 data_engine/etf_simulator.py 中增加真实 ETF Tick 数据加载功能"
- "修复 strategies/pcp_arbitrage.py 中的重复信号问题"
- "增加逐月批量回测功能"
```
