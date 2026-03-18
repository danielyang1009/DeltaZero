# -*- coding: utf-8 -*-
"""
TickAligner — 市场状态引擎

职责：
  1. 接收零散的 TickData / ETFTickData 更新
  2. 维护内部 LKV（Last Known Value）字典
  3. 每次更新后返回当前完整的 MarketSnapshot

这是实盘和回测共用的唯一状态容器。
策略（BaseStrategy 实现类）不再持有任何市场状态，
仅接收 MarketSnapshot 作为输入。

使用示例（实盘）：
    aligner = TickAligner()
    for tick in zmq_stream:
        snapshot = aligner.update_tick(tick)
        signals  = strategy.generate_signals(snapshot)

使用示例（回测）：
    aligner = TickAligner()
    for mtick in feed:
        snapshot = aligner.update_tick(mtick.option_tick or mtick.etf_tick)
        signals  = strategy.generate_signals(snapshot)
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Union

from models.data import ETFTickData, MarketSnapshot, OptionTickData


class TickAligner:
    """
    市场截面状态引擎

    维护所有合约的 LKV 快照，每次接收新 tick 后更新相应条目，
    并返回最新的完整 MarketSnapshot。

    Attributes:
        _options_lkv: 期权合约代码 → 最新 TickData
        _etf_lkv:     ETF 代码    → 最新 ETFTickData
    """

    def __init__(self) -> None:
        self._options_lkv: Dict[str, OptionTickData]    = {}
        self._etf_lkv: Dict[str, ETFTickData]     = {}

    # ──────────────────────────────────────────────────────────
    # 核心接口
    # ──────────────────────────────────────────────────────────

    def update_tick(self, tick: Union[OptionTickData, ETFTickData]) -> MarketSnapshot:
        """
        接收一个新 tick，更新内部 LKV，返回当前完整市场截面快照。

        Args:
            tick: TickData（期权）或 ETFTickData（标的 ETF）

        Returns:
            更新后的 MarketSnapshot（options/etf 字典均为浅拷贝，线程安全读）
        """
        if isinstance(tick, ETFTickData):
            self._etf_lkv[tick.etf_code] = tick
        else:
            self._options_lkv[tick.contract_code] = tick
        return self.snapshot()

    def snapshot(self) -> MarketSnapshot:
        """
        返回当前市场截面快照（浅拷贝）。

        快照的 options / etf 字典在返回时复制一次，
        避免调用方在遍历期间被并发更新覆盖。
        """
        return MarketSnapshot(
            ts=datetime.now(),
            options=dict(self._options_lkv),
            etf=dict(self._etf_lkv),
        )

    # ──────────────────────────────────────────────────────────
    # 辅助接口
    # ──────────────────────────────────────────────────────────

    def update_option(self, tick: OptionTickData) -> None:
        """单独更新期权 LKV（不返回快照，适合批量导入场景）"""
        self._options_lkv[tick.contract_code] = tick

    def update_etf(self, tick: ETFTickData) -> None:
        """单独更新 ETF LKV（不返回快照，适合批量导入场景）"""
        self._etf_lkv[tick.etf_code] = tick

    def get_option_quote(self, code: str) -> Optional[OptionTickData]:
        """获取指定期权合约的最新报价（供 VIX 引擎使用）。"""
        return self._options_lkv.get(code)

    def reset(self) -> None:
        """清空所有 LKV（跨日切换时调用）"""
        self._options_lkv.clear()
        self._etf_lkv.clear()

    @property
    def option_count(self) -> int:
        return len(self._options_lkv)

    @property
    def etf_count(self) -> int:
        return len(self._etf_lkv)
