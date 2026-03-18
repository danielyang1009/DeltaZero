"""
Put-Call Parity 套利策略

基于认沽认购平价关系检测套利机会，严格区分买卖盘口（Bid/Ask）吃单。

正向套利（Forward / Conversion）：买现货 + 买Put + 卖Call
  理论单股利润 = K - (S_ask + P_ask - C_bid)
  真实单张净利 = 理论单股利润 × multiplier - ETF规费 - 期权双边手续费

反向套利（Reverse / Reversal）：融券卖现货 + 卖Put + 买Call
  理论单股利润 = (S_bid + P_bid - C_ask) - K
  真实单张净利 = 理论单股利润 × multiplier - ETF规费 - 期权双边手续费
  注意：反向套利未计融券利息，默认 enable_reverse=False 关闭。

乘数（multiplier）：标准合约 10000，ETF 分红后调整型合约可能为 10265 等。
现货对冲数量等于 multiplier（不一定是 10000 股）。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config.settings import TradingConfig
from models import (
    ContractInfo,
    ETFTickData,
    SignalType,
    TickData,
    TradeSignal,
)
from models.data import MarketSnapshot
from models.order import ArbitrageSignal
from strategies.base import BaseStrategy

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

    def _get_etf_quote(self, underlying_code: Optional[str] = None) -> Optional[ETFTickData]:
        """获取指定（或最近更新的）ETF 行情快照"""
        if underlying_code:
            return self.latest_etf_quotes.get(underlying_code)
        return self.latest_etf_quote

    def get_etf_price(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """获取 ETF 最新价格"""
        quote = self._get_etf_quote(underlying_code)
        return quote.price if quote is not None else None

    def get_etf_ask(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """获取 ETF 卖一价（NaN 时回退到 last）"""
        quote = self._get_etf_quote(underlying_code)
        if quote is None:
            return None
        return quote.ask_price if not math.isnan(quote.ask_price) else quote.price

    def get_etf_bid(self, underlying_code: Optional[str] = None) -> Optional[float]:
        """获取 ETF 买一价（NaN 时回退到 last）"""
        quote = self._get_etf_quote(underlying_code)
        if quote is None:
            return None
        return quote.bid_price if not math.isnan(quote.bid_price) else quote.price

    def get_etf_ask_volume(self, underlying_code: Optional[str] = None) -> Optional[int]:
        """获取 ETF 卖一量（份），未知时返回 None。"""
        quote = self._get_etf_quote(underlying_code)
        if quote is None:
            return None
        return int(quote.ask_volume) if quote.ask_volume > 0 else None

    def get_etf_bid_volume(self, underlying_code: Optional[str] = None) -> Optional[int]:
        """获取 ETF 买一量（份），未知时返回 None。"""
        quote = self._get_etf_quote(underlying_code)
        if quote is None:
            return None
        return int(quote.bid_volume) if quote.bid_volume > 0 else None

    def reset(self) -> None:
        """清空所有快照"""
        self.latest_option_quotes.clear()
        self.latest_etf_quotes.clear()
        self.latest_etf_quote = None


def _calc_forward_metrics(
    *,
    K: float,
    mult: int,
    S_ask: float,
    C_bid: float,
    C_ask: float,
    P_bid: float,
    P_ask: float,
    etf_fee_rate: float,
    option_rt_fee: float,
    c_bid_vol: int,
    c_ask_vol: int,
    p_bid_vol: int,
    p_ask_vol: int,
    s_bid_vol: Optional[int],
    s_ask_vol: Optional[int],
) -> Dict[str, Optional[float]]:
    """
    正向 PCP 套利核心数学计算（模块级纯函数）。

    被 PCPArbitrage._compute_forward_metrics 和
    PCPArbitrageStrategy._evaluate_pair 共同调用。
    """
    fwd_per_share = K - (S_ask + P_ask - C_bid)
    fwd_etf_fee   = S_ask * mult * etf_fee_rate
    fwd_profit    = fwd_per_share * mult - fwd_etf_fee - option_rt_fee

    c_mid = (C_ask + C_bid) / 2.0 if (C_ask + C_bid) > 0 else math.nan
    p_mid = (P_ask + P_bid) / 2.0 if (P_ask + P_bid) > 0 else math.nan
    c_spread = (C_ask - C_bid) / c_mid if c_mid > 0 else math.nan
    p_spread = (P_ask - P_bid) / p_mid if p_mid > 0 else math.nan
    spread_candidates = [x for x in [c_spread, p_spread] if math.isfinite(x) and x >= 0]
    spread_ratio = max(spread_candidates) if spread_candidates else None

    denom_c = c_bid_vol + c_ask_vol
    obi_c   = (c_bid_vol / denom_c) if denom_c > 0 else None
    _s_bv   = s_bid_vol or 0
    _s_av   = s_ask_vol or 0
    denom_s = _s_av + _s_bv
    obi_s   = (_s_av / denom_s) if (_s_av > 0 and denom_s > 0) else None
    denom_p = p_bid_vol + p_ask_vol
    obi_p   = (p_ask_vol / denom_p) if denom_p > 0 else None

    max_qty = None
    if s_ask_vol is not None and s_ask_vol > 0 and mult > 0 and c_bid_vol > 0 and p_ask_vol > 0:
        s_contracts = math.floor(s_ask_vol * 100 / mult)
        max_qty = min(float(c_bid_vol), float(p_ask_vol), float(s_contracts))

    S_ask_1tick   = S_ask + 0.001
    P_ask_1tick   = P_ask + 0.0001
    C_bid_1tick   = max(C_bid - 0.0001, 0.0)
    fwd_1tick     = K - (S_ask_1tick + P_ask_1tick - C_bid_1tick)
    fwd_etf_1tick = S_ask_1tick * mult * etf_fee_rate
    net_1tick     = fwd_1tick * mult - fwd_etf_1tick - option_rt_fee
    tick_loss     = fwd_profit - net_1tick
    tolerance     = (fwd_profit / tick_loss) if tick_loss > 0 else None

    return {
        "fwd_per_share": fwd_per_share,
        "fwd_profit":    fwd_profit,
        "net_1tick":     net_1tick,
        "max_qty":       max_qty,
        "spread_ratio":  spread_ratio,
        "obi_c": obi_c,
        "obi_s": obi_s,
        "obi_p": obi_p,
        "tolerance":     tolerance,
    }


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

    @staticmethod
    def _safe_level1_volume(level1: List[int]) -> int:
        if not level1:
            return 0
        try:
            return max(int(level1[0]), 0)
        except Exception:
            return 0

    def _compute_forward_metrics(
        self,
        *,
        K: float,
        mult: int,
        S_ask: float,
        C_bid: float,
        C_ask: float,
        P_bid: float,
        P_ask: float,
        etf_fee_rate: float,
        option_rt_fee: float,
        c_bid_vol: int,
        c_ask_vol: int,
        p_bid_vol: int,
        p_ask_vol: int,
        s_bid_vol: Optional[int],
        s_ask_vol: Optional[int],
    ) -> Dict[str, Optional[float]]:
        """计算正向套利净利与流动性/滑点指标。"""
        fwd_per_share = K - (S_ask + P_ask - C_bid)
        fwd_etf_fee = S_ask * mult * etf_fee_rate
        fwd_profit = fwd_per_share * mult - fwd_etf_fee - option_rt_fee

        c_mid = (C_ask + C_bid) / 2.0 if (C_ask + C_bid) > 0 else math.nan
        p_mid = (P_ask + P_bid) / 2.0 if (P_ask + P_bid) > 0 else math.nan
        c_spread = (C_ask - C_bid) / c_mid if c_mid > 0 else math.nan
        p_spread = (P_ask - P_bid) / p_mid if p_mid > 0 else math.nan
        spread_candidates = [x for x in [c_spread, p_spread] if math.isfinite(x) and x >= 0]
        spread_ratio = max(spread_candidates) if spread_candidates else None

        # OBI：订单流失衡度。正向套利：卖 Call（需买一支撑）、买 S（需卖一支撑）、买 Put（需卖一支撑）
        denom_c = c_bid_vol + c_ask_vol
        obi_c = (c_bid_vol / denom_c) if denom_c > 0 else None
        _s_bv = s_bid_vol or 0
        _s_av = s_ask_vol or 0
        denom_s = _s_av + _s_bv
        obi_s = (_s_av / denom_s) if (_s_av > 0 and denom_s > 0) else None
        denom_p = p_bid_vol + p_ask_vol
        obi_p = (p_ask_vol / denom_p) if denom_p > 0 else None

        # ETF 一档量在部分数据源不可得，缺失时 max_qty 返回 None（由展示层显示为 --）
        # 交易软件显示买量/卖量为手（1手=100股），需转换为期权张数并向下取整
        max_qty = None
        if s_ask_vol is not None and s_ask_vol > 0 and mult > 0 and c_bid_vol > 0 and p_ask_vol > 0:
            s_contracts = math.floor(s_ask_vol * 100 / mult)
            max_qty = min(float(c_bid_vol), float(p_ask_vol), float(s_contracts))

        # 单 tick 最坏滑点：ETF +0.001，Put +0.0001，Call -0.0001
        S_ask_1tick = S_ask + 0.001
        P_ask_1tick = P_ask + 0.0001
        C_bid_1tick = max(C_bid - 0.0001, 0.0)
        fwd_per_share_1tick = K - (S_ask_1tick + P_ask_1tick - C_bid_1tick)
        fwd_etf_fee_1tick = S_ask_1tick * mult * etf_fee_rate
        net_1tick = fwd_per_share_1tick * mult - fwd_etf_fee_1tick - option_rt_fee
        tick_loss = fwd_profit - net_1tick
        tolerance = (fwd_profit / tick_loss) if tick_loss > 0 else None

        return {
            "fwd_per_share": fwd_per_share,
            "fwd_profit": fwd_profit,
            "net_1tick": net_1tick,
            "max_qty": max_qty,
            "spread_ratio": spread_ratio,
            "obi_c": obi_c,
            "obi_s": obi_s,
            "obi_p": obi_p,
            "tolerance": tolerance,
        }

    def on_option_tick(self, tick: TickData) -> None:
        """接收期权 Tick 更新"""
        self.aligner.update_option(tick)

    def on_etf_tick(self, tick: ETFTickData) -> None:
        """接收 ETF Tick 更新"""
        self.aligner.update_etf(tick)

    def scan_pairs_for_display(
        self,
        call_put_pairs: List[Tuple[ContractInfo, ContractInfo]],
        current_time: Optional[datetime] = None,
    ) -> List[TradeSignal]:
        """
        为展示用：对所有有报价的配对计算正向利润（含负值），不按 min_profit_threshold 过滤。

        每个配对始终返回一个 TradeSignal（signal_type=FORWARD），net_profit_estimate
        为实际利润，可为负值。报价缺失的配对跳过。

        调用方（monitor）负责按行权价相对平值筛选固定行数。
        """
        signals: List[TradeSignal] = []

        for call_info, put_info in call_put_pairs:
            sig = self._evaluate_pair_for_display(call_info, put_info, current_time)
            if sig is not None:
                signals.append(sig)

        signals.sort(key=lambda s: s.strike)
        return signals

    def _evaluate_pair_for_display(
        self,
        call_info: ContractInfo,
        put_info: ContractInfo,
        current_time: Optional[datetime] = None,
    ) -> Optional[TradeSignal]:
        """计算配对的正向利润，始终返回 TradeSignal（不按阈值过滤），报价缺失则返回 None。"""
        call_tick = self.aligner.get_option_quote(call_info.contract_code)
        put_tick  = self.aligner.get_option_quote(put_info.contract_code)
        underlying = call_info.underlying_code
        etf_price  = self.aligner.get_etf_price(underlying)

        if call_tick is None or put_tick is None or etf_price is None:
            return None

        if current_time is None:
            current_time = max(call_tick.timestamp, put_tick.timestamp)

        C_bid = call_tick.bid_prices[0]
        C_ask = call_tick.ask_prices[0]
        P_bid = put_tick.bid_prices[0]
        P_ask = put_tick.ask_prices[0]

        if any(math.isnan(p) for p in [C_bid, C_ask, P_bid, P_ask]):
            return None
        if any(p <= 0 for p in [C_bid, C_ask, P_bid, P_ask]):
            return None

        K    = call_info.strike_price
        mult = call_info.contract_unit
        T    = call_info.time_to_expiry(current_time.date())
        r    = self.config.risk_free_rate

        _s_ask = self.aligner.get_etf_ask(underlying)
        S_ask = _s_ask if _s_ask is not None else etf_price
        s_ask_vol = self.aligner.get_etf_ask_volume(underlying)
        s_bid_vol = self.aligner.get_etf_bid_volume(underlying)

        etf_fee_rate   = self.config.etf_fee_rate
        option_rt_fee  = self.config.option_round_trip_fee

        metrics = self._compute_forward_metrics(
            K=K,
            mult=mult,
            S_ask=S_ask,
            C_bid=C_bid,
            C_ask=C_ask,
            P_bid=P_bid,
            P_ask=P_ask,
            etf_fee_rate=etf_fee_rate,
            option_rt_fee=option_rt_fee,
            c_bid_vol=self._safe_level1_volume(call_tick.bid_volumes),
            c_ask_vol=self._safe_level1_volume(call_tick.ask_volumes),
            p_bid_vol=self._safe_level1_volume(put_tick.bid_volumes),
            p_ask_vol=self._safe_level1_volume(put_tick.ask_volumes),
            s_bid_vol=s_bid_vol,
            s_ask_vol=s_ask_vol,
        )
        fwd_per_share = float(metrics["fwd_per_share"] or 0.0)
        fwd_profit = float(metrics["fwd_profit"] or 0.0)
        if self.config.include_interest and T > 0:
            fwd_profit -= K * (1 - math.exp(-r * T)) * mult
        fwd_detail    = (
            f"K({K:.3g})-S_a({S_ask:.4f})-P_a({P_ask:.4f})+C_b({C_bid:.4f})"
            f"={fwd_per_share:.4f}/股"
        )
        theoretical_spread = etf_price - K * math.exp(-r * T)

        return TradeSignal(
            timestamp=current_time,
            signal_type=SignalType.FORWARD,
            call_code=call_info.contract_code,
            put_code=put_info.contract_code,
            underlying_code=underlying,
            strike=K,
            expiry=call_info.expiry_date,
            call_ask=C_ask, call_bid=C_bid,
            put_ask=P_ask,  put_bid=P_bid,
            spot_price=etf_price,
            theoretical_spread=theoretical_spread,
            actual_spread=C_bid - P_ask,
            net_profit_estimate=fwd_profit,
            confidence=self._calc_confidence(fwd_profit, call_tick, put_tick),
            multiplier=mult,
            is_adjusted=call_info.is_adjusted,
            calc_detail=fwd_detail,
            max_qty=metrics["max_qty"],
            spread_ratio=metrics["spread_ratio"],
            obi_c=metrics["obi_c"],
            obi_s=metrics["obi_s"],
            obi_p=metrics["obi_p"],
            net_1tick=metrics["net_1tick"],
            tolerance=metrics["tolerance"],
        )

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
        self.signal_count += len(signals)
        return signals

    def _evaluate_pair(
        self,
        call_info: ContractInfo,
        put_info: ContractInfo,
        current_time: Optional[datetime] = None,
    ) -> Optional[TradeSignal]:
        """
        评估单对 Call/Put 的套利机会。

        严格使用 Bid/Ask 吃单价格，动态读取合约真实乘数。
        """
        if (
            call_info.strike_price != put_info.strike_price
            or call_info.expiry_date != put_info.expiry_date
            or call_info.underlying_code != put_info.underlying_code
        ):
            logger.warning(
                "配对校验失败: Call=%s Put=%s (K=%.4f/%.4f, exp=%s/%s, und=%s/%s)",
                call_info.contract_code, put_info.contract_code,
                call_info.strike_price, put_info.strike_price,
                call_info.expiry_date, put_info.expiry_date,
                call_info.underlying_code, put_info.underlying_code,
            )
            return None

        call_tick = self.aligner.get_option_quote(call_info.contract_code)
        put_tick  = self.aligner.get_option_quote(put_info.contract_code)
        underlying = call_info.underlying_code
        etf_price  = self.aligner.get_etf_price(underlying)

        if call_tick is None or put_tick is None or etf_price is None:
            return None

        if current_time is None:
            current_time = max(call_tick.timestamp, put_tick.timestamp)

        C_bid = call_tick.bid_prices[0]
        C_ask = call_tick.ask_prices[0]
        P_bid = put_tick.bid_prices[0]
        P_ask = put_tick.ask_prices[0]

        if any(math.isnan(p) for p in [C_bid, C_ask, P_bid, P_ask]):
            return None
        if any(p <= 0 for p in [C_bid, C_ask, P_bid, P_ask]):
            return None

        K    = call_info.strike_price
        mult = call_info.contract_unit                 # 真实乘数（标准 10000 或调整后）
        T    = call_info.time_to_expiry(current_time.date())
        r    = self.config.risk_free_rate

        _s_ask = self.aligner.get_etf_ask(underlying)
        S_ask = _s_ask if _s_ask is not None else etf_price
        _s_bid = self.aligner.get_etf_bid(underlying)
        S_bid = _s_bid if _s_bid is not None else etf_price
        s_ask_vol = self.aligner.get_etf_ask_volume(underlying)
        s_bid_vol = self.aligner.get_etf_bid_volume(underlying)

        etf_fee_rate        = self.config.etf_fee_rate
        option_rt_fee       = self.config.option_round_trip_fee
        theoretical_spread  = etf_price - K * math.exp(-r * T)

        # ── 正向套利（Forward）：买现货 + 买Put + 卖Call ─────────
        metrics = self._compute_forward_metrics(
            K=K,
            mult=mult,
            S_ask=S_ask,
            C_bid=C_bid,
            C_ask=C_ask,
            P_bid=P_bid,
            P_ask=P_ask,
            etf_fee_rate=etf_fee_rate,
            option_rt_fee=option_rt_fee,
            c_bid_vol=self._safe_level1_volume(call_tick.bid_volumes),
            c_ask_vol=self._safe_level1_volume(call_tick.ask_volumes),
            p_bid_vol=self._safe_level1_volume(put_tick.bid_volumes),
            p_ask_vol=self._safe_level1_volume(put_tick.ask_volumes),
            s_bid_vol=s_bid_vol,
            s_ask_vol=s_ask_vol,
        )
        fwd_per_share = float(metrics["fwd_per_share"] or 0.0)
        fwd_profit = float(metrics["fwd_profit"] or 0.0)
        fwd_detail     = (
            f"K({K:.3g})-S_a({S_ask:.4f})-P_a({P_ask:.4f})+C_b({C_bid:.4f})"
            f"={fwd_per_share:.4f}/股"
        )

        # ── 反向套利（Reverse）：融券卖现货 + 卖Put + 买Call ─────
        rev_per_share  = (S_bid + P_bid - C_ask) - K
        rev_etf_fee    = S_bid * mult * etf_fee_rate
        rev_profit     = rev_per_share * mult - rev_etf_fee - option_rt_fee
        rev_detail     = (
            f"S_b({S_bid:.4f})+P_b({P_bid:.4f})-C_a({C_ask:.4f})-K({K:.3g})"
            f"={rev_per_share:.4f}/股"
        )

        best: Optional[TradeSignal] = None

        # 异常值预警：单张净利超常理时标记，疑为乘数/行权价匹配错误
        if abs(fwd_profit) > 2000 or abs(rev_profit) > 2000:
            logger.warning(
                "疑为计算异常: 正向净利=%.2f 元/张, 反向净利=%.2f 元/张 (Call=%s, K=%.4f, mult=%d)",
                fwd_profit, rev_profit, call_info.contract_code, K, mult,
            )

        if fwd_profit >= self.config.min_profit_threshold:
            best = TradeSignal(
                timestamp=current_time,
                signal_type=SignalType.FORWARD,
                call_code=call_info.contract_code,
                put_code=put_info.contract_code,
                underlying_code=underlying,
                strike=K,
                expiry=call_info.expiry_date,
                call_ask=C_ask, call_bid=C_bid,
                put_ask=P_ask,  put_bid=P_bid,
                spot_price=etf_price,
                theoretical_spread=theoretical_spread,
                actual_spread=C_bid - P_ask,
                net_profit_estimate=fwd_profit,
                confidence=self._calc_confidence(fwd_profit, call_tick, put_tick),
                multiplier=mult,
                is_adjusted=call_info.is_adjusted,
                calc_detail=fwd_detail,
                max_qty=metrics["max_qty"],
                spread_ratio=metrics["spread_ratio"],
                obi_c=metrics["obi_c"],
                obi_s=metrics["obi_s"],
                obi_p=metrics["obi_p"],
                net_1tick=metrics["net_1tick"],
                tolerance=metrics["tolerance"],
            )

        if self.config.enable_reverse and rev_profit >= self.config.min_profit_threshold:
            if best is None or rev_profit > best.net_profit_estimate:
                best = TradeSignal(
                    timestamp=current_time,
                    signal_type=SignalType.REVERSE,
                    call_code=call_info.contract_code,
                    put_code=put_info.contract_code,
                    underlying_code=underlying,
                    strike=K,
                    expiry=call_info.expiry_date,
                    call_ask=C_ask, call_bid=C_bid,
                    put_ask=P_ask,  put_bid=P_bid,
                    spot_price=etf_price,
                    theoretical_spread=theoretical_spread,
                    actual_spread=P_bid - C_ask,
                    net_profit_estimate=rev_profit,
                    confidence=self._calc_confidence(rev_profit, call_tick, put_tick),
                    multiplier=mult,
                    is_adjusted=call_info.is_adjusted,
                    calc_detail=rev_detail,
                    max_qty=metrics["max_qty"],
                    spread_ratio=metrics["spread_ratio"],
                    obi_c=metrics["obi_c"],
                    obi_s=metrics["obi_s"],
                    obi_p=metrics["obi_p"],
                    net_1tick=metrics["net_1tick"],
                    tolerance=metrics["tolerance"],
                )

        return best

    @staticmethod
    def _calc_confidence(
        profit: float,
        call_tick: TickData,
        put_tick: TickData,
    ) -> float:
        """综合置信度：利润大小 + 盘口价差 + 挂单量"""
        profit_score = min(profit / 500.0, 1.0)

        call_spread = call_tick.spread
        put_spread  = put_tick.spread
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


# ══════════════════════════════════════════════════════════════════════
# Phase 3 新架构：PCPArbitrageStrategy（无状态，继承 BaseStrategy）
# ══════════════════════════════════════════════════════════════════════

class PCPArbitrageStrategy(BaseStrategy):
    """
    Put-Call Parity 套利策略（无状态版，Phase 3+）

    Alpha Model：发现定价偏差，输出 ArbitrageSignal。
    不持有任何市场行情状态（LKV 由外部 TickAligner 维护）。

    核心接口：
      generate_signals(snapshot)              — 扫描 self._pairs，返回过阈值信号
      scan_pairs_for_display(snapshot, pairs) — 无阈值过滤，供监控页面展示全量配对
      scan_opportunities(snapshot, pairs)     — 有阈值过滤，供交易触发

    使用示例：
        aligner  = TickAligner()
        strategy = PCPArbitrageStrategy(config)
        strategy.set_pairs(call_put_pairs)

        for tick in stream:
            snapshot = aligner.update_tick(tick)
            signals  = strategy.generate_signals(snapshot)
    """

    def __init__(self, config: TradingConfig) -> None:
        self.config = config
        self._pairs: List[Tuple[ContractInfo, ContractInfo]] = []

    def set_pairs(self, pairs: List[Tuple[ContractInfo, ContractInfo]]) -> None:
        """注入 Call/Put 配对列表（合约元数据，非市场状态）"""
        self._pairs = pairs

    # ──────────────────────────────────────────────────────────
    # BaseStrategy 接口实现
    # ──────────────────────────────────────────────────────────

    def generate_signals(self, snapshot: MarketSnapshot) -> List[ArbitrageSignal]:
        """
        使用 self._pairs 扫描套利机会（含 min_profit_threshold 过滤）。

        需先调用 set_pairs() 注入配对，否则返回空列表。
        """
        if not self._pairs:
            return []
        return self.scan_opportunities(snapshot, self._pairs)

    # ──────────────────────────────────────────────────────────
    # 扫描接口（供 market_cache / backtest 调用）
    # ──────────────────────────────────────────────────────────

    def scan_pairs_for_display(
        self,
        snapshot: MarketSnapshot,
        pairs: List[Tuple[ContractInfo, ContractInfo]],
        current_time: Optional[datetime] = None,
    ) -> List[ArbitrageSignal]:
        """
        为监控页面扫描全量配对，不按 min_profit 过滤（含负利润信号）。

        Args:
            snapshot:     当前市场截面
            pairs:        (Call, Put) 配对列表（可为 ATM 筛选后的子集）
            current_time: 时间戳（None 则从快照 ts 推断）

        Returns:
            ArbitrageSignal 列表，按行权价升序排列
        """
        ts = current_time or snapshot.ts
        results: List[ArbitrageSignal] = []

        for call_info, put_info in pairs:
            sig = self._evaluate_pair(snapshot, call_info, put_info, ts, threshold=None)
            if sig is not None:
                results.append(sig)

        results.sort(key=lambda s: s.strike)
        return results

    def scan_opportunities(
        self,
        snapshot: MarketSnapshot,
        pairs: List[Tuple[ContractInfo, ContractInfo]],
        current_time: Optional[datetime] = None,
    ) -> List[ArbitrageSignal]:
        """
        扫描套利机会，按 min_profit_threshold 过滤，按净利润降序排列。

        Returns:
            满足阈值的 ArbitrageSignal 列表
        """
        ts = current_time or snapshot.ts
        results: List[ArbitrageSignal] = []

        for call_info, put_info in pairs:
            sig = self._evaluate_pair(
                snapshot, call_info, put_info, ts,
                threshold=self.config.min_profit_threshold,
            )
            if sig is not None:
                results.append(sig)

        results.sort(key=lambda s: s.net_profit, reverse=True)
        return results

    # ──────────────────────────────────────────────────────────
    # 核心数学计算（纯函数，从 snapshot 取价）
    # ──────────────────────────────────────────────────────────

    def _evaluate_pair(
        self,
        snapshot: MarketSnapshot,
        call_info: ContractInfo,
        put_info: ContractInfo,
        ts: datetime,
        threshold: Optional[float],
    ) -> Optional[ArbitrageSignal]:
        """
        评估单对 Call/Put 的正向 PCP 套利机会。

        从 MarketSnapshot 提取盘口，执行数学计算，返回 ArbitrageSignal。
        threshold=None 表示不过滤（用于 display 模式）。

        Args:
            snapshot:  市场截面
            call_info: 认购合约元信息
            put_info:  认沽合约元信息
            ts:        计算时间戳
            threshold: 净利润过滤阈值（None 则不过滤）
        """
        # ── 1. 从 snapshot 取盘口 ─────────────────────────────
        call_tick = snapshot.get_option(call_info.contract_code)
        put_tick  = snapshot.get_option(put_info.contract_code)
        underlying = call_info.underlying_code
        etf_tick  = snapshot.get_etf(underlying)

        if call_tick is None or put_tick is None or etf_tick is None:
            return None

        C_bid = call_tick.bid_prices[0]
        C_ask = call_tick.ask_prices[0]
        P_bid = put_tick.bid_prices[0]
        P_ask = put_tick.ask_prices[0]

        if any(math.isnan(p) for p in [C_bid, C_ask, P_bid, P_ask]):
            return None
        if any(p <= 0 for p in [C_bid, C_ask, P_bid, P_ask]):
            return None

        # ── 2. ETF 盘口 ──────────────────────────────────────
        etf_price = etf_tick.price
        S_ask = (
            etf_tick.ask_price
            if not math.isnan(etf_tick.ask_price) and etf_tick.ask_price > 0
            else etf_price
        )
        s_ask_vol = int(etf_tick.ask_volume) if etf_tick.ask_volume > 0 else None
        s_bid_vol = int(etf_tick.bid_volume) if etf_tick.bid_volume > 0 else None

        # ── 3. 合约参数 ───────────────────────────────────────
        K    = call_info.strike_price
        mult = call_info.contract_unit
        T    = call_info.time_to_expiry(ts.date())
        r    = self.config.risk_free_rate

        # ── 4. 计算正向套利指标 ───────────────────────────────
        metrics = _calc_forward_metrics(
            K=K, mult=mult,
            S_ask=S_ask, C_bid=C_bid, C_ask=C_ask,
            P_bid=P_bid, P_ask=P_ask,
            etf_fee_rate=self.config.etf_fee_rate,
            option_rt_fee=self.config.option_round_trip_fee,
            c_bid_vol=PCPArbitrage._safe_level1_volume(call_tick.bid_volumes),
            c_ask_vol=PCPArbitrage._safe_level1_volume(call_tick.ask_volumes),
            p_bid_vol=PCPArbitrage._safe_level1_volume(put_tick.bid_volumes),
            p_ask_vol=PCPArbitrage._safe_level1_volume(put_tick.ask_volumes),
            s_bid_vol=s_bid_vol,
            s_ask_vol=s_ask_vol,
        )

        fwd_per_share = float(metrics["fwd_per_share"] or 0.0)
        fwd_profit    = float(metrics["fwd_profit"] or 0.0)

        if self.config.include_interest and T > 0:
            fwd_profit -= K * (1 - math.exp(-r * T)) * mult

        # 异常值预警
        if abs(fwd_profit) > 2000:
            logger.warning(
                "疑为计算异常: 净利润=%.2f（Call=%s K=%.4f mult=%d）",
                fwd_profit, call_info.contract_code, K, mult,
            )

        # ── 5. 阈值过滤 ───────────────────────────────────────
        if threshold is not None and fwd_profit < threshold:
            return None

        calc_detail = (
            f"K({K:.3g})-S_a({S_ask:.4f})-P_a({P_ask:.4f})+C_b({C_bid:.4f})"
            f"={fwd_per_share:.4f}/股"
        )

        return ArbitrageSignal(
            ts=ts,
            underlying=underlying,
            call_code=call_info.contract_code,
            put_code=put_info.contract_code,
            expiry=call_info.expiry_date,
            strike=K,
            direction=SignalType.FORWARD,
            net_profit=fwd_profit,
            # 执行价格（供 Portfolio 和展示层直接使用）
            call_bid=C_bid,
            put_ask=P_ask,
            spot_ask=S_ask,
            # 流动性指标
            max_qty=metrics["max_qty"],
            spread_ratio=metrics["spread_ratio"],
            obi_call=metrics["obi_c"],
            obi_put=metrics["obi_p"],
            obi_spot=metrics["obi_s"],
            net_1tick=metrics["net_1tick"],
            tolerance=metrics["tolerance"],
            # 元信息
            calc_detail=calc_detail,
            multiplier=mult,
            is_adjusted=call_info.is_adjusted,
            snapshot=snapshot,
        )
