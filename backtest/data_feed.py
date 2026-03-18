# -*- coding: utf-8 -*-
"""
历史数据生成器

将期权 Tick 和 ETF Tick 合并为按时间排序的 MergedTick 事件流，
供 BacktestEngine 逐事件驱动策略与撮合器。

用法：
    feed = HistoricalFeed(option_ticks, etf_ticks)
    for mtick in feed:
        strategy.on_tick(mtick)

从 backtest/engine.py 的 BacktestEngine._merge_tick_streams() 提取。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional

from models import ETFTickData, TickData


@dataclass
class MergedTick:
    """合并后的 Tick 事件，统一期权和 ETF Tick"""
    timestamp: datetime
    tick_type: str                      # "option" | "etf"
    option_tick: Optional[TickData]  = None
    etf_tick: Optional[ETFTickData]  = None


class HistoricalFeed:
    """
    历史数据生成器

    将期权 Tick 字典和 ETF Tick 列表合并，
    返回一个按时间顺序排列的 MergedTick 迭代器。

    Args:
        option_ticks: 合约代码 → TickData 列表
        etf_ticks:    ETFTickData 列表（可来自真实 K 线或 GBM 模拟）
    """

    def __init__(
        self,
        option_ticks: Dict[str, List[TickData]],
        etf_ticks: List[ETFTickData],
    ) -> None:
        self._merged: List[MergedTick] = self._merge(option_ticks, etf_ticks)

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[MergedTick]:
        return iter(self._merged)

    def __len__(self) -> int:
        return len(self._merged)

    # ──────────────────────────────────────────────────────────
    # 内部实现
    # ──────────────────────────────────────────────────────────

    def _merge(
        self,
        option_ticks: Dict[str, List[TickData]],
        etf_ticks: List[ETFTickData],
    ) -> List[MergedTick]:
        """合并所有 Tick 流并按时间排序"""
        merged: List[MergedTick] = []

        for ticks in option_ticks.values():
            for tick in ticks:
                merged.append(MergedTick(
                    timestamp=tick.timestamp,
                    tick_type="option",
                    option_tick=tick,
                ))

        for tick in etf_ticks:
            merged.append(MergedTick(
                timestamp=tick.timestamp,
                tick_type="etf",
                etf_tick=tick,
            ))

        merged.sort(key=lambda m: m.timestamp)
        return merged
