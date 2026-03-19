# -*- coding: utf-8 -*-
"""
回测撮合引擎（Broker）

职责：将 ArbitrageSignal 转化为三腿 TradeRecord 列表。
实现四条实盘微观机制：
  1. 哨兵拦截：ask1=999999.0 或 bid1=0.0 → 废单
  2. 跨价撮合：BUY 对齐 ask1，SELL 对齐 bid1，加一档滑点
  3. 容量限制：成交组数不超过 signal.max_qty
  4. 保证金前置校验：卖出 Call 前验证可用资金

Portfolio 退化为纯会计层，不再承担任何撮合职责。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional

from config.settings import TradingConfig
from models import (
    ArbitrageSignal,
    AssetType,
    ContractInfo,
    OrderSide,
    SignalAction,
    TradeRecord,
)
from risk.margin import MarginCalculator

logger = logging.getLogger(__name__)

# 哨兵值常量（来自交易软件的无效盘口标记）
_SENTINEL_ASK = 999999.0
_SENTINEL_BID = 0.0


class BaseBroker(ABC):
    """撮合引擎抽象基类"""

    @abstractmethod
    def execute_signal(
        self,
        signal: ArbitrageSignal,
        num_sets: int,
        available_cash: float,
        margin_calculator: MarginCalculator,
        contracts: Dict[str, ContractInfo],
        underlying_close: float,
        signal_id: Optional[int] = None,
    ) -> List[TradeRecord]: ...


class BacktestBroker(BaseBroker):
    """
    回测撮合引擎

    四步校验 + FOK（全成或全不成）语义：
    任意一步校验失败立即返回空列表，不做部分成交。
    """

    def __init__(self, config: TradingConfig) -> None:
        self.config = config

    def execute_signal(
        self,
        signal: ArbitrageSignal,
        num_sets: int,
        available_cash: float,
        margin_calculator: MarginCalculator,
        contracts: Dict[str, ContractInfo],
        underlying_close: float,
        signal_id: Optional[int] = None,
    ) -> List[TradeRecord]:
        """
        执行套利信号（FOK 语义）

        Returns:
            三腿 TradeRecord 列表；任一校验失败返回空列表
        """
        if signal.action == SignalAction.CLOSE:
            return self._execute_close(signal, num_sets, signal_id)

        slp = self.config.slippage
        fee = self.config.fee
        unit = signal.multiplier

        # ── 规则 1 & 2：哨兵拦截 + 跨价撮合定价 ──────────────────
        put_exec  = signal.put_ask
        call_exec = signal.call_bid
        etf_exec  = signal.spot_ask

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

        # 加滑点
        put_exec  += slp.option_slippage_ticks * slp.option_tick_size
        call_exec -= slp.option_slippage_ticks * slp.option_tick_size
        etf_exec  += slp.etf_slippage_ticks    * slp.etf_tick_size

        # ── 规则 3：容量限制 ────────────────────────────────────────
        if signal.max_qty is not None and num_sets > signal.max_qty:
            capped = max(1, int(signal.max_qty))
            logger.debug(
                "规则3：容量限制，num_sets %d → %d（max_qty=%.1f）",
                num_sets, capped, signal.max_qty,
            )
            num_sets = capped

        # ── 规则 4：保证金前置校验 ──────────────────────────────────
        call_info = contracts.get(signal.call_code)
        if call_info is None:
            logger.warning("规则4：未找到 Call 合约信息: %s", signal.call_code)
            return []

        required_margin = margin_calculator.calc_initial_margin(
            call_info, call_exec, underlying_close,
        ).initial_margin * num_sets

        etf_quantity  = num_sets * unit
        etf_cost      = etf_exec  * etf_quantity
        etf_comm      = max(etf_cost * fee.etf_commission_rate, fee.etf_min_commission)
        put_cost      = put_exec  * unit * num_sets
        put_comm      = fee.option_commission_per_contract * num_sets
        call_revenue  = call_exec * unit * num_sets
        call_comm     = fee.option_commission_per_contract * num_sets
        total_outflow = etf_cost + put_cost + etf_comm + put_comm + call_comm - call_revenue

        if available_cash < total_outflow + required_margin:
            logger.info(
                "规则4：资金不足，需 %.2f，可用 %.2f（K=%.4f，跳过）",
                total_outflow + required_margin, available_cash, signal.strike,
            )
            return []

        # ── 构造三腿 TradeRecord（trade_id=0，由 Portfolio.process_trades 赋值）──
        ts = signal.ts
        etf_slippage = slp.etf_slippage_ticks * slp.etf_tick_size * etf_quantity
        opt_slippage = slp.option_slippage_ticks * slp.option_tick_size * unit * num_sets

        trades: List[TradeRecord] = [
            # ETF 买入
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.ETF,
                contract_code=signal.underlying,
                side=OrderSide.BUY,
                price=etf_exec,
                quantity=etf_quantity,
                commission=etf_comm,
                slippage_cost=etf_slippage,
                signal_id=signal_id,
                direction=+1,
                multiplier=1,
                margin_reserved=0.0,
            ),
            # Put 买入
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.OPTION,
                contract_code=signal.put_code,
                side=OrderSide.BUY,
                price=put_exec,
                quantity=num_sets,
                commission=put_comm,
                slippage_cost=opt_slippage,
                signal_id=signal_id,
                direction=+1,
                multiplier=unit,
                margin_reserved=0.0,
            ),
            # Call 卖出
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.OPTION,
                contract_code=signal.call_code,
                side=OrderSide.SELL,
                price=call_exec,
                quantity=num_sets,
                commission=call_comm,
                slippage_cost=opt_slippage,
                signal_id=signal_id,
                direction=-1,
                multiplier=unit,
                margin_reserved=required_margin,
            ),
        ]

        logger.info(
            "正向套利成交: Strike=%.4f, Expiry=%s, 组数=%d, 保证金=%.2f",
            signal.strike, signal.expiry, num_sets, required_margin,
        )
        return trades

    def _execute_close(
        self,
        signal: ArbitrageSignal,
        num_sets: int,
        signal_id: Optional[int] = None,
    ) -> List[TradeRecord]:
        """
        执行平仓信号（FOK 语义）

        字段复用约定（CLOSE 语义）：
          signal.spot_ask → ETF 买一价（卖出 ETF）
          signal.put_ask  → Put 买一价（卖出 Put）
          signal.call_bid → Call 卖一价（买入 Call）
        """
        slp  = self.config.slippage
        fee  = self.config.fee
        unit = signal.multiplier

        etf_exec  = signal.spot_ask   # ETF bid
        put_exec  = signal.put_ask    # Put bid
        call_exec = signal.call_bid   # Call ask

        # ── 哨兵拦截 ────────────────────────────────────────────────
        if etf_exec <= 0 or etf_exec >= _SENTINEL_ASK:
            logger.warning("CLOSE 废单：ETF 买一价=%.4f 无效（K=%.4f）", etf_exec, signal.strike)
            return []
        if put_exec <= 0 or put_exec >= _SENTINEL_ASK:
            logger.warning("CLOSE 废单：Put 买一价=%.4f 无效（K=%.4f）", put_exec, signal.strike)
            return []
        if call_exec <= 0 or call_exec >= _SENTINEL_ASK:
            logger.warning("CLOSE 废单：Call 卖一价=%.4f 无效（K=%.4f）", call_exec, signal.strike)
            return []

        # ── 容量限制（平仓盘口承接能力）────────────────────────────
        if signal.max_qty is not None and num_sets > signal.max_qty:
            capped = int(signal.max_qty)
            if capped <= 0:
                logger.warning(
                    "CLOSE 废单：盘口容量为零，废单（K=%.4f %s）",
                    signal.strike, signal.expiry,
                )
                return []
            logger.debug(
                "CLOSE 容量限制：num_sets %d → %d（max_qty=%.1f）",
                num_sets, capped, signal.max_qty,
            )
            num_sets = capped

        # ── 滑点（平仓方向反转：卖出向下，买入向上）────────────────
        etf_exec  -= slp.etf_slippage_ticks    * slp.etf_tick_size
        put_exec  -= slp.option_slippage_ticks * slp.option_tick_size
        call_exec += slp.option_slippage_ticks * slp.option_tick_size

        # ── 手续费计算 ───────────────────────────────────────────────
        etf_quantity = num_sets * unit
        etf_cost     = etf_exec * etf_quantity
        etf_comm     = max(etf_cost * fee.etf_commission_rate, fee.etf_min_commission)
        put_comm     = fee.option_commission_per_contract * num_sets
        call_comm    = fee.option_commission_per_contract * num_sets

        ts           = signal.ts
        etf_slippage = slp.etf_slippage_ticks    * slp.etf_tick_size * etf_quantity
        opt_slippage = slp.option_slippage_ticks * slp.option_tick_size * unit * num_sets

        trades: List[TradeRecord] = [
            # ETF 卖出
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.ETF,
                contract_code=signal.underlying,
                side=OrderSide.SELL,
                price=etf_exec,
                quantity=etf_quantity,
                commission=etf_comm,
                slippage_cost=etf_slippage,
                signal_id=signal_id,
                direction=-1,
                multiplier=1,
                margin_reserved=0.0,
            ),
            # Put 卖出
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.OPTION,
                contract_code=signal.put_code,
                side=OrderSide.SELL,
                price=put_exec,
                quantity=num_sets,
                commission=put_comm,
                slippage_cost=opt_slippage,
                signal_id=signal_id,
                direction=-1,
                multiplier=unit,
                margin_reserved=0.0,
            ),
            # Call 买入（买回空头）
            TradeRecord(
                trade_id=0,
                timestamp=ts,
                asset_type=AssetType.OPTION,
                contract_code=signal.call_code,
                side=OrderSide.BUY,
                price=call_exec,
                quantity=num_sets,
                commission=call_comm,
                slippage_cost=opt_slippage,
                signal_id=signal_id,
                direction=+1,
                multiplier=unit,
                margin_reserved=0.0,
            ),
        ]

        logger.info(
            "平仓成交: Strike=%.4f, Expiry=%s, 组数=%d",
            signal.strike, signal.expiry, num_sets,
        )
        return trades
