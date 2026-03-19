# -*- coding: utf-8 -*-
"""
Tick-by-Tick 回测编排引擎（重构版）

职责：驱动 HistoricalFeed → 策略回调 → Portfolio 撮合循环。

重构说明：
  - 撮合逻辑与资金管理 → backtest/portfolio.py（Portfolio 类）
  - 数据流生成          → backtest/data_feed.py（HistoricalFeed 类）
  - 本文件只保留编排逻辑（循环驱动 + 权益曲线采样 + 结果汇总）

向后兼容：
  - run(option_ticks, etf_ticks, ...) 接口签名不变，内部委托给 HistoricalFeed + Portfolio
  - MergedTick 从 data_feed 重新导出，run.py 的 `from backtest.engine import MergedTick` 继续有效
  - engine.account 属性映射到 engine.portfolio（旧代码通过 account 访问持仓仍可工作）
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from config.settings import TradingConfig
from models import ArbitrageSignal, ContractInfo, ETFTickData, OptionTickData
from risk.margin import MarginCalculator

from backtest.broker import BacktestBroker
from backtest.data_feed import HistoricalFeed, MergedTick   # noqa: F401  (re-export MergedTick)
from backtest.portfolio import Portfolio

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Tick-by-Tick 回测编排引擎

    编排流程：
      HistoricalFeed（时间序列）
        → 逐 MergedTick 推送给 strategy_callback
        → 收到 TradeSignal 列表
        → Portfolio.apply_signal()（含四条微观机制校验）
        → 每 100 tick 采样权益曲线

    Attributes:
        portfolio:         资金与保证金引擎（原 Account）
        signals_generated: 回测中产生的所有信号
        equity_curve:      权益曲线（datetime, equity）列表
    """

    def __init__(self, config: TradingConfig) -> None:
        self.config             = config
        self.portfolio          = Portfolio(config.initial_capital, config)
        self.broker             = BacktestBroker(config)
        self.margin_calculator  = MarginCalculator(config)
        self.signals_generated: List[ArbitrageSignal]       = []
        self.equity_curve: List[Tuple[datetime, float]]     = []
        self._price_cache: Dict[str, float]                 = {}
        self.contracts: Dict[str, ContractInfo]             = {}

    # ──────────────────────────────────────────────────────────
    # 主接口（向后兼容签名）
    # ──────────────────────────────────────────────────────────

    def run(
        self,
        option_ticks: Dict[str, List[OptionTickData]],
        etf_ticks: List[ETFTickData],
        contracts: Dict[str, ContractInfo],
        strategy_callback: Callable[
            [MergedTick, "BacktestEngine"],
            List[ArbitrageSignal],
        ],
        underlying_close: Optional[float] = None,
    ) -> Dict:
        """
        执行 Tick-by-Tick 回测

        Args:
            option_ticks:      合约代码 → TickData 列表
            etf_ticks:         ETF Tick 列表
            contracts:         合约代码 → ContractInfo
            strategy_callback: 策略回调，接收 (MergedTick, engine) 返回信号列表
            underlying_close:  标的前收盘价（保证金计算用，None 则取首个 ETF 价格）

        Returns:
            {"trade_history", "signals", "equity_curve", "final_state"}
        """
        self.contracts = contracts
        feed = HistoricalFeed(option_ticks, etf_ticks)
        logger.info("回测开始：共 %d 个 Tick 事件", len(feed))

        if underlying_close is None and etf_ticks:
            underlying_close = etf_ticks[0].price

        total_signals = 0
        total_trades  = 0
        merged_list   = list(feed)

        for i, mtick in enumerate(merged_list):
            signals = strategy_callback(mtick, self)

            for signal in signals:
                sig_idx = len(self.signals_generated)
                self.signals_generated.append(signal)
                total_signals += 1

                if getattr(signal, 'action', 'OPEN') == 'CLOSE':
                    # CLOSE 路径：由持仓决定组数，绕过 _calc_max_sets
                    current_date = mtick.timestamp.date()
                    num_sets = self._get_closeable_sets(signal, current_date)
                    if num_sets <= 0:
                        continue
                else:
                    # OPEN 路径：现有逻辑不变
                    num_sets = min(
                        self.config.max_position_per_signal,
                        self._calc_max_sets(signal, underlying_close or 0),
                    )
                    if num_sets <= 0:
                        continue

                trades = self.broker.execute_signal(
                    signal, num_sets,
                    available_cash=self.portfolio.cash,
                    margin_calculator=self.margin_calculator,
                    contracts=contracts,
                    underlying_close=underlying_close or 0,
                    signal_id=sig_idx,
                )
                if trades:
                    self.portfolio.process_trades(trades)
                total_trades += len(trades)

            # 每 100 tick 采样一次权益（避免每 tick 都调 mark_to_market 拖慢速度）
            if i % 100 == 0 or i == len(merged_list) - 1:
                market_prices = self._get_latest_prices(mtick)
                unrealized    = self.portfolio.mark_to_market(market_prices, self.contracts)
                equity        = self.portfolio.cash + unrealized
                self.equity_curve.append((mtick.timestamp, equity))

        logger.info(
            "回测完成：%d 个信号，%d 笔成交，最终权益 %.2f",
            total_signals, total_trades,
            self.equity_curve[-1][1] if self.equity_curve else self.portfolio.cash,
        )

        return {
            "trade_history": self.portfolio.trade_history,
            "signals":       self.signals_generated,
            "equity_curve":  self.equity_curve,
            "final_state":   self.portfolio.snapshot(
                merged_list[-1].timestamp if merged_list else datetime.now(),
            ),
        }

    # ──────────────────────────────────────────────────────────
    # 向后兼容属性
    # ──────────────────────────────────────────────────────────

    @property
    def account(self) -> Portfolio:
        """向后兼容：旧代码通过 engine.account 访问，映射到 portfolio"""
        return self.portfolio

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────

    def _calc_max_sets(self, signal: ArbitrageSignal, underlying_close: float) -> int:
        """根据可用资金估算最大开仓组数（粗估，精确校验在 BacktestBroker.execute_signal）"""
        unit           = signal.multiplier
        etf_cost_est   = signal.spot_ask * unit
        margin_est     = underlying_close * unit * self.config.margin.call_margin_ratio_1
        cost_per_set   = etf_cost_est + margin_est

        if cost_per_set <= 0:
            return 0

        max_sets = int(self.portfolio.cash * 0.8 / cost_per_set)
        return max(0, min(max_sets, self.config.max_position_per_signal))

    def _get_closeable_sets(self, signal: ArbitrageSignal, current_date: date) -> int:
        """
        从 Portfolio 提取实际可平仓组数。

        约束：min(|Call空头|, Put多头, ETF多头 // unit)
        ⚠️ ETF T+1：今日买入的 ETF 当天不可卖出，返回 0
        """
        call_pos = self.portfolio.positions.get(signal.call_code)
        put_pos  = self.portfolio.positions.get(signal.put_code)
        etf_pos  = self.portfolio.positions.get(signal.underlying)

        if not (call_pos and call_pos.quantity < 0
                and put_pos and put_pos.quantity > 0
                and etf_pos and etf_pos.quantity > 0):
            return 0

        # T+1 冻结检查：ETF 今日买入不可卖出
        etf_buy_date = self.portfolio._etf_buy_dates.get(signal.underlying)
        if etf_buy_date is not None and etf_buy_date == current_date:
            return 0

        unit = signal.multiplier
        return min(
            abs(call_pos.quantity),
            put_pos.quantity,
            etf_pos.quantity // unit,
        )

    def _get_latest_prices(self, mtick: MergedTick) -> Dict[str, float]:
        """逐 Tick 维护价格缓存，供 mark_to_market 使用"""
        if mtick.option_tick is not None:
            self._price_cache[mtick.option_tick.contract_code] = mtick.option_tick.current
        if mtick.etf_tick is not None:
            self._price_cache[mtick.etf_tick.etf_code] = mtick.etf_tick.price
        return self._price_cache
