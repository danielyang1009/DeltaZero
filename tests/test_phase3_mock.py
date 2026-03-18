# -*- coding: utf-8 -*-
"""
Phase 3 端到端冒烟测试

链路：TickAligner.update_tick() → MarketSnapshot
    → PCPArbitrageStrategy.scan_pairs_for_display()
    → ArbitrageSignal

直接运行：python tests/test_phase3_mock.py
"""
import math
import sys
from pathlib import Path
from datetime import date, datetime

# 确保项目根目录在 sys.path（直接运行脚本时 tests/ 不在根目录）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows GBK 终端显示修复（不影响断言逻辑）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from models.data import ContractInfo, ETFTickData, OptionType, TickData
from data_engine.tick_aligner import TickAligner
from strategies.pcp_arbitrage import PCPArbitrageStrategy
from config.settings import TradingConfig

# ── Mock 合约元信息 ──────────────────────────────────────
# 行权价 K=3.000，净利润公式：
#   (K - S_ask - P_ask + C_bid) × 10000 - S_ask×10000×0.0002 - 3.0
#   = (3.000 - 2.995 - 0.060 + 0.100) × 10000 - 5.99 - 3.0 ≈ 441 元
EXPIRY    = date(2026, 6, 25)
CALL_CODE = "10004786.SH"
PUT_CODE  = "10004787.SH"
ETF_CODE  = "510050.SH"
STRIKE    = 3.000


def _make_contract(code: str, opt_type: OptionType) -> ContractInfo:
    return ContractInfo(
        contract_code=code,
        short_name=f"50ETF{'购' if opt_type == OptionType.CALL else '沽'}Mock",
        underlying_code=ETF_CODE,
        option_type=opt_type,
        strike_price=STRIKE,
        list_date=date(2026, 1, 1),
        expiry_date=EXPIRY,
        delivery_month="202606",
        contract_unit=10000,
    )


call_info = _make_contract(CALL_CODE, OptionType.CALL)
put_info  = _make_contract(PUT_CODE,  OptionType.PUT)

# ── Mock Tick 行情 ───────────────────────────────────────
now = datetime.now()

call_tick = TickData(
    timestamp=now, contract_code=CALL_CODE,
    current=0.102, volume=1000, high=0.110, low=0.095, money=102.0, position=5000,
    bid_prices=[0.100, math.nan, math.nan, math.nan, math.nan],  # 买一=0.100（卖出 Call）
    ask_prices=[0.105, math.nan, math.nan, math.nan, math.nan],
    bid_volumes=[200, 0, 0, 0, 0],
    ask_volumes=[150, 0, 0, 0, 0],
)
put_tick = TickData(
    timestamp=now, contract_code=PUT_CODE,
    current=0.058, volume=800, high=0.065, low=0.050, money=46.4, position=3000,
    bid_prices=[0.055, math.nan, math.nan, math.nan, math.nan],
    ask_prices=[0.060, math.nan, math.nan, math.nan, math.nan],  # 卖一=0.060（买入 Put）
    bid_volumes=[180, 0, 0, 0, 0],
    ask_volumes=[120, 0, 0, 0, 0],
)
etf_tick = ETFTickData(
    timestamp=now, etf_code=ETF_CODE, price=2.995,
    ask_price=2.995, bid_price=2.994,    # 卖一=2.995（买入 ETF）
    ask_volume=500, bid_volume=600,       # 500手=5万份 → 5张合约
)

# ── 喂入 TickAligner → 获取 MarketSnapshot ───────────────
aligner = TickAligner()
aligner.update_tick(call_tick)
aligner.update_tick(put_tick)
snapshot = aligner.update_tick(etf_tick)  # 每次 update 都返回当前全量快照

assert snapshot.get_option(CALL_CODE) is not None, "call_tick 未入 snapshot"
assert snapshot.get_option(PUT_CODE)  is not None, "put_tick 未入 snapshot"
assert snapshot.get_etf(ETF_CODE)     is not None, "etf_tick 未入 snapshot"

# ── 策略扫描 ─────────────────────────────────────────────
strategy = PCPArbitrageStrategy(TradingConfig())
signals  = strategy.scan_pairs_for_display(snapshot, [(call_info, put_info)])

# ── 断言 & 打印 ──────────────────────────────────────────
assert len(signals) == 1, f"期望 1 个信号，实际 {len(signals)} 个"
sig = signals[0]

print("=== Phase 3 冒烟测试 ===")
print(f"  underlying   : {sig.underlying}")
print(f"  strike       : {sig.strike}")
print(f"  net_profit   : {sig.net_profit:.2f} 元")
print(f"  call_bid     : {sig.call_bid}")
print(f"  put_ask      : {sig.put_ask}")
print(f"  spot_ask     : {sig.spot_ask}")
print(f"  max_qty      : {sig.max_qty}")
print(f"  spread_ratio : {round(sig.spread_ratio, 4) if sig.spread_ratio is not None else 'N/A'}")
print(f"  tolerance    : {round(sig.tolerance, 2) if sig.tolerance is not None else 'N/A'}")

# 净利润应远高于阈值（理论 ≈ 441 元）
assert sig.net_profit > 400, f"净利润应 > 400 元，实际 {sig.net_profit:.2f}"

# 执行价格字段应精确等于盘口输入值
assert abs(sig.call_bid - 0.100) < 1e-9, f"call_bid 应=0.100，实际 {sig.call_bid}"
assert abs(sig.put_ask  - 0.060) < 1e-9, f"put_ask  应=0.060，实际 {sig.put_ask}"
assert abs(sig.spot_ask - 2.995) < 1e-9, f"spot_ask 应=2.995，实际 {sig.spot_ask}"

# max_qty = min(c_bid_vol=200, p_ask_vol=120, s_contracts=floor(500*100/10000)=5) = 5
assert sig.max_qty == 5.0, f"max_qty 应=5.0，实际 {sig.max_qty}"

print("所有断言通过 OK")
