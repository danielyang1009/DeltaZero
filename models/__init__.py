# -*- coding: utf-8 -*-
"""
models 包

向后兼容出口：全项目现有的 `from models import ...` 语句无需修改，
所有原有符号仍可从此顶层包直接导入。

新代码推荐按子模块精确导入：
    from models.data  import OptionTickData, MarketSnapshot
    from models.order import ArbitrageSignal, SignalAction, Order
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
    MarketSnapshot,
    GreeksAttribution,
)

# ── 来自 models.order ─────────────────────────────────────────
from models.order import (
    SignalAction,
    OrderSide,
    AssetType,
    BaseSignal,
    ArbitrageSignal,
    DirectionalSignal,
    LegOrder,
    Order,
    TradeRecord,
    Position,
    AccountState,
)

__all__ = [
    # data
    "OptionType", "UNDERLYING_MAP", "CODE_SUFFIX_MAP", "normalize_code",
    "OptionTickData", "ETFTickData", "TickPacket", "DataProvider",
    "ContractInfo", "MarketSnapshot", "GreeksAttribution",
    # order
    "SignalAction", "OrderSide", "AssetType",
    "BaseSignal", "ArbitrageSignal", "DirectionalSignal",
    "LegOrder", "Order",
    "TradeRecord", "Position", "AccountState",
]
