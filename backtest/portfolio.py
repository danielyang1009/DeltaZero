# -*- coding: utf-8 -*-
"""
资金与保证金引擎（Portfolio）

从 backtest/engine.py 的 Account 类提取，专注于资金管理职责。

实盘微观机制四条规则（在 apply_signal() 中严格执行）：
  1. 跨价撮合：BUY 对齐 ask1，SELL 对齐 bid1，绝不使用 last 价
  2. 哨兵值拦截：ask1=999999.0 或 bid1=0.0 → 认定为废单，拒绝成交
  3. 容量限制：成交组数不超过 signal.max_qty（策略侧已按盘口量约束）
  4. 保证金前置校验：卖出开仓前验证可用资金，不足则拒绝

职责边界：
  - Portfolio 只负责"结算"（资金流水、持仓更新、盈亏记录）
  - 不感知 ZMQ / Parquet / 策略逻辑
  - 由 BacktestEngine 驱动调用
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
    SignalType,
    TradeRecord,
    TradeSignal,
)
from risk.margin import MarginCalculator

logger = logging.getLogger(__name__)

# 哨兵值常量（来自交易软件的无效盘口标记）
_SENTINEL_ASK  = 999999.0
_SENTINEL_BID  = 0.0


class Portfolio:
    """
    资金与保证金引擎

    维护现金、持仓、保证金占用和已实现盈亏。
    每次 apply_signal() 完成四条实盘微观机制校验后再模拟成交。

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

    def apply_signal(
        self,
        signal: TradeSignal,
        margin_calculator: MarginCalculator,
        contracts: Dict[str, ContractInfo],
        underlying_close: float,
        num_sets: int = 1,
        signal_id: Optional[int] = None,
    ) -> List[TradeRecord]:
        """
        执行套利信号（含四条实盘微观机制校验）

        Args:
            signal:            交易信号（TradeSignal，向后兼容）
            margin_calculator: 保证金计算器
            contracts:         合约信息字典
            underlying_close:  标的前收盘价（保证金计算用）
            num_sets:          拟开仓组数
            signal_id:         关联的信号序号（用于追溯）

        Returns:
            成交记录列表；任一校验不通过则返回空列表
        """
        if signal.signal_type == SignalType.FORWARD:
            return self._execute_forward(
                signal, margin_calculator, contracts,
                underlying_close, num_sets, signal_id,
            )
        elif signal.signal_type == SignalType.REVERSE:
            logger.info(
                "检测到反向套利信号（A股做空受限，仅记录）: Strike=%.4f, 预估利润=%.2f",
                signal.strike, signal.net_profit_estimate,
            )
            return []
        return []

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
    # 内部撮合逻辑
    # ──────────────────────────────────────────────────────────

    def _execute_forward(
        self,
        signal: TradeSignal,
        margin_calculator: MarginCalculator,
        contracts: Dict[str, ContractInfo],
        underlying_close: float,
        num_sets: int,
        signal_id: Optional[int],
    ) -> List[TradeRecord]:
        """
        正向套利三腿撮合：买 ETF（ask1）+ 买 Put（ask1）+ 卖 Call（bid1）

        四条微观机制校验顺序：
          1/2 → 3 → 4 → 执行
        """
        unit = signal.multiplier
        fee  = self.config.fee
        slp  = self.config.slippage

        # ── 规则 1 & 2：跨价撮合 + 哨兵值绝对拦截 ─────────────────
        #
        # BUY Put  → 对齐 ask1（signal.put_ask）
        # SELL Call → 对齐 bid1（signal.call_bid）
        # BUY ETF  → 对齐 ask1（signal.spot_price，策略侧已取 etf_ask）
        #
        put_exec   = signal.put_ask
        call_exec  = signal.call_bid
        etf_exec   = signal.spot_price

        if put_exec >= _SENTINEL_ASK or put_exec <= 0:
            logger.warning(
                "规则2：Put ask1=%.4f 触发哨兵值，废单（K=%.4f %s）",
                put_exec, signal.strike, signal.expiry,
            )
            return []

        if call_exec <= _SENTINEL_BID:
            logger.warning(
                "规则2：Call bid1=%.4f 为零，废单（K=%.4f %s）",
                call_exec, signal.strike, signal.expiry,
            )
            return []

        if etf_exec >= _SENTINEL_ASK or etf_exec <= 0:
            logger.warning(
                "规则2：ETF 价格=%.4f 触发哨兵值，废单",
                etf_exec,
            )
            return []

        # 加滑点（跨价撮合已对齐盘口，再加一档模拟冲击成本）
        put_exec  += slp.option_slippage_ticks * slp.option_tick_size
        call_exec -= slp.option_slippage_ticks * slp.option_tick_size
        etf_exec  += slp.etf_slippage_ticks    * slp.etf_tick_size

        # ── 规则 3：容量限制 ────────────────────────────────────────
        #
        # signal.max_qty 由策略在生成信号时按盘口量（min(call_bidv1, put_askv1, etf_askv1)）计算。
        # 此处作为硬性上限，防止超量成交。
        #
        if signal.max_qty is not None and num_sets > signal.max_qty:
            capped = max(1, int(signal.max_qty))
            logger.debug(
                "规则3：容量限制，num_sets %d → %d（max_qty=%.1f）",
                num_sets, capped, signal.max_qty,
            )
            num_sets = capped

        # ── 规则 4：保证金前置校验 ──────────────────────────────────
        #
        # 卖出 Call（Short Open）在成交前必须确认可用资金充足。
        # 若不足，整单拒绝（模拟"冻结失败"）。
        #
        call_info = contracts.get(signal.call_code)
        if call_info is None:
            logger.warning("规则4：未找到 Call 合约信息: %s", signal.call_code)
            return []

        margin_result    = margin_calculator.calc_initial_margin(
            call_info, call_exec, underlying_close,
        )
        required_margin  = margin_result.initial_margin * num_sets

        etf_quantity = num_sets * unit
        etf_cost     = etf_exec * etf_quantity
        etf_comm     = max(etf_cost * fee.etf_commission_rate, fee.etf_min_commission)
        put_cost     = put_exec  * unit * num_sets
        put_comm     = fee.option_commission_per_contract * num_sets
        call_revenue = call_exec * unit * num_sets
        call_comm    = fee.option_commission_per_contract * num_sets

        total_outflow = etf_cost + put_cost + etf_comm + put_comm + call_comm - call_revenue
        required_cash = total_outflow + required_margin

        if self.cash < required_cash:
            logger.info(
                "规则4：资金不足，需 %.2f，可用 %.2f（K=%.4f，跳过）",
                required_cash, self.cash, signal.strike,
            )
            return []

        # ── 执行三腿成交 ─────────────────────────────────────────────
        records: List[TradeRecord] = []

        # ETF 买入（ask1 + 滑点）
        records.append(self._record_trade(
            signal.timestamp, AssetType.ETF, signal.underlying_code,
            OrderSide.BUY, etf_exec, etf_quantity, etf_comm,
            slp.etf_slippage_ticks * slp.etf_tick_size * etf_quantity,
            signal_id=signal_id,
        ))
        self._update_position(
            signal.underlying_code, AssetType.ETF,
            OrderSide.BUY, etf_exec, etf_quantity, contracts=contracts,
        )
        self._etf_buy_dates[signal.underlying_code] = signal.timestamp.date()
        self.cash -= (etf_cost + etf_comm)

        # Put 买入（ask1 + 滑点）
        records.append(self._record_trade(
            signal.timestamp, AssetType.OPTION, signal.put_code,
            OrderSide.BUY, put_exec, num_sets, put_comm,
            slp.option_slippage_ticks * slp.option_tick_size * unit * num_sets,
            signal_id=signal_id,
        ))
        self._update_position(
            signal.put_code, AssetType.OPTION,
            OrderSide.BUY, put_exec, num_sets, contracts=contracts,
        )
        self.cash -= (put_cost + put_comm)

        # Call 卖出（bid1 - 滑点）
        records.append(self._record_trade(
            signal.timestamp, AssetType.OPTION, signal.call_code,
            OrderSide.SELL, call_exec, num_sets, call_comm,
            slp.option_slippage_ticks * slp.option_tick_size * unit * num_sets,
            signal_id=signal_id,
        ))
        self._update_position(
            signal.call_code, AssetType.OPTION,
            OrderSide.SELL, call_exec, num_sets, contracts=contracts,
        )
        self.cash += (call_revenue - call_comm)

        # 冻结保证金
        pos = self.positions.get(signal.call_code)
        if pos:
            pos.margin_occupied = required_margin
        self.total_margin += required_margin

        logger.info(
            "正向套利成交: Strike=%.4f, Expiry=%s, 组数=%d, 保证金=%.2f",
            signal.strike, signal.expiry, num_sets, required_margin,
        )
        return records

    # ──────────────────────────────────────────────────────────
    # 持仓与成交记录工具
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

    def _record_trade(
        self,
        timestamp: datetime,
        asset_type: AssetType,
        code: str,
        side: OrderSide,
        price: float,
        quantity: int,
        commission: float,
        slippage_cost: float,
        signal_id: Optional[int] = None,
    ) -> TradeRecord:
        """生成并记录一笔成交"""
        self._trade_counter += 1
        self.total_commission += commission

        record = TradeRecord(
            trade_id=self._trade_counter,
            timestamp=timestamp,
            asset_type=asset_type,
            contract_code=code,
            side=side,
            price=price,
            quantity=quantity,
            commission=commission,
            slippage_cost=slippage_cost,
            signal_id=signal_id,
        )
        self.trade_history.append(record)
        return record
