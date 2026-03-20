# 波动率微笑算法

访问 `http://127.0.0.1:8787/vol_smile`，实时展示 50ETF / 300ETF / 500ETF 期权的隐含波动率微笑曲线与 IV 数据表格。

## 实时推送架构

```
ZMQ SUB（market-cache-zmq 线程）
      ↓ CONFLATE=1，只保最新消息
market_cache._lkv（内存 LKV 快照）
      ↓ 每 100ms 微批次
market_cache._compute_loop（market-cache-compute 线程）
      ↓ Brent 法 IV → loop.call_soon_threadsafe → asyncio.Queue
dashboard._ws_broadcaster（FastAPI 事件循环）
      ↓ WebSocket 推送
vol_smile.html：requestAnimationFrame 增量渲染
```

页面通过 WebSocket `/ws/vol_smile` 接收推送，断线 2s 自动重连，无需手动刷新。

## 核心算法：Black-76 + 隐含远期

为规避 A 股融券成本高昂及股息率难以估计的问题，弃用标准 Black-Scholes 的现货 $S$ 与股息率 $q$，改用**隐含远期 + Black-76** 框架。

### Step 1：倒算隐含远期价格 F

从同一行权价的认购、认沽中间价出发，利用 Put-Call Parity 反推：

$$F = K_{atm} + (C_{mid} - P_{mid}) \cdot e^{rT}$$

其中 $K_{atm}$ 为满足 $\arg\min |C_{mid} - P_{mid}|$ 的行权价（市场隐含平值点）。

### Step 2：Brent 法求解 IV

`calculators/vectorized_pricer.py` 的 `VectorizedIVCalculator` 对每个合约用 **Brent 法**（`scipy.optimize.brentq`，区间 `[1e-4, 5.0]`，`xtol=1e-6`）求解 IV：

$$\sigma^* = \mathop{\text{RootFind}}_{\sigma \in [10^{-4},\, 5.0]} \bigl( \text{Black76}(F,K,T,r,\sigma) - \text{Price}_{mid} = 0 \bigr)$$

Brent 法要求区间端点异号；端点同号（深度虚值、价格违反无套利边界）直接输出 `nan`，绝无发散风险。原 Newton-Raphson 在 Vega 极小时步长越界导致的 `nan` 问题已彻底消除。

## 三条 GUARD 机制

| 保护 | 位置 | 机制 |
|------|------|------|
| **[GUARD-3]** 微观流动性防线 | `market_cache.py`，Brent 上游 | mid < 10 Tick（0.001 元）或价差 > max(20 Tick, mid×30%) → mid/bid/ask 三路同步置 `nan` |
| **[GUARD-1]** 无套利边界过滤 | `vectorized_pricer.py` | price≤0 / not finite / K≤0 / price < intrinsic−1e-4 → 直接 `nan`，跳过 brentq |
| **[GUARD-2]** T 精度 | `vectorized_pricer.py` | `time.time()` 毫秒 Unix 时间戳，`calc_T()` 返回 `max(T, 1e-6)`，防止 T≤0 |

## 流动性拼接：主力 IV 曲线

`market_cache._compute_loop` 在求解完 Call/Put IV 后，对每个行权价按流动性择优拼接，生成**主力 IV 曲线**（前端蓝色粗线）：

| 行权价区间 | 选用来源 | 原因 |
|------------|----------|------|
| K < F × 0.995 | Put IV | Call 深度实值（脏），Put 虚值（干净） |
| K > F × 1.005 | Call IV | Put 深度实值（脏），Call 虚值（干净） |
| 平值附近 | 价差较小的一侧；两侧相等时取均值（标注 AVG） | 按盘口紧凑度择优 |

## HTTP 兜底路径

`/api/vol_smile` HTTP 端点调用 `calc_iv_black76()`（`calculators/iv_calculator.py`），同样使用 Brent 法求解单合约 IV。

## IV 数据表格列说明

| 列 | 说明 |
|----|------|
| Call IV / Put IV | 中间价对应 IV |
| Call/Put Bid/Ask IV | 买卖价对应 IV |
| 主力 IV | 流动性拼接后的 IV，括号内标注来源（C/P/AVG） |
| IV Skew (C−P) | 同行权价 Call IV 减 Put IV |
| PCP 偏差 | `C_mid + K·disc − P_mid − F·disc`（偏离 0 表示 PCP 套利机会） |

行级告警：超过 PCP 阈值（默认 0.003）黄色高亮，超过 Skew 阈值（默认 0.02）红色高亮，ATM 行蓝色高亮。

## 实现文件

| 文件 | 说明 |
|------|------|
| `calculators/iv_calculator.py` | `calc_implied_forward()` + `black76_price()` + `calc_iv_black76()`（Brent 法，HTTP 兜底） |
| `calculators/vectorized_pricer.py` | `VectorizedIVCalculator`（Brent 法 IV + Greeks，GUARD-1/2） |
| `web/market_cache.py` | ZMQ SUB 线程 + compute 线程 + `get_rich_snapshot()` |
| `web/dashboard.py` | `/ws/vol_smile` WS endpoint + `_ws_broadcaster` + `/api/vol_smile` HTTP 端点 |
| `web/templates/vol_smile.html` | WS 客户端 + rAF 增量渲染 + IV 表格 + 阈值告警 |

## 无风险利率

优先从当日中债国债收益率曲线（`cgb_yieldcurve_YYYYMMDD.csv`）按实际剩余期限取值，7 日内无文件则回退固定 2%。
