"""
VIX-like model-free implied volatility engine.

参考 Cboe VIX 方法论实现单期限无模型方差计算：
    sigma^2 = (2/T) * sum(DeltaK/K^2 * exp(RT) * Q(K)) - (1/T) * (F/K0 - 1)^2
    VIX = 100 * sqrt(sigma^2)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from models import ContractInfo

if TYPE_CHECKING:
    from calculators.yield_curve import BoundedCubicSplineRate


MINUTES_PER_YEAR = 525_600.0


@dataclass
class VIXResult:
    """单个标的的一次 VIX 计算结果。"""

    vix: float
    variance: float
    t: float
    forward: float
    k0: float
    expiry: datetime
    republished: bool = False


@dataclass
class _StrikeQuote:
    strike: float
    call_mid: Optional[float]
    put_mid: Optional[float]


def _safe_mid(bid: float, ask: float) -> Optional[float]:
    if (
        bid is None
        or ask is None
        or math.isnan(bid)
        or math.isnan(ask)
        or bid <= 0
        or ask <= 0
        or ask < bid
    ):
        return None
    return (bid + ask) / 2.0


def _to_expiry_dt(expiry_date, expiry_clock: time) -> datetime:
    return datetime.combine(expiry_date, expiry_clock)


class VIXEngine:
    """
    无模型隐含波动率（VIX-like）计算引擎。

    说明：
    - 使用分钟精度 T，按 525,600 分钟年化。
    - ATM 参照点：|Call - Put| 最小。
    - K0：首个 <= F 的行权价。
    - OTM 组件：K0 下方 Put、K0 上方 Call，连续两个 0 报价后停止。
    - K0 处使用 (Call + Put) / 2。
    """

    def __init__(
        self,
        risk_free_rate: Union[float, "BoundedCubicSplineRate"] = 0.02,
        *,
        minutes_per_year: float = MINUTES_PER_YEAR,
        expiry_clock: time = time(15, 0),
    ) -> None:
        self.risk_free_rate = risk_free_rate
        self.minutes_per_year = minutes_per_year
        self.expiry_clock = expiry_clock

    def compute_for_underlying(
        self,
        pairs: Sequence[Tuple[ContractInfo, ContractInfo]],
        aligner: Any,
        now: datetime,
        *,
        last_result: Optional[VIXResult] = None,
        enable_republication: bool = True,
    ) -> Optional[VIXResult]:
        """
        对单个标的的多到期配对，按最近到期优先计算 VIX。

        Args:
            pairs: (call, put) 配对列表（同标的，可含多到期）
            aligner: 提供 get_option_quote(code) 的对象
            now: 当前时刻
            last_result: 上一次可用结果（用于 republish）
            enable_republication: 当 K0 无有效报价时是否复用 last_result
        """
        by_expiry: Dict[datetime, List[_StrikeQuote]] = {}

        for call_info, put_info in pairs:
            if (
                call_info.expiry_date != put_info.expiry_date
                or call_info.strike_price != put_info.strike_price
            ):
                continue

            call_tick = aligner.get_option_quote(call_info.contract_code)
            put_tick = aligner.get_option_quote(put_info.contract_code)
            call_mid = None
            put_mid = None
            if call_tick is not None:
                call_mid = _safe_mid(call_tick.bid_prices[0], call_tick.ask_prices[0])
            if put_tick is not None:
                put_mid = _safe_mid(put_tick.bid_prices[0], put_tick.ask_prices[0])

            expiry_dt = _to_expiry_dt(call_info.expiry_date, self.expiry_clock)
            by_expiry.setdefault(expiry_dt, []).append(
                _StrikeQuote(
                    strike=call_info.strike_price,
                    call_mid=call_mid,
                    put_mid=put_mid,
                )
            )

        for expiry_dt in sorted(by_expiry.keys()):
            result = self.compute_from_strike_quotes(
                expiry=expiry_dt,
                strike_quotes=by_expiry[expiry_dt],
                now=now,
                last_result=last_result,
                enable_republication=enable_republication,
            )
            if result is not None:
                return result
        return None

    def compute_from_strike_quotes(
        self,
        *,
        expiry: datetime,
        strike_quotes: Iterable[_StrikeQuote],
        now: datetime,
        last_result: Optional[VIXResult] = None,
        enable_republication: bool = True,
    ) -> Optional[VIXResult]:
        """对单一期限计算 VIX。"""
        t = self._calc_t_minutes(now, expiry)
        if t is None:
            return None

        quotes = sorted(strike_quotes, key=lambda x: x.strike)
        if not quotes:
            return None

        # 1) ATM 参照点（|Call - Put| 最小）
        atm_ref = self._pick_atm_reference(quotes)
        if atm_ref is None:
            return None

        k_atm, c_atm, p_atm = atm_ref
        rt = self._get_rt(t)
        exp_rt = math.exp(rt)

        # 2) 远期价格 F 与 K0
        forward = k_atm + exp_rt * (c_atm - p_atm)
        all_strikes = [q.strike for q in quotes]
        k0_candidates = [k for k in all_strikes if k <= forward]
        if not k0_candidates:
            return None
        k0 = max(k0_candidates)

        k0_quote = next((q for q in quotes if q.strike == k0), None)
        if k0_quote is None or k0_quote.call_mid is None or k0_quote.put_mid is None:
            if enable_republication and last_result is not None:
                return VIXResult(
                    vix=last_result.vix,
                    variance=last_result.variance,
                    t=last_result.t,
                    forward=last_result.forward,
                    k0=last_result.k0,
                    expiry=last_result.expiry,
                    republished=True,
                )
            return None

        # 3) 成分筛选 + DeltaK
        q_by_k: Dict[float, float] = {k0: (k0_quote.call_mid + k0_quote.put_mid) / 2.0}

        # K0 以下: OTM Put，向下直到连续两个 0 报价
        puts_below = [q for q in quotes if q.strike < k0]
        zero_streak = 0
        for q in sorted(puts_below, key=lambda x: x.strike, reverse=True):
            if q.put_mid is None or q.put_mid <= 0:
                zero_streak += 1
                if zero_streak >= 2:
                    break
                continue
            zero_streak = 0
            q_by_k[q.strike] = q.put_mid

        # K0 以上: OTM Call，向上直到连续两个 0 报价
        calls_above = [q for q in quotes if q.strike > k0]
        zero_streak = 0
        for q in sorted(calls_above, key=lambda x: x.strike):
            if q.call_mid is None or q.call_mid <= 0:
                zero_streak += 1
                if zero_streak >= 2:
                    break
                continue
            zero_streak = 0
            q_by_k[q.strike] = q.call_mid

        strikes = sorted(q_by_k.keys())
        if len(strikes) < 2:
            return None

        # 4) 核心方差公式
        sigma_sum = 0.0
        for i, k in enumerate(strikes):
            if i == 0:
                delta_k = strikes[1] - strikes[0]
            elif i == len(strikes) - 1:
                delta_k = strikes[-1] - strikes[-2]
            else:
                delta_k = (strikes[i + 1] - strikes[i - 1]) / 2.0

            if delta_k <= 0:
                return None

            qk = q_by_k[k]
            sigma_sum += (delta_k / (k * k)) * exp_rt * qk

        variance = (2.0 / t) * sigma_sum - (1.0 / t) * ((forward / k0 - 1.0) ** 2)
        if not math.isfinite(variance):
            return None

        variance = max(variance, 0.0)
        vix = 100.0 * math.sqrt(variance)
        return VIXResult(
            vix=vix,
            variance=variance,
            t=t,
            forward=forward,
            k0=k0,
            expiry=expiry,
            republished=False,
        )

    def _calc_t_minutes(self, now: datetime, expiry: datetime) -> Optional[float]:
        minutes = (expiry - now).total_seconds() / 60.0
        if minutes <= 0:
            return None
        return minutes / self.minutes_per_year

    def _get_rt(self, t_years: float) -> float:
        """
        返回 e^(rT) 中的 rT。
        - 常数利率: rT = rate * T
        - 曲线利率: r = curve.get_rate(T*365)，再乘 T
        """
        if hasattr(self.risk_free_rate, "get_rate"):
            rate = float(self.risk_free_rate.get_rate(t_years * 365.0))
            return rate * t_years
        return float(self.risk_free_rate) * t_years

    @staticmethod
    def _pick_atm_reference(
        quotes: Sequence[_StrikeQuote],
    ) -> Optional[Tuple[float, float, float]]:
        candidates: List[Tuple[float, float, float, float]] = []
        for q in quotes:
            if q.call_mid is None or q.put_mid is None:
                continue
            diff = abs(q.call_mid - q.put_mid)
            candidates.append((diff, q.strike, q.call_mid, q.put_mid))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0], x[1]))
        _, strike, call_mid, put_mid = candidates[0]
        return strike, call_mid, put_mid
