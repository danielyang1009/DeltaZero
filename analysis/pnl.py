"""
盈亏分析与归因模块

计算回测结果的核心绩效指标和希腊字母归因分析。
输出包含控制台表格和可选的 matplotlib 图表。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from models import (
    AccountState,
    ArbitrageSignal,
    BaseSignal,
    DirectionalSignal,
    GreeksAttribution,
    TradeRecord,
    AssetType,
    OrderSide,
)

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """回测绩效指标汇总"""
    total_pnl: float                # 总盈亏
    total_return: float             # 总收益率
    annualized_return: float        # 年化收益率
    max_drawdown: float             # 最大回撤（金额）
    max_drawdown_pct: float         # 最大回撤（百分比）
    win_rate: float                 # 胜率
    profit_loss_ratio: float        # 盈亏比
    sharpe_ratio: float             # 夏普比率
    total_trades: int               # 总交易笔数
    total_signals: int              # 总信号数
    total_commission: float         # 总手续费
    avg_profit_per_signal: float    # 每信号平均利润
    trading_days: int               # 交易天数


@dataclass
class SignalPnLResult:
    """单个信号的标准化结算结果（多态 PnL 统计的 DTO）"""
    signal_id:     str    # 信号序号（str 形式，兼容未来非整数 ID）
    signal_type:   str    # 信号类型名，如 "Arbitrage" / "Directional"
    gross_pnl:     float  # 毛利润（不含手续费和滑点）
    net_pnl:       float  # 净利润（含手续费和滑点）
    slippage_cost: float  # 总滑点摩擦
    commission:    float  # 总手续费


class PnLAnalyzer:
    """
    盈亏分析器

    从回测结果（交易记录、权益曲线、信号列表）中计算
    绩效指标和希腊字母归因。

    Attributes:
        risk_free_rate: 无风险利率（年化）
        trading_days_per_year: 年交易日数
    """

    def __init__(
        self,
        risk_free_rate: float = 0.02,
        trading_days_per_year: int = 252,
    ) -> None:
        """
        初始化分析器

        Args:
            risk_free_rate: 无风险年化利率（用于 Sharpe 计算）
            trading_days_per_year: 年交易日数
        """
        self.risk_free_rate = risk_free_rate
        self.trading_days_per_year = trading_days_per_year

    def analyze(
        self,
        trade_history: List[TradeRecord],
        signals: List[BaseSignal],
        equity_curve: List[Tuple[datetime, float]],
        initial_capital: float,
    ) -> PerformanceMetrics:
        """
        计算完整的绩效指标

        Args:
            trade_history: 成交记录列表
            signals: 信号列表
            equity_curve: 权益曲线 [(时间, 权益值), ...]
            initial_capital: 初始资金

        Returns:
            PerformanceMetrics 绩效指标
        """
        if not equity_curve:
            return self._empty_metrics()

        equities = [e[1] for e in equity_curve]
        timestamps = [e[0] for e in equity_curve]

        total_pnl = equities[-1] - initial_capital
        total_return = total_pnl / initial_capital if initial_capital > 0 else 0.0

        trading_days = self._calc_trading_days(timestamps)
        annualized_return = self._annualize_return(total_return, trading_days)

        max_dd, max_dd_pct = self._calc_max_drawdown(equities)

        signal_results = self._dispatch_signal_pnls(signals, trade_history)
        signal_pnls    = [r.net_pnl for r in signal_results]
        win_rate = self._calc_win_rate(signal_pnls)
        pl_ratio = self._calc_profit_loss_ratio(signal_pnls)

        daily_returns = self._calc_daily_returns(equity_curve)
        sharpe = self._calc_sharpe_ratio(daily_returns)

        total_commission = sum(t.commission for t in trade_history)
        avg_profit = total_pnl / len(signals) if signals else 0.0

        return PerformanceMetrics(
            total_pnl=round(total_pnl, 2),
            total_return=round(total_return, 4),
            annualized_return=round(annualized_return, 4),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd_pct, 4),
            win_rate=round(win_rate, 4),
            profit_loss_ratio=round(pl_ratio, 2),
            sharpe_ratio=round(sharpe, 2),
            total_trades=len(trade_history),
            total_signals=len(signals),
            total_commission=round(total_commission, 2),
            avg_profit_per_signal=round(avg_profit, 2),
            trading_days=trading_days,
        )

    def calc_greeks_attribution(
        self,
        trade_history: List[TradeRecord],
        signals: List[BaseSignal],
    ) -> GreeksAttribution:
        """
        希腊字母盈亏归因（骨架实现，结果仅供参考）

        将总 P&L 按固定比例粗略拆分为 Delta / Gamma / Theta / Vega。
        完整实现需要逐 Tick 的 Greeks 快照数据。

        Args:
            trade_history: 成交记录
            signals: 信号列表

        Returns:
            GreeksAttribution 归因结果（近似值）
        """
        logger.warning("Greeks 归因为骨架实现，比例为固定估算值，仅供参考")
        total_pnl = sum(
            (t.price * t.quantity * (1 if t.side == OrderSide.SELL else -1))
            for t in trade_history
            if t.asset_type == AssetType.OPTION
        )

        # 骨架归因：粗略按比例拆分
        # 实际实现应使用逐 Tick 的 Greeks 变化量乘以持仓做积分
        attribution = GreeksAttribution(
            delta_pnl=total_pnl * 0.6,   # Delta 通常贡献最大
            gamma_pnl=total_pnl * 0.15,
            theta_pnl=total_pnl * 0.15,
            vega_pnl=total_pnl * 0.05,
            residual=total_pnl * 0.05,
        )

        logger.info(
            "Greeks 归因（骨架）: Delta=%.2f, Gamma=%.2f, Theta=%.2f, Vega=%.2f, 残差=%.2f",
            attribution.delta_pnl, attribution.gamma_pnl,
            attribution.theta_pnl, attribution.vega_pnl, attribution.residual,
        )
        return attribution

    def print_report(
        self,
        metrics: PerformanceMetrics,
        attribution: Optional[GreeksAttribution] = None,
    ) -> str:
        """
        生成控制台报告文本

        Args:
            metrics: 绩效指标
            attribution: Greeks 归因（可选）

        Returns:
            格式化的报告字符串
        """
        try:
            from tabulate import tabulate
            has_tabulate = True
        except ImportError:
            has_tabulate = False

        lines: List[str] = []
        lines.append("")
        lines.append("=" * 60)
        lines.append("          回测绩效报告")
        lines.append("=" * 60)

        summary_data = [
            ["总盈亏 (P&L)", f"{metrics.total_pnl:,.2f} 元"],
            ["总收益率", f"{metrics.total_return:.2%}"],
            ["年化收益率", f"{metrics.annualized_return:.2%}"],
            ["最大回撤", f"{metrics.max_drawdown:,.2f} 元 ({metrics.max_drawdown_pct:.2%})"],
            ["胜率", f"{metrics.win_rate:.2%}"],
            ["盈亏比", f"{metrics.profit_loss_ratio:.2f}"],
            ["夏普比率", f"{metrics.sharpe_ratio:.2f}"],
            ["总信号数", f"{metrics.total_signals}"],
            ["总成交笔数", f"{metrics.total_trades}"],
            ["总手续费", f"{metrics.total_commission:,.2f} 元"],
            ["每信号平均利润", f"{metrics.avg_profit_per_signal:,.2f} 元"],
            ["交易天数", f"{metrics.trading_days}"],
        ]

        if has_tabulate:
            lines.append(tabulate(summary_data, headers=["指标", "数值"], tablefmt="grid"))
        else:
            for row in summary_data:
                lines.append(f"  {row[0]:20s}  {row[1]}")

        if attribution is not None:
            lines.append("")
            lines.append("-" * 60)
            lines.append("          Greeks 盈亏归因")
            lines.append("-" * 60)

            attr_data = [
                ["Delta P&L", f"{attribution.delta_pnl:,.2f} 元"],
                ["Gamma P&L", f"{attribution.gamma_pnl:,.2f} 元"],
                ["Theta P&L", f"{attribution.theta_pnl:,.2f} 元"],
                ["Vega P&L", f"{attribution.vega_pnl:,.2f} 元"],
                ["残差", f"{attribution.residual:,.2f} 元"],
                ["合计", f"{attribution.total:,.2f} 元"],
            ]

            if has_tabulate:
                lines.append(tabulate(attr_data, headers=["归因项", "金额"], tablefmt="grid"))
            else:
                for row in attr_data:
                    lines.append(f"  {row[0]:20s}  {row[1]}")

        lines.append("=" * 60)
        report = "\n".join(lines)
        return report

    def plot_equity_curve(
        self,
        equity_curve: List[Tuple[datetime, float]],
        title: str = "权益曲线",
        save_path: Optional[str] = None,
    ) -> None:
        """
        绘制权益曲线图

        Args:
            equity_curve: [(时间, 权益值), ...]
            title: 图表标题
            save_path: 保存路径（不传则显示）
        """
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            logger.warning("matplotlib 未安装，跳过权益曲线绘制")
            return

        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False

        times = [e[0] for e in equity_curve]
        values = [e[1] for e in equity_curve]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[3, 1])

        ax1.plot(times, values, linewidth=1.5, color="#2196F3")
        ax1.fill_between(times, values, alpha=0.1, color="#2196F3")
        ax1.set_title(title, fontsize=14)
        ax1.set_ylabel("权益（元）", fontsize=11)
        ax1.grid(True, alpha=0.3)

        if len(values) > 1:
            peak = np.maximum.accumulate(values)
            drawdown = [(v - p) / p if p > 0 else 0 for v, p in zip(values, peak)]
            ax2.fill_between(times, drawdown, alpha=0.4, color="#F44336")
            ax2.set_ylabel("回撤", fontsize=11)
            ax2.set_title("回撤曲线", fontsize=12)
            ax2.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            logger.info("权益曲线已保存: %s", save_path)
        else:
            plt.show()

        plt.close(fig)

    # ============================================================
    # 内部计算方法
    # ============================================================

    def _calc_max_drawdown(
        self, equities: List[float],
    ) -> Tuple[float, float]:
        """计算最大回撤（金额和百分比）"""
        if len(equities) < 2:
            return 0.0, 0.0

        arr = np.array(equities)
        peak = np.maximum.accumulate(arr)
        drawdowns = peak - arr
        drawdown_pcts = np.where(peak > 0, drawdowns / peak, 0.0)

        max_dd = float(drawdowns.max())
        max_dd_pct = float(drawdown_pcts.max())
        return max_dd, max_dd_pct

    def _dispatch_signal_pnls(
        self,
        signals: List[BaseSignal],
        trade_history: List[TradeRecord],
    ) -> List[SignalPnLResult]:
        """
        按信号类型分派结算逻辑，返回标准化 DTO 列表。
        全局统计（胜率/盈亏比）必须基于此结果，禁止在外部直接访问信号字段。
        """
        if not signals:
            return []

        trades_by_signal: Dict[int, List[TradeRecord]] = {}
        for t in trade_history:
            if t.signal_id is not None:
                trades_by_signal.setdefault(t.signal_id, []).append(t)

        results: List[SignalPnLResult] = []
        for i, signal in enumerate(signals):
            legs = trades_by_signal.get(i, [])
            if isinstance(signal, ArbitrageSignal):
                res = self._process_arbitrage(signal, i, legs)
            elif isinstance(signal, DirectionalSignal):
                res = self._process_directional(signal, i, legs)
            else:
                logger.warning("未知的信号类型: %s，跳过", type(signal).__name__)
                continue
            if res is not None:
                results.append(res)
        return results

    def _process_arbitrage(
        self,
        signal: ArbitrageSignal,
        idx: int,
        legs: List[TradeRecord],
    ) -> Optional[SignalPnLResult]:
        if not legs:
            # 无成交记录 = 信号被拒单或未执行，实际盈亏必须为 0
            # ⚠️ 绝不能返回 signal.net_profit，否则产生未成交的"利润幻觉"
            return SignalPnLResult(
                signal_id=str(idx),
                signal_type="Arbitrage",
                gross_pnl=0.0,
                net_pnl=0.0,
                slippage_cost=0.0,
                commission=0.0,
            )
        commission    = sum(t.commission    for t in legs)
        slippage_cost = sum(t.slippage_cost for t in legs)
        # ⚠️ 必须乘以 t.multiplier 还原真实资金流水：
        #   ETF 腿：multiplier=1，quantity=num_sets*unit，两者之积即总股数，正确
        #   期权腿：multiplier=unit(10000)，quantity=num_sets，两者之积还原名义价值
        cash_flow = sum(
            t.price * t.quantity * t.multiplier * (1 if t.side == OrderSide.BUY else -1)
            for t in legs
        )
        gross_pnl = -(cash_flow)
        net_pnl   = -(cash_flow + commission + slippage_cost)
        return SignalPnLResult(
            signal_id=str(idx),
            signal_type="Arbitrage",
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            slippage_cost=slippage_cost,
            commission=commission,
        )

    def _process_directional(
        self,
        signal: DirectionalSignal,
        idx: int,
        legs: List[TradeRecord],
    ) -> Optional[SignalPnLResult]:
        # [FUTURE ARCHITECTURE NOTE]: 未来在此实现单边策略的盈亏归因
        raise NotImplementedError("DirectionalSignal 的 PnL 统计尚未实现")

    @staticmethod
    def _calc_win_rate(pnls: List[float]) -> float:
        """计算胜率"""
        if not pnls:
            return 0.0
        wins = sum(1 for p in pnls if p > 0)
        return wins / len(pnls)

    @staticmethod
    def _calc_profit_loss_ratio(pnls: List[float]) -> float:
        """计算盈亏比（平均盈利 / 平均亏损）"""
        profits = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]

        if not profits or not losses:
            return 0.0

        return (sum(profits) / len(profits)) / (sum(losses) / len(losses))

    def _calc_daily_returns(
        self, equity_curve: List[Tuple[datetime, float]],
    ) -> List[float]:
        """从权益曲线计算日收益率序列"""
        if len(equity_curve) < 2:
            return []

        daily: Dict[str, float] = {}
        for ts, eq in equity_curve:
            day_key = ts.strftime("%Y-%m-%d")
            daily[day_key] = eq

        sorted_days = sorted(daily.keys())
        returns: List[float] = []
        for i in range(1, len(sorted_days)):
            prev_eq = daily[sorted_days[i - 1]]
            curr_eq = daily[sorted_days[i]]
            if prev_eq > 0:
                returns.append((curr_eq - prev_eq) / prev_eq)

        return returns

    def _calc_sharpe_ratio(self, daily_returns: List[float]) -> float:
        """计算年化夏普比率"""
        if len(daily_returns) < 2:
            return 0.0

        arr = np.array(daily_returns)
        daily_rf = self.risk_free_rate / self.trading_days_per_year
        excess_returns = arr - daily_rf

        std = float(np.std(excess_returns, ddof=1))
        if std < 1e-10:
            return 0.0

        mean = float(np.mean(excess_returns))
        return mean / std * math.sqrt(self.trading_days_per_year)

    def _annualize_return(self, total_return: float, trading_days: int) -> float:
        """年化收益率"""
        if trading_days <= 0:
            return 0.0
        years = trading_days / self.trading_days_per_year
        if years <= 0:
            return 0.0
        if total_return <= -1:
            return -1.0
        return (1 + total_return) ** (1 / years) - 1

    @staticmethod
    def _calc_trading_days(timestamps: List[datetime]) -> int:
        """计算覆盖的交易天数"""
        if len(timestamps) < 2:
            return 1
        days = set(ts.date() for ts in timestamps)
        return len(days)

    @staticmethod
    def _empty_metrics() -> PerformanceMetrics:
        """返回空绩效指标"""
        return PerformanceMetrics(
            total_pnl=0, total_return=0, annualized_return=0,
            max_drawdown=0, max_drawdown_pct=0, win_rate=0,
            profit_loss_ratio=0, sharpe_ratio=0, total_trades=0,
            total_signals=0, total_commission=0,
            avg_profit_per_signal=0, trading_days=0,
        )
