"""
标的 ETF 价格模拟器

在缺失真实 ETF Tick 数据时，基于期权市场数据模拟生成对齐的标的价格。

核心策略：
1. 利用 PCP 隐含标的价格 S = C - P + K*exp(-rT) 作为锚点
2. 在锚点之间用几何布朗运动（GBM）插值
3. 确保模拟价格与期权市场逻辑一致
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from models import (
    ContractInfo,
    ETFTickData,
    OptionType,
    TickData,
)

logger = logging.getLogger(__name__)


class ETFSimulator:
    """
    标的 ETF 价格模拟器

    支持两种模式：
    1. 锚点插值模式：从期权价格反推隐含标的价格并用 GBM 填充
    2. 纯 GBM 模式：给定初始价格和波动率直接模拟

    Attributes:
        volatility: 模拟年化波动率
        drift: 模拟年化漂移率
        risk_free_rate: 无风险利率
    """

    _SECONDS_PER_YEAR = 252 * 6.5 * 3600

    def __init__(
        self,
        volatility: float = 0.20,
        drift: float = 0.03,
        risk_free_rate: float = 0.02,
        seed: Optional[int] = None,
    ) -> None:
        """
        初始化模拟器

        Args:
            volatility: 年化波动率
            drift: 年化漂移率（μ）
            risk_free_rate: 无风险利率
            seed: 随机种子（用于可复现的模拟）
        """
        self.volatility = volatility
        self.drift = drift
        self.risk_free_rate = risk_free_rate
        self._rng = np.random.default_rng(seed)

    def _step_gbm(self, price: float, dt_seconds: float) -> float:
        """单步 GBM：S(t+dt) = S(t) * exp((μ - σ²/2)dt + σ√dt·Z)"""
        dt_years = max(dt_seconds / self._SECONDS_PER_YEAR, 1e-10)
        z = self._rng.standard_normal()
        return price * math.exp(
            (self.drift - 0.5 * self.volatility ** 2) * dt_years
            + self.volatility * math.sqrt(dt_years) * z
        )

    def simulate_from_option_ticks(
        self,
        option_ticks: Dict[str, List[TickData]],
        contracts: Dict[str, ContractInfo],
        etf_code: str,
        initial_price: float,
    ) -> List[ETFTickData]:
        """
        基于期权 Tick 数据模拟标的 ETF 价格

        从 Call/Put 市场价格反推隐含标的价格作为锚点，
        在锚点之间用 GBM 插值，确保模拟价格与期权市场一致。

        Args:
            option_ticks: 合约代码 -> TickData 列表
            contracts: 合约代码 -> ContractInfo
            etf_code: 目标 ETF 代码（如 '510050.SH'）
            initial_price: 初始 ETF 价格

        Returns:
            按时间排序的 ETFTickData 列表
        """
        all_timestamps = self._collect_all_timestamps(option_ticks)
        if not all_timestamps:
            logger.warning("无期权 Tick 时间戳，无法模拟 ETF 价格")
            return []

        logger.info(
            "模拟 ETF 价格: %s, 时间范围 %s ~ %s, 共 %d 个时间点",
            etf_code, all_timestamps[0], all_timestamps[-1], len(all_timestamps),
        )

        anchor_points = self._compute_anchor_points(
            option_ticks, contracts, all_timestamps,
        )
        logger.info("计算得到 %d 个隐含价格锚点", len(anchor_points))

        if not anchor_points:
            return self._simulate_pure_gbm(
                all_timestamps, etf_code, initial_price,
            )

        return self._interpolate_with_gbm(
            all_timestamps, anchor_points, etf_code, initial_price,
        )

    def simulate_pure_gbm(
        self,
        timestamps: List[datetime],
        etf_code: str,
        initial_price: float,
    ) -> List[ETFTickData]:
        """
        纯几何布朗运动模拟

        公式：S(t+dt) = S(t) * exp((μ - σ²/2)*dt + σ*√dt*Z)

        Args:
            timestamps: 目标时间点列表（需按时间排序）
            etf_code: ETF 代码
            initial_price: 初始价格

        Returns:
            ETFTickData 列表
        """
        return self._simulate_pure_gbm(timestamps, etf_code, initial_price)

    def _simulate_pure_gbm(
        self,
        timestamps: List[datetime],
        etf_code: str,
        initial_price: float,
    ) -> List[ETFTickData]:
        """纯 GBM 模拟实现"""
        if not timestamps:
            return []

        result: List[ETFTickData] = []
        price = initial_price

        for i, ts in enumerate(timestamps):
            if i == 0:
                result.append(ETFTickData(
                    timestamp=ts, etf_code=etf_code, price=price, is_simulated=True,
                ))
                continue

            dt_seconds = (ts - timestamps[i - 1]).total_seconds()
            price = round(max(self._step_gbm(price, dt_seconds), 0.001), 4)

            spread_half = price * 0.0005
            result.append(ETFTickData(
                timestamp=ts,
                etf_code=etf_code,
                price=price,
                is_simulated=True,
                ask_price=round(price + spread_half, 4),
                bid_price=round(price - spread_half, 4),
            ))

        return result

    def _compute_anchor_points(
        self,
        option_ticks: Dict[str, List[TickData]],
        contracts: Dict[str, ContractInfo],
        all_timestamps: List[datetime],
    ) -> Dict[datetime, float]:
        """
        从期权市场价格反推隐含标的价格锚点

        利用 Put-Call Parity: S_implied = C - P + K * exp(-rT)
        选取同时有 Call/Put 报价的时间点计算。

        Returns:
            时间戳 -> 隐含标的价格
        """
        call_contracts = {}
        put_contracts = {}
        for code, info in contracts.items():
            if info.option_type == OptionType.CALL:
                key = (info.underlying_code, info.strike_price, info.expiry_date)
                call_contracts[key] = code
            else:
                key = (info.underlying_code, info.strike_price, info.expiry_date)
                put_contracts[key] = code

        pair_keys = set(call_contracts.keys()) & set(put_contracts.keys())
        if not pair_keys:
            logger.info("未找到 Call/Put 配对，无法计算隐含价格锚点")
            return {}

        anchors: Dict[datetime, List[float]] = defaultdict(list)
        latest_quotes: Dict[str, TickData] = {}

        tick_index: Dict[str, int] = {code: 0 for code in option_ticks}

        for ts in all_timestamps:
            for code in option_ticks:
                idx = tick_index[code]
                ticks = option_ticks[code]
                while idx < len(ticks) and ticks[idx].timestamp <= ts:
                    latest_quotes[code] = ticks[idx]
                    idx += 1
                tick_index[code] = idx

            for key in pair_keys:
                call_code = call_contracts[key]
                put_code = put_contracts[key]

                call_tick = latest_quotes.get(call_code)
                put_tick = latest_quotes.get(put_code)

                if call_tick is None or put_tick is None:
                    continue

                c_price = call_tick.mid_price
                p_price = put_tick.mid_price

                if c_price <= 0 or p_price <= 0:
                    continue

                _, strike, expiry = key
                t_to_expiry = max((expiry - ts.date()).days / 365.0, 1e-6)
                discount = math.exp(-self.risk_free_rate * t_to_expiry)

                s_implied = c_price - p_price + strike * discount
                if s_implied > 0:
                    anchors[ts].append(s_implied)

        result: Dict[datetime, float] = {}
        for ts, prices in anchors.items():
            result[ts] = float(np.median(prices))

        return result

    def _interpolate_with_gbm(
        self,
        all_timestamps: List[datetime],
        anchors: Dict[datetime, float],
        etf_code: str,
        initial_price: float,
    ) -> List[ETFTickData]:
        """
        在锚点之间用 GBM 插值

        锚点处使用隐含标的价格，非锚点处从最近锚点出发做 GBM 漫步。

        Returns:
            ETFTickData 列表
        """
        sorted_anchor_times = sorted(anchors.keys())
        result: List[ETFTickData] = []

        price = initial_price
        anchor_idx = 0

        for i, ts in enumerate(all_timestamps):
            if ts in anchors:
                price = anchors[ts]
                while anchor_idx < len(sorted_anchor_times) and sorted_anchor_times[anchor_idx] <= ts:
                    anchor_idx += 1
            else:
                if i > 0:
                    dt_seconds = (ts - all_timestamps[i - 1]).total_seconds()
                    price = max(self._step_gbm(price, dt_seconds), 0.001)

            price = round(price, 4)
            spread_half = price * 0.0005

            result.append(ETFTickData(
                timestamp=ts,
                etf_code=etf_code,
                price=price,
                is_simulated=True,
                ask_price=round(price + spread_half, 4),
                bid_price=round(price - spread_half, 4),
            ))

        return result

    @staticmethod
    def _collect_all_timestamps(
        option_ticks: Dict[str, List[TickData]],
    ) -> List[datetime]:
        """收集并去重排序所有期权 Tick 的时间戳"""
        ts_set: set = set()
        for ticks in option_ticks.values():
            for tick in ticks:
                ts_set.add(tick.timestamp)
        return sorted(ts_set)
