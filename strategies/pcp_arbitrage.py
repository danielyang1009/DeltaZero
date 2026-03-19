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
from data_engine.tick_aligner import TickAligner
from models import (
    ContractInfo,
    ETFTickData,
    SignalType,
    OptionTickData,
)
from models.data import MarketSnapshot
from models.order import ArbitrageSignal
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


def _safe_level1_volume(level1: List[int]) -> int:
    """安全取一档量（空列表或非整数返回 0）"""
    if not level1:
        return 0
    try:
        return max(int(level1[0]), 0)
    except Exception:
        return 0


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

    被 PCPArbitrageStrategy._evaluate_pair 调用。
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


def _calc_close_metrics(
    *,
    K: float,
    mult: int,
    S_bid: float,
    C_ask: float,
    P_bid: float,
    etf_fee_rate: float,
    option_rt_fee: float,
) -> Dict[str, Optional[float]]:
    """
    平仓 PCP 套利核心数学计算（模块级纯函数）。

    平仓方向：卖出 ETF + 卖出 Put + 买入 Call（与开仓反向）
    close_per_share = S_bid + P_bid - C_ask - K
    close_net = close_per_share * mult - S_bid * mult * etf_fee_rate - option_rt_fee
    """
    close_per_share = S_bid + P_bid - C_ask - K
    etf_fee         = S_bid * mult * etf_fee_rate
    close_net       = close_per_share * mult - etf_fee - option_rt_fee

    return {
        "close_per_share": close_per_share,
        "close_net":       close_net,
    }


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
      scan_close_opportunities(snapshot, pairs) — 扫描平仓机会（CLOSE 信号）

    使用示例：
        aligner  = TickAligner()
        strategy = PCPArbitrageStrategy(config)
        strategy.set_pairs(call_put_pairs)

        for tick in stream:
            snapshot = aligner.update_tick(tick)
            signals  = strategy.generate_signals(snapshot)
    """

    def __init__(self, config: TradingConfig, close_profit_threshold: float = 0.0) -> None:
        self.config = config
        self.close_profit_threshold = close_profit_threshold
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
            c_bid_vol=_safe_level1_volume(call_tick.bid_volumes),
            c_ask_vol=_safe_level1_volume(call_tick.ask_volumes),
            p_bid_vol=_safe_level1_volume(put_tick.bid_volumes),
            p_ask_vol=_safe_level1_volume(put_tick.ask_volumes),
            s_bid_vol=s_bid_vol,
            s_ask_vol=s_ask_vol,
        )

        fwd_per_share = float(metrics["fwd_per_share"] or 0.0)
        fwd_profit    = float(metrics["fwd_profit"] or 0.0)

        if self.config.include_interest and T > 0:
            fwd_profit -= K * (1 - math.exp(-r * T)) * mult

        # 异常值预警：仅对过大的正利润报警（负利润为正常的"无机会"状态，不报警）
        if fwd_profit > 2000:
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

    def _evaluate_pair_for_close(
        self,
        snapshot: MarketSnapshot,
        call_info: ContractInfo,
        put_info: ContractInfo,
        ts: datetime,
    ) -> Optional[ArbitrageSignal]:
        """
        评估单对 Call/Put 的平仓机会。

        平仓方向：卖 ETF + 卖 Put + 买 Call（反向，释放正向开仓持仓）
        """
        call_tick = snapshot.get_option(call_info.contract_code)
        put_tick  = snapshot.get_option(put_info.contract_code)
        underlying = call_info.underlying_code
        etf_tick  = snapshot.get_etf(underlying)

        if call_tick is None or put_tick is None or etf_tick is None:
            return None

        C_ask = call_tick.ask_prices[0]
        P_bid = put_tick.bid_prices[0]

        if any(math.isnan(p) for p in [C_ask, P_bid]):
            return None
        if any(p <= 0 for p in [C_ask, P_bid]):
            return None

        S_bid = (
            etf_tick.bid_price
            if not math.isnan(etf_tick.bid_price) and etf_tick.bid_price > 0
            else etf_tick.price
        )
        if math.isnan(S_bid) or S_bid <= 0:
            return None

        K    = call_info.strike_price
        mult = call_info.contract_unit

        metrics = _calc_close_metrics(
            K=K, mult=mult,
            S_bid=S_bid, C_ask=C_ask, P_bid=P_bid,
            etf_fee_rate=self.config.etf_fee_rate,
            option_rt_fee=self.config.option_round_trip_fee,
        )

        close_net = float(metrics["close_net"] or 0.0)

        if close_net < self.close_profit_threshold:
            return None

        calc_detail = (
            f"S_b({S_bid:.4f})+P_b({P_bid:.4f})-C_a({C_ask:.4f})-K({K:.3g})"
            f"={float(metrics['close_per_share']):.4f}/股 [CLOSE]"
        )

        return ArbitrageSignal(
            ts=ts,
            underlying=underlying,
            call_code=call_info.contract_code,
            put_code=put_info.contract_code,
            expiry=call_info.expiry_date,
            strike=K,
            direction=SignalType.REVERSE,
            net_profit=close_net,
            # 字段复用：CLOSE 语义下存平仓盘口价
            spot_ask=S_bid,   # 实为 ETF 买一（卖出用）
            put_ask=P_bid,    # 实为 Put 买一（卖出用）
            call_bid=C_ask,   # 实为 Call 卖一（买入用）
            max_qty=None,     # 由 Engine 根据持仓决定
            # 元信息
            calc_detail=calc_detail,
            multiplier=mult,
            is_adjusted=call_info.is_adjusted,
            action="CLOSE",
            snapshot=snapshot,
        )

    def scan_close_opportunities(
        self,
        snapshot: MarketSnapshot,
        pairs: List[Tuple[ContractInfo, ContractInfo]],
        current_time: Optional[datetime] = None,
    ) -> List[ArbitrageSignal]:
        """
        扫描所有配对的平仓机会（Engine 负责按持仓过滤）。

        策略本身不感知持仓，只输出满足阈值的 CLOSE 信号。
        """
        ts = current_time or snapshot.ts
        results: List[ArbitrageSignal] = []

        for call_info, put_info in pairs:
            sig = self._evaluate_pair_for_close(snapshot, call_info, put_info, ts)
            if sig is not None:
                results.append(sig)

        return results
