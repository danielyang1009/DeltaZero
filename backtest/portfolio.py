# -*- coding: utf-8 -*-
"""
资金与保证金引擎（Portfolio）—— 纯会计层

Phase 5 重构：撮合职责已完全移入 backtest/broker.py（BacktestBroker）。
Portfolio 仅负责：
  - 记账（现金流水、持仓均价、已实现盈亏）
  - 保证金冻结跟踪
  - 账户快照生成

调用方式：
  trades = broker.execute_signal(signal, ...)
  portfolio.process_trades(trades)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from config.settings import TradingConfig
from models import (
    AccountState,
    AssetType,
    ContractInfo,
    OrderSide,
    Position,
    TradeRecord,
)

logger = logging.getLogger(__name__)


class Portfolio:
    """
    资金与保证金引擎（纯会计层）

    维护现金、持仓、保证金占用和已实现盈亏。
    由 BacktestEngine 在收到 Broker 返回的 TradeRecord 列表后调用 process_trades()。

    Attributes:
        cash:          当前可用现金
        positions:     合约代码 → Position 映射
        trade_history: 全部成交记录
        total_margin:  当前保证金占用总额
    """

    def __init__(self, initial_capital: float, config: TradingConfig) -> None:
        self.cash             = initial_capital
        self.initial_capital  = initial_capital
        self.config           = config
        self.positions: Dict[str, Position]    = {}
        self.trade_history: List[TradeRecord]  = []
        self.total_commission: float           = 0.0
        self.total_margin: float               = 0.0
        self._trade_counter: int               = 0
        self._etf_buy_dates: Dict[str, date]   = {}   # ETF T+1 约束

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def process_trades(self, trades: List[TradeRecord]) -> None:
        """
        批量记账：处理 Broker 返回的成交记录列表

        每笔 TradeRecord 按顺序执行：
          1. 分配唯一 trade_id
          2. 现金流（direction * price * qty * multiplier + fee）
          3. 保证金释放（平仓买入 Call 时，在持仓更新前按比例释放）
          4. 持仓更新（均价 / 已实现盈亏）
          5. ETF T+1 约束跟踪
          6. 保证金冻结（仅 Call 卖出腿）
          7. 追加到 trade_history
        """
        for trade in trades:
            # 1. 分配唯一 trade_id
            self._trade_counter += 1
            trade.trade_id = self._trade_counter

            # 2. 现金流
            self.cash -= (
                trade.direction * trade.price * trade.quantity * trade.multiplier
            ) + trade.fee
            self.total_commission += trade.fee

            # 3. 保证金释放（平仓买入 Call 时，按比例释放已冻结保证金）
            #    ⚠️ 必须在 _update_position 之前执行（此时 pos.quantity 仍是平仓前的值）
            if (trade.asset_type == AssetType.OPTION
                    and trade.direction == 1
                    and trade.contract_code in self.positions):
                pos = self.positions[trade.contract_code]
                if pos.quantity < 0 and pos.margin_occupied > 0:
                    close_qty    = min(trade.quantity, abs(pos.quantity))
                    release_ratio = close_qty / abs(pos.quantity)
                    released     = pos.margin_occupied * release_ratio
                    pos.margin_occupied -= released
                    self.total_margin   -= released

            # 4. 持仓更新
            side = OrderSide.BUY if trade.direction > 0 else OrderSide.SELL
            self._update_position(
                trade.contract_code, trade.asset_type, side,
                trade.price, trade.quantity,
            )

            # 5. ETF T+1 约束跟踪
            if trade.asset_type == AssetType.ETF and trade.direction > 0:
                self._etf_buy_dates[trade.contract_code] = trade.timestamp.date()

            # 6. 保证金冻结（仅 Call 卖出腿 margin_reserved > 0）
            if trade.margin_reserved > 0:
                pos = self.positions.get(trade.contract_code)
                if pos:
                    pos.margin_occupied += trade.margin_reserved
                self.total_margin += trade.margin_reserved

            # 7. 记录
            self.trade_history.append(trade)

    def mark_to_market(
        self,
        market_prices: Dict[str, float],
        contracts: Optional[Dict[str, ContractInfo]] = None,
    ) -> float:
        """
        用当前市价更新全部持仓的浮动盈亏

        Args:
            market_prices: 合约代码 → 最新价格
            contracts:     合约信息字典（用于获取调整型合约的真实乘数）

        Returns:
            当前总未实现盈亏
        """
        total_unrealized = 0.0
        for code, pos in self.positions.items():
            if pos.quantity == 0:
                continue
            current_price = market_prices.get(code)
            if current_price is None:
                continue

            if pos.asset_type == AssetType.OPTION:
                unit = (
                    contracts[code].contract_unit
                    if contracts and code in contracts
                    else self.config.contract_unit
                )
                unrealized = (current_price - pos.avg_cost) * pos.quantity * unit
            else:
                unrealized = (current_price - pos.avg_cost) * pos.quantity

            total_unrealized += unrealized
        return total_unrealized

    def snapshot(self, timestamp: datetime) -> AccountState:
        """生成当前账户状态全量快照"""
        return AccountState(
            timestamp=timestamp,
            cash=self.cash,
            total_margin=self.total_margin,
            positions=dict(self.positions),
            realized_pnl=sum(p.realized_pnl for p in self.positions.values()),
            unrealized_pnl=0.0,
            total_commission=self.total_commission,
        )

    # ──────────────────────────────────────────────────────────
    # 持仓工具（内部）
    # ──────────────────────────────────────────────────────────

    def _update_position(
        self,
        code: str,
        asset_type: AssetType,
        side: OrderSide,
        price: float,
        quantity: int,
        *,
        contracts: Optional[Dict[str, ContractInfo]] = None,
    ) -> None:
        """更新持仓，处理开仓/平仓/已实现盈亏"""
        if code not in self.positions:
            self.positions[code] = Position(contract_code=code, asset_type=asset_type)

        pos = self.positions[code]
        signed_qty = quantity if side == OrderSide.BUY else -quantity

        if (pos.quantity >= 0 and signed_qty > 0) or (pos.quantity <= 0 and signed_qty < 0):
            # 同向：加仓，更新均价
            total_cost = pos.avg_cost * abs(pos.quantity) + price * abs(signed_qty)
            new_qty    = pos.quantity + signed_qty
            pos.avg_cost = total_cost / abs(new_qty) if new_qty != 0 else 0.0
            pos.quantity = new_qty
        else:
            # 反向：平仓，计算已实现盈亏
            close_qty = min(abs(pos.quantity), abs(signed_qty))
            if asset_type == AssetType.OPTION and contracts:
                info = contracts.get(code)
                unit = info.contract_unit if info else self.config.contract_unit
            else:
                unit = self.config.contract_unit if asset_type == AssetType.OPTION else 1

            if pos.quantity > 0:
                realized = (price - pos.avg_cost) * close_qty * unit
            else:
                realized = (pos.avg_cost - price) * close_qty * unit

            pos.realized_pnl += realized
            pos.quantity     += signed_qty

            if abs(signed_qty) > close_qty and pos.quantity != 0:
                pos.avg_cost = price
