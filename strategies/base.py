# -*- coding: utf-8 -*-
"""
策略抽象基类

定义策略的统一接口。所有策略实现类必须继承 BaseStrategy 并实现
generate_signals(snapshot) 方法。

核心约定：
  - 策略类是 Pure Function 风格的"大脑"，不持有任何市场行情状态
  - 唯一输入是 MarketSnapshot（由外部 TickAligner 维护）
  - 唯一输出是 List[ArbitrageSignal]
  - 可以持有配置参数（config, pairs 等），这些属于"配置状态"而非"市场状态"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from models.data import MarketSnapshot
from models.order import ArbitrageSignal


class BaseStrategy(ABC):
    """
    策略抽象基类

    子类必须实现 generate_signals()。

    设计原则：
      - 无状态：不缓存 tick、不维护 LKV
      - 可测试：输入固定 snapshot 必然产生固定输出
      - 可替换：实盘、回测调用方只依赖此接口
    """

    @abstractmethod
    def generate_signals(self, snapshot: MarketSnapshot) -> List[ArbitrageSignal]:
        """
        从市场截面快照中扫描套利机会，返回信号列表。

        Args:
            snapshot: 当前市场截面（由 TickAligner.snapshot() 生成）

        Returns:
            发现的套利信号列表；无机会时返回空列表
        """

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        快照更新事件钩子（可选实现）。

        在每次 TickAligner.update_tick() 之后、调用 generate_signals() 之前触发。
        默认空实现，子类可覆盖用于统计、日志、预计算等轻量操作。

        Args:
            snapshot: 最新市场截面
        """
