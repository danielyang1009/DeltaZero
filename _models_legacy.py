"""
全局数据模型定义

定义系统中所有核心数据结构，包括 Tick 行情、合约信息、交易信号、
账户状态等 dataclass，供各模块统一引用。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ============================================================
# 枚举类型
# ============================================================

class OptionType(Enum):
    """期权类型"""
    CALL = "call"
    PUT = "put"


class SignalType(Enum):
    """套利信号方向"""
    FORWARD = "forward"    # 正向：买现货 + 买Put + 卖Call
    REVERSE = "reverse"    # 反向：卖现货 + 卖Put + 买Call


class OrderSide(Enum):
    """委托方向"""
    BUY = "buy"
    SELL = "sell"


class AssetType(Enum):
    """资产类别"""
    OPTION = "option"
    ETF = "etf"


# ============================================================
# Tick 行情数据
# ============================================================

@dataclass
class TickData:
    """
    统一的 Tick 行情数据结构

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
    ask_prices: List[float] = field(default_factory=lambda: [math.nan] * 5)
    ask_volumes: List[int] = field(default_factory=lambda: [0] * 5)
    bid_prices: List[float] = field(default_factory=lambda: [math.nan] * 5)
    bid_volumes: List[int] = field(default_factory=lambda: [0] * 5)

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
    ask_price: float = math.nan # 卖一价
    bid_price: float = math.nan # 买一价
    ask_volume: int = 0         # 卖一量（份）
    bid_volume: int = 0         # 买一量（份）
    is_simulated: bool = False  # 标记是否为模拟数据


@dataclass
class TickPacket:
    """跨线程/跨模块传递的统一 tick 数据包。"""
    is_etf: bool
    tick_row: Dict[str, Any]
    tick_obj: Any
    underlying_code: str


class DataProvider(ABC):
    """统一数据采集接口。"""

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
# 合约基本信息
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

    # 已有交易所后缀（如 .SH/.SZ）则保持原样
    if "." in code:
        return code

    # 纯数字等无后缀代码，补齐目标后缀，确保跨模块可对齐
    return f"{code}{target_suffix}"


@dataclass
class ContractInfo:
    """
    期权合约基本信息

    数据来源：metadata/YYYY-MM-DD_optionchain.csv（fetch_optionchain 产出）
    缺失字段（如合约单位）使用市场默认值。
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
    is_adjusted: bool = False   # 是否为调整型合约（ETF 分红后产生，乘数≠10000，行权价非标准）

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
# 交易信号
# ============================================================

@dataclass
class TradeSignal:
    """
    PCP 套利交易信号

    由策略模块产出，传递给回测引擎执行。
    """
    timestamp: datetime
    signal_type: SignalType     # 正向 / 反向
    call_code: str              # 认购合约代码
    put_code: str               # 认沽合约代码
    underlying_code: str        # 标的 ETF 代码
    strike: float               # 行权价
    expiry: date                # 到期日

    # 触发时的市场价格快照
    call_ask: float             # Call 卖一价（正向套利卖出 Call 的成交价参考）
    call_bid: float             # Call 买一价
    put_ask: float              # Put 卖一价
    put_bid: float              # Put 买一价
    spot_price: float           # 标的 ETF 价格

    # 理论与实际价差
    theoretical_spread: float   # 理论 PCP 价差
    actual_spread: float        # 实际市场价差
    net_profit_estimate: float  # 扣除费用后的预估净利润（每张合约）
    confidence: float = 0.0     # 信号置信度 [0, 1]
    multiplier: int = 10000     # 该合约的真实乘数（标准 10000，调整型可能为 10265 等）
    is_adjusted: bool = False   # 是否为分红调整型合约（乘数≠10000）
    calc_detail: str = ""       # 计算明细（人可读的盘口公式字符串）
    max_qty: Optional[float] = None          # 瓶颈容量（可成交组数）
    spread_ratio: Optional[float] = None     # 盘口价差率（取 Call/Put 最大）
    obi_c: Optional[float] = None            # 订单流失衡度（Call 买一支撑，卖 Call）
    obi_s: Optional[float] = None           # 订单流失衡度（ETF 卖一支撑，买 S）
    obi_p: Optional[float] = None           # 订单流失衡度（Put 卖一支撑，买 Put）
    net_1tick: Optional[float] = None       # 单 tick 压力测试净利润
    tolerance: Optional[float] = None        # 容错空间（可承受 tick 数）


# ============================================================
# 交易记录
# ============================================================

@dataclass
class TradeRecord:
    """
    单笔成交记录

    记录回测引擎中每一笔模拟成交的详细信息。
    """
    trade_id: int
    timestamp: datetime
    asset_type: AssetType       # 期权 / ETF
    contract_code: str
    side: OrderSide             # 买入 / 卖出
    price: float                # 成交价
    quantity: int               # 成交数量（期权为张数，ETF 为份数）
    commission: float           # 手续费
    slippage_cost: float        # 滑点成本
    signal_id: Optional[int] = None  # 关联的信号 ID


# ============================================================
# 持仓与账户
# ============================================================

@dataclass
class Position:
    """
    单品种持仓

    跟踪特定合约的净持仓、成本和盈亏。
    """
    contract_code: str
    asset_type: AssetType
    quantity: int = 0           # 净持仓（正为多头，负为空头）
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
    """
    账户状态快照

    记录某一时刻的资金和持仓全貌。
    """
    timestamp: datetime
    cash: float                             # 可用现金
    total_margin: float                     # 总保证金占用
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0              # 累计已实现盈亏
    unrealized_pnl: float = 0.0            # 未实现盈亏
    total_commission: float = 0.0          # 累计手续费

    @property
    def equity(self) -> float:
        """账户权益 = 现金 + 未实现盈亏"""
        return self.cash + self.unrealized_pnl



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
    vega_pnl: float = 0.0
    residual: float = 0.0      # 残差（高阶项 + 模型误差）

    @property
    def total(self) -> float:
        return self.delta_pnl + self.gamma_pnl + self.theta_pnl + self.vega_pnl + self.residual
