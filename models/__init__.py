# -*- coding: utf-8 -*-
"""
models 包

向后兼容出口：全项目现有的 `from models import ...` 语句无需修改，
所有原有符号仍可从此顶层包直接导入。

新代码推荐按子模块精确导入：
    from models.data  import TickData, MarketSnapshot
    from models.order import ArbitrageSignal, Order
"""

# ── 来自 models.data ──────────────────────────────────────────
from models.data import (
    OptionType,
    UNDERLYING_MAP,
    CODE_SUFFIX_MAP,
    normalize_code,
    OptionTickData,
    ETFTickData,
    TickPacket,
    DataProvider,
    ContractInfo,
    MarketSnapshot,         # 新增：Phase 3 策略无状态化的核心载体
    GreeksAttribution,
)

# ── 来自 models.order ─────────────────────────────────────────
from models.order import (
    SignalType,
    OrderSide,
    AssetType,
    ArbitrageSignal,        # 新增：Alpha Model 输出
    LegOrder,               # 新增：单腿委托
    Order,                  # 新增：多腿套利订单
    TradeRecord,
    Position,
    AccountState,
    TradeSignal,            # 向后兼容保留，Phase 3 后被 ArbitrageSignal 替代
)

__all__ = [
    # data
    "OptionType", "UNDERLYING_MAP", "CODE_SUFFIX_MAP", "normalize_code",
    "OptionTickData", "ETFTickData", "TickPacket", "DataProvider",
    "ContractInfo", "MarketSnapshot", "GreeksAttribution",
    # order
    "SignalType", "OrderSide", "AssetType",
    "ArbitrageSignal", "LegOrder", "Order",
    "TradeRecord", "Position", "AccountState",
    "TradeSignal",
]
