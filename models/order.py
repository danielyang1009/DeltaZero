# -*- coding: utf-8 -*-
"""
信号、订单与账户模型

职责分层：
  ArbitrageSignal  — Alpha Model 输出：套利机会观察（展示 + 决策依据，不含执行意图）
  LegOrder / Order — Execution Model 输入：执行层（broker / 回测撮合器）从 Signal 转化而来
  TradeRecord      — 成交记录（已执行，不可变）
  Position         — 单品种持仓（快照，由 Portfolio 维护）
  AccountState     — 账户全量快照
  TradeSignal      — 向后兼容保留，Phase 3 后将被 ArbitrageSignal 替代
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Dict, List, Optional

# ArbitrageSignal 中引用 MarketSnapshot
from models.data import MarketSnapshot


# ============================================================
# 枚举
# ============================================================

class SignalType(Enum):
    """套利信号方向"""
    FORWARD = "forward"    # 正向：买现货 + 买Put + 卖Call
    REVERSE = "reverse"    # 反向：卖现货 + 卖Put + 买Call


class OrderSide(Enum):
    """委托方向"""
    BUY  = "buy"
    SELL = "sell"


class AssetType(Enum):
    """资产类别"""
    OPTION = "option"
    ETF    = "etf"


# ============================================================
# Alpha Model 输出：ArbitrageSignal
# ============================================================

@dataclass
class ArbitrageSignal:
    """
    套利机会信号（Alpha Model 唯一输出）

    策略只负责发现定价偏差并输出此结构。
    是否下单、如何下单由外层 Execution Model 决定。

    字段设计原则：
      - 包含足够的信息供前端展示和人工决策
      - 不含执行细节（无 Order、无成交价格假设）
      - snapshot 提供触发时刻的完整市场截面引用
    """
    ts: datetime
    underlying: str             # 标的 ETF 代码，如 510050.SH
    call_code: str              # 认购合约代码
    put_code: str               # 认沽合约代码
    expiry: date                # 到期日
    strike: float               # 行权价
    direction: SignalType       # 正向 / 反向

    # 核心收益指标
    net_profit: float                       # 扣费后净利润（元/组）

    # 执行价格（信号触发时刻的盘口价格，自包含，无需再查 snapshot）
    call_bid: float = 0.0                   # Call 买一价（卖出 Call 对齐 bid1）
    put_ask: float  = 0.0                   # Put 卖一价（买入 Put 对齐 ask1）
    spot_ask: float = 0.0                   # ETF 卖一价（买入 ETF 对齐 ask1）

    # 风险与质量指标（ETF 量未知时为 None）
    max_qty: Optional[float]    = None      # 理论最大可成交组数（受盘口量约束）
    spread_ratio: Optional[float] = None   # 盘口价差率（Call/Put 取最大）
    obi_call: Optional[float]   = None     # Call 订单失衡度（买一/卖一量比）
    obi_put: Optional[float]    = None     # Put 订单失衡度
    obi_spot: Optional[float]   = None     # ETF 订单失衡度
    net_1tick: Optional[float]  = None     # 净利润对单 tick 滑动的敏感度
    tolerance: Optional[float]  = None     # 可承受的 tick 数（容错空间）

    # 触发时刻的市场快照引用（只读，供需要完整盘口的场景）
    snapshot: Optional[MarketSnapshot] = None

    # 调试/展示用
    calc_detail: str = ""                   # 人可读的盘口公式字符串
    multiplier: int  = 10000               # 合约单位（调整型合约可能不等于 10000）
    is_adjusted: bool = False              # 是否为分红调整型合约


# ============================================================
# Execution Model 输入：LegOrder / Order
# ============================================================

@dataclass
class LegOrder:
    """单腿委托（多腿订单的组成部分）"""
    code: str
    side: OrderSide
    qty: int
    limit_price: float          # 对齐盘口的委托价：BUY→ask1，SELL→bid1


@dataclass
class Order:
    """
    多腿套利订单

    由执行层（broker / backtest engine）从 ArbitrageSignal 转化而来。
    携带具体的委托价格和数量，供撮合引擎执行。
    """
    signal_ref: ArbitrageSignal
    legs: List[LegOrder]
    created_at: datetime
    num_sets: int = 1           # 组数（所有腿的数量倍数）

    @property
    def direction(self) -> SignalType:
        return self.signal_ref.direction


# ============================================================
# 成交记录（不可变，回测/实盘均使用）
# ============================================================

@dataclass
class TradeRecord:
    """
    单笔成交记录

    记录每一笔模拟成交的详细信息。
    quantity 对于期权为张数，对于 ETF 为份数。
    """
    trade_id: int
    timestamp: datetime
    asset_type: AssetType
    contract_code: str
    side: OrderSide
    price: float                # 实际成交价（含滑点）
    quantity: int
    commission: float
    slippage_cost: float
    signal_id: Optional[int] = None   # 关联的信号序号


# ============================================================
# 持仓与账户状态
# ============================================================

@dataclass
class Position:
    """单品种持仓（净持仓，由 Portfolio 维护）"""
    contract_code: str
    asset_type: AssetType
    quantity: int   = 0         # 净持仓（正为多头，负为空头）
    avg_cost: float = 0.0      # 持仓均价
    realized_pnl: float = 0.0  # 已实现盈亏
    margin_occupied: float = 0.0  # 占用保证金（仅期权卖方）

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


@dataclass
class AccountState:
    """账户状态全量快照（某一时刻）"""
    timestamp: datetime
    cash: float
    total_margin: float
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float   = 0.0
    unrealized_pnl: float = 0.0
    total_commission: float = 0.0

    @property
    def equity(self) -> float:
        """账户权益 = 现金 + 未实现盈亏"""
        return self.cash + self.unrealized_pnl


# ============================================================
# 向后兼容：TradeSignal（Phase 3 后将被 ArbitrageSignal 替代）
# ============================================================

@dataclass
class TradeSignal:
    """
    PCP 套利交易信号（旧版，向后兼容保留）

    Phase 3 重构完成后，此类型将被 ArbitrageSignal 替代。
    新代码请使用 ArbitrageSignal。
    """
    timestamp: datetime
    signal_type: SignalType
    call_code: str
    put_code: str
    underlying_code: str
    strike: float
    expiry: date

    # 触发时的市场价格快照
    call_ask: float
    call_bid: float
    put_ask: float
    put_bid: float
    spot_price: float

    # 理论与实际价差
    theoretical_spread: float
    actual_spread: float
    net_profit_estimate: float
    confidence: float = 0.0
    multiplier: int = 10000
    is_adjusted: bool = False
    calc_detail: str = ""
    max_qty: Optional[float] = None
    spread_ratio: Optional[float] = None
    obi_c: Optional[float] = None
    obi_s: Optional[float] = None
    obi_p: Optional[float] = None
    net_1tick: Optional[float] = None
    tolerance: Optional[float] = None
