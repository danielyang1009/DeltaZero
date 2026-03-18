# -*- coding: utf-8 -*-
"""
行情数据模型

包含所有与市场行情相关的数据结构：
  - Tick 行情（TickData / ETFTickData）
  - 合约元信息（ContractInfo）
  - 市场截面快照（MarketSnapshot）—— 由 TickAligner 组装，作为策略的唯一输入
  - 希腊字母归因（GreeksAttribution）
  - 通用工具（normalize_code / UNDERLYING_MAP）
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ============================================================
# 枚举
# ============================================================

class OptionType(Enum):
    """期权类型"""
    CALL = "call"
    PUT  = "put"


# ============================================================
# 代码标准化工具
# ============================================================

# 标的简称 -> ETF 代码映射
UNDERLYING_MAP: Dict[str, str] = {
    "50ETF":    "510050.SH",
    "300ETF":   "510300.SH",
    "500ETF":   "510500.SH",
    "科创50":   "588000.SH",
    "科创板50": "588000.SH",
}

# 代码后缀映射（数据源适配）
CODE_SUFFIX_MAP = {
    ".XSHG": ".SH",
    ".XSHE": ".SZ",
}


def normalize_code(code: str, target_suffix: str = ".SH") -> str:
    """
    将不同数据源的证券代码后缀标准化

    Args:
        code: 原始代码，如 '10000001.XSHG' 或 '10000001.SH'
        target_suffix: 目标后缀，默认 '.SH'

    Returns:
        标准化后的代码，如 '10000001.SH'
    """
    if code is None:
        return ""
    code = str(code).strip()
    if not code:
        return ""

    for src, dst in CODE_SUFFIX_MAP.items():
        if code.endswith(src):
            if dst == target_suffix:
                return code.replace(src, dst)
            return code

    if "." in code:
        return code

    return f"{code}{target_suffix}"


# ============================================================
# Tick 行情数据
# ============================================================

@dataclass
class TickData:
    """
    统一的期权 Tick 行情数据结构

    兼容不同数据源的盘口深度差异：50ETF 含5档，300ETF/500ETF 仅1档。
    缺失的档位以 NaN / 0 填充。
    """
    timestamp: datetime
    contract_code: str          # 标准化代码（.SH 后缀）
    current: float              # 最新价
    volume: int                 # 累计成交量
    high: float                 # 最高价
    low: float                  # 最低价
    money: float                # 累计成交额
    position: int               # 持仓量
    ask_prices:  List[float] = field(default_factory=lambda: [math.nan] * 5)
    ask_volumes: List[int]   = field(default_factory=lambda: [0] * 5)
    bid_prices:  List[float] = field(default_factory=lambda: [math.nan] * 5)
    bid_volumes: List[int]   = field(default_factory=lambda: [0] * 5)

    @property
    def mid_price(self) -> float:
        """买卖一档中间价"""
        ask1 = self.ask_prices[0]
        bid1 = self.bid_prices[0]
        if math.isnan(ask1) or math.isnan(bid1):
            return self.current
        return (ask1 + bid1) / 2.0

    @property
    def spread(self) -> float:
        """买卖一档价差"""
        ask1 = self.ask_prices[0]
        bid1 = self.bid_prices[0]
        if math.isnan(ask1) or math.isnan(bid1):
            return math.nan
        return ask1 - bid1


@dataclass
class ETFTickData:
    """
    标的 ETF Tick 数据

    可来自实际数据或模拟器生成，与期权 Tick 时间对齐。
    """
    timestamp: datetime
    etf_code: str               # 如 510050.SH
    price: float                # 最新价
    volume: int = 0
    ask_price:  float = math.nan  # 卖一价
    bid_price:  float = math.nan  # 买一价
    ask_volume: int   = 0         # 卖一量（份）
    bid_volume: int   = 0         # 买一量（份）
    is_simulated: bool = False    # 标记是否为模拟数据


@dataclass
class TickPacket:
    """跨线程/跨模块传递的统一 tick 数据包（data_bus 内部使用）。"""
    is_etf: bool
    tick_row: Dict[str, Any]
    tick_obj: Any
    underlying_code: str


class DataProvider(ABC):
    """统一数据采集接口（data_bus 层实现）。"""

    @abstractmethod
    def start(self) -> bool:
        """启动采集。"""

    @abstractmethod
    def stop(self) -> None:
        """停止采集。"""

    @property
    @abstractmethod
    def option_count(self) -> int:
        """当前期权订阅数量。"""

    @property
    def active_underlyings(self) -> List[str]:
        """当前活跃标的。默认空列表。"""
        return []

    def is_trading_safe(self, underlying: str) -> bool:
        """默认安全；子类可覆盖实现熔断逻辑。"""
        return True


# ============================================================
# 合约元信息
# ============================================================

@dataclass
class ContractInfo:
    """
    期权合约基本信息

    数据来源：metadata/wind_sse_optionchain.xlsx
    """
    contract_code: str          # 标准化代码（.SH 后缀），如 10000001.SH
    short_name: str             # 证券简称，如 "50ETF购2015年3月2200"
    underlying_code: str        # 标的 ETF 代码，如 510050.SH
    option_type: OptionType     # 认购 -> CALL，认沽 -> PUT
    strike_price: float         # 行权价
    list_date: date             # 起始交易日期
    expiry_date: date           # 最后交易日期（到期日）
    delivery_month: str         # 交割月份，如 "201503"
    contract_unit: int = 10000  # 合约单位（标准 10000，调整型合约不等于此值）
    exchange: str = "SH"        # 交易所
    is_adjusted: bool = False   # 是否为调整型合约（ETF 分红后产生，乘数≠10000）

    @property
    def is_call(self) -> bool:
        return self.option_type == OptionType.CALL

    @property
    def is_put(self) -> bool:
        return self.option_type == OptionType.PUT

    def time_to_expiry(self, current_date: date) -> float:
        """计算距到期日的年化时间（以自然日 / 365 计）"""
        delta = (self.expiry_date - current_date).days
        return max(delta / 365.0, 0.0)


# ============================================================
# 市场截面快照（Phase 3 策略无状态化的核心载体）
# ============================================================

@dataclass
class MarketSnapshot:
    """
    市场截面快照

    由 TickAligner（data_engine/tick_aligner.py）持续维护并组装。
    是策略 generate_signals(snapshot) 的唯一输入，确保策略本身完全无状态。

    使用方：
      - 实盘：monitors/monitor.py 中的 TickAligner 在每个 ZMQ tick 后更新快照
      - 回测：backtest/engine.py 中的 TickAligner 在每个 MergedTick 后更新快照
    """
    ts: datetime
    options: Dict[str, TickData]     # contract_code → latest TickData
    etf: Dict[str, ETFTickData]      # etf_code → latest ETFTickData

    def get_option(self, code: str) -> Optional[TickData]:
        return self.options.get(code)

    def get_etf(self, underlying: str) -> Optional[ETFTickData]:
        return self.etf.get(underlying)

    def option_ask1(self, code: str) -> Optional[float]:
        """取期权卖一价（哨兵值 999999.0 视为无效）"""
        tick = self.options.get(code)
        if tick is None:
            return None
        v = tick.ask_prices[0]
        if math.isnan(v) or v >= 999999.0 or v <= 0:
            return None
        return v

    def option_bid1(self, code: str) -> Optional[float]:
        """取期权买一价（0 视为无效）"""
        tick = self.options.get(code)
        if tick is None:
            return None
        v = tick.bid_prices[0]
        if math.isnan(v) or v <= 0:
            return None
        return v

    def etf_ask1(self, etf_code: str) -> Optional[float]:
        tick = self.etf.get(etf_code)
        if tick is None:
            return None
        v = tick.ask_price
        if math.isnan(v) or v >= 999999.0 or v <= 0:
            return None
        return v

    def etf_bid1(self, etf_code: str) -> Optional[float]:
        tick = self.etf.get(etf_code)
        if tick is None:
            return None
        v = tick.bid_price
        if math.isnan(v) or v <= 0:
            return None
        return v


# ============================================================
# Greeks 归因
# ============================================================

@dataclass
class GreeksAttribution:
    """
    希腊字母盈亏归因

    将组合 P&L 拆解为各 Greeks 贡献。
    """
    delta_pnl: float = 0.0
    gamma_pnl: float = 0.0
    theta_pnl: float = 0.0
    vega_pnl: float  = 0.0
    residual: float  = 0.0     # 残差（高阶项 + 模型误差）

    @property
    def total(self) -> float:
        return self.delta_pnl + self.gamma_pnl + self.theta_pnl + self.vega_pnl + self.residual
