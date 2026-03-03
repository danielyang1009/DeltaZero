"""
Put-Call Parity 套利策略

基于认沽认购平价关系 C - P = S - K*exp(-rT) 检测套利机会。

正向套利（Conversion）：当 C - P > S - K*exp(-rT) + costs
  操作：买入现货 + 买入 Put + 卖出 Call
  锁定利润 = (C_bid - P_ask) - (S_ask - K*exp(-rT)) - costs

反向套利（Reversal）：当 K*exp(-rT) - S > P - C + costs
  操作：卖出现货 + 卖出 Put + 买入 Call（A股现货做空受限，仅记录）
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from config.settings import TradingConfig
from models import (
    ContractInfo,
    ETFTickData,
    OptionType,
    SignalType,
    TickData,
    TradeSignal,
    normalize_code,
)

logger = logging.getLogger(__name__)


class TickAligner:
    """
    多合约 Tick 流时间对齐器

    维护每个合约的最新报价快照（Last-Known-Value 机制）。
    支持多标的 ETF：按 etf_code 分别存储，避免多品种互相覆盖。

    Attributes:
        latest_option_quotes: 合约代码 -> 最新 TickData
        latest_etf_quotes: ETF代码 -> 最新 ETFTickData（多品种支持）
        latest_etf_quote: 最近更新的 ETF 行情（向后兼容）
    """

    def __init__(self) -> None:
        self.latest_option_quotes: Dict[str, TickData] = {}
        self.latest_etf_quotes: Dict[str, ETFTickData] = {}   # 按标的代码分别存储
        self.latest_etf_quote: Optional[ETFTickData] = None   # 向后兼容：最近更新的 ETF

    def update_option(self, tick: TickData) -> None:
        """更新期权报价快照"""
        self.latest_option_quotes[tick.contract_code] = tick

    def update_etf(self, tick: ETFTickData) -> None:
        """更新 ETF 报价快照（按 etf_code 分别存储）"""
        self.latest_etf_quotes[tick.etf_code] = tick
        self.latest_etf_quote = tick  # 向后兼容

    def get_option_quote(self, code: str) -> Optional[TickData]:
        """获取指定合约的最新报价"""
        return self.latest_option_quotes.get(code)

    def get_etf_price(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """
        获取 ETF 最新价格

        Args:
            underlying_code: ETF 代码（如 '510050.SH'）。不传则返回最近更新的 ETF 价格。
        """
        quote = (
            self.latest_etf_quotes.get(underlying_code)
            if underlying_code
            else self.latest_etf_quote
        )
        return quote.price if quote is not None else None

    def get_etf_ask(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """获取 ETF 卖一价"""
        quote = (
            self.latest_etf_quotes.get(underlying_code)
            if underlying_code
            else self.latest_etf_quote
        )
        if quote is None:
            return None
        price = quote.ask_price
        return price if not math.isnan(price) else quote.price

    def get_etf_bid(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """获取 ETF 买一价"""
        quote = (
            self.latest_etf_quotes.get(underlying_code)
            if underlying_code
            else self.latest_etf_quote
        )
        if quote is None:
            return None
        price = quote.bid_price
        return price if not math.isnan(price) else quote.price

    def reset(self) -> None:
        """清空所有快照"""
        self.latest_option_quotes.clear()
        self.latest_etf_quotes.clear()
        self.latest_etf_quote = None


class PCPArbitrage:
    """
    Put-Call Parity 套利策略

    扫描同行权价的 Call/Put 对，结合实时标的价格检测 PCP 偏离，
    输出标准化的交易信号。

    Attributes:
        config: 交易配置
        aligner: Tick 对齐器
        signal_count: 累计产生的信号数量
    """

    def __init__(self, config: TradingConfig) -> None:
        """
        初始化策略

        Args:
            config: 交易配置（含费率、滑点、阈值等参数）
        """
        self.config = config
        self.aligner = TickAligner()
        self.signal_count: int = 0

    def on_option_tick(self, tick: TickData) -> None:
        """接收期权 Tick 更新"""
        self.aligner.update_option(tick)

    def on_etf_tick(self, tick: ETFTickData) -> None:
        """接收 ETF Tick 更新"""
        self.aligner.update_etf(tick)

    def scan_opportunities(
        self,
        call_put_pairs: List[Tuple[ContractInfo, ContractInfo]],
        current_time: Optional[datetime] = None,
    ) -> List[TradeSignal]:
        """
        扫描 PCP 套利机会

        遍历所有 Call/Put 配对，计算理论价差与实际价差的偏离，
        过滤出满足最低利润阈值的信号。

        Args:
            call_put_pairs: (Call ContractInfo, Put ContractInfo) 配对列表
            current_time: 当前时间（不传则从最新报价推断）

        Returns:
            满足条件的 TradeSignal 列表，按预估利润降序排列
        """
        signals: List[TradeSignal] = []

        for call_info, put_info in call_put_pairs:
            signal = self._evaluate_pair(call_info, put_info, current_time)
            if signal is not None:
                signals.append(signal)

        signals.sort(key=lambda s: s.net_profit_estimate, reverse=True)
        return signals

    def _evaluate_pair(
        self,
        call_info: ContractInfo,
        put_info: ContractInfo,
        current_time: Optional[datetime] = None,
    ) -> Optional[TradeSignal]:
        """
        评估单对 Call/Put 的套利机会

        计算正向和反向两个方向的利润空间，返回利润更高的信号（如果满足阈值）。

        Returns:
            TradeSignal 或 None
        """
        call_tick = self.aligner.get_option_quote(call_info.contract_code)
        put_tick = self.aligner.get_option_quote(put_info.contract_code)

        # 按合约标的代码查询对应 ETF 行情（多品种支持）
        underlying = call_info.underlying_code
        etf_price = self.aligner.get_etf_price(underlying)

        if call_tick is None or put_tick is None or etf_price is None:
            return None

        if current_time is None:
            current_time = max(call_tick.timestamp, put_tick.timestamp)

        call_bid = call_tick.bid_prices[0]
        call_ask = call_tick.ask_prices[0]
        put_bid = put_tick.bid_prices[0]
        put_ask = put_tick.ask_prices[0]

        if any(math.isnan(p) for p in [call_bid, call_ask, put_bid, put_ask]):
            return None
        if any(p <= 0 for p in [call_bid, call_ask, put_bid, put_ask]):
            return None

        K = call_info.strike_price
        T = call_info.time_to_expiry(current_time.date())
        r = self.config.risk_free_rate
        discount_factor = math.exp(-r * T)
        pv_strike = K * discount_factor

        theoretical_spread = etf_price - pv_strike  # S - K*exp(-rT)

        etf_ask = self.aligner.get_etf_ask(underlying) or etf_price
        etf_bid = self.aligner.get_etf_bid(underlying) or etf_price

        # === 正向套利（Conversion）===
        # 卖出 Call（得 C_bid）+ 买入 Put（付 P_ask）+ 买入现货（付 S_ask）
        forward_revenue = call_bid - put_ask
        forward_cost = etf_ask - pv_strike
        forward_costs = self._estimate_costs(call_bid, put_ask, etf_ask)
        forward_profit = (forward_revenue - forward_cost - forward_costs) * self.config.contract_unit

        # === 反向套利（Reversal）===
        # 买入 Call（付 C_ask）+ 卖出 Put（得 P_bid）+ 卖出现货（得 S_bid）
        reverse_revenue = pv_strike - etf_bid
        reverse_cost = put_bid - call_ask
        reverse_costs = self._estimate_costs(call_ask, put_bid, etf_bid)
        reverse_profit_raw = reverse_revenue + reverse_cost - reverse_costs
        reverse_profit = reverse_profit_raw * self.config.contract_unit

        best_signal: Optional[TradeSignal] = None

        if forward_profit >= self.config.min_profit_threshold:
            self.signal_count += 1
            best_signal = TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.FORWARD,
                call_code=call_info.contract_code,
                put_code=put_info.contract_code,
                underlying_code=call_info.underlying_code,
                strike=K,
                expiry=call_info.expiry_date,
                call_ask=call_ask,
                call_bid=call_bid,
                put_ask=put_ask,
                put_bid=put_bid,
                spot_price=etf_price,
                theoretical_spread=theoretical_spread,
                actual_spread=forward_revenue,
                net_profit_estimate=forward_profit,
                confidence=self._calc_confidence(forward_profit, call_tick, put_tick),
            )

        if reverse_profit >= self.config.min_profit_threshold:
            if best_signal is None or reverse_profit > best_signal.net_profit_estimate:
                self.signal_count += 1
                best_signal = TradeSignal(
                    timestamp=current_time,
                    signal_type=SignalType.REVERSE,
                    call_code=call_info.contract_code,
                    put_code=put_info.contract_code,
                    underlying_code=call_info.underlying_code,
                    strike=K,
                    expiry=call_info.expiry_date,
                    call_ask=call_ask,
                    call_bid=call_bid,
                    put_ask=put_ask,
                    put_bid=put_bid,
                    spot_price=etf_price,
                    theoretical_spread=theoretical_spread,
                    actual_spread=put_bid - call_ask,
                    net_profit_estimate=reverse_profit,
                    confidence=self._calc_confidence(reverse_profit, call_tick, put_tick),
                )

        return best_signal

    def _estimate_costs(
        self,
        option_price_1: float,
        option_price_2: float,
        etf_price: float,
    ) -> float:
        """
        估算单组套利的交易成本（每份合约单位）

        包含：期权手续费（双边2张） + ETF 佣金 + 滑点
        """
        fee = self.config.fee
        slp = self.config.slippage
        unit = self.config.contract_unit

        option_commission = 2.0 * fee.option_commission_per_contract / unit

        etf_turnover = etf_price * unit
        etf_commission = max(etf_turnover * fee.etf_commission_rate, fee.etf_min_commission) / unit

        option_slippage = 2.0 * slp.option_slippage_ticks * slp.option_tick_size
        etf_slippage = slp.etf_slippage_ticks * slp.etf_tick_size

        return option_commission + etf_commission + option_slippage + etf_slippage

    @staticmethod
    def _calc_confidence(
        profit: float,
        call_tick: TickData,
        put_tick: TickData,
    ) -> float:
        """
        计算信号置信度

        综合考虑：利润大小、盘口价差、盘口挂单量。
        """
        profit_score = min(profit / 500.0, 1.0)

        call_spread = call_tick.spread
        put_spread = put_tick.spread
        if math.isnan(call_spread) or math.isnan(put_spread):
            spread_score = 0.3
        else:
            avg_spread = (call_spread + put_spread) / 2.0
            spread_score = max(0.0, 1.0 - avg_spread / 0.01)

        min_vol = min(
            call_tick.bid_volumes[0], call_tick.ask_volumes[0],
            put_tick.bid_volumes[0], put_tick.ask_volumes[0],
        )
        volume_score = min(min_vol / 50.0, 1.0)

        return 0.4 * profit_score + 0.3 * spread_score + 0.3 * volume_score
