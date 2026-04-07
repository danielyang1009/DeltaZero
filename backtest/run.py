"""
回测运行入口（编排层）

推荐用法：
    python -m backtest --data-dir sample_data
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.settings import TradingConfig, get_default_config
from models import ArbitrageSignal, ContractInfo, ETFTickData

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest.run")


def run_backtest(
    config: TradingConfig,
    data_dir: str = "sample_data",
    start_month: Optional[str] = None,
    end_month: Optional[str] = None,
    output_chart: Optional[str] = None,
    etf_data_dir: Optional[str] = None,
    bar_mode: str = "close",
) -> None:
    from analysis.pnl import PnLAnalyzer
    from backtest.engine import BacktestEngine, MergedTick
    from data_engine.bar_data_loader import BarDataLoader
    from data_engine.contract_catalog import ContractInfoManager, get_optionchain_path
    from backtest.etf_price_simulator import ETFSimulator
    from data_engine.tick_data_loader import TickLoader
    from data_engine.tick_aligner import TickAligner
    from strategies.pcp_arbitrage import PCPArbitrageStrategy

    logger.info("=" * 60)
    logger.info("Step 1: 加载合约基本信息")
    logger.info("=" * 60)

    contract_mgr = ContractInfoManager()
    ref_date = datetime.strptime(start_month + "-01", "%Y-%m-%d").date() if start_month else datetime.now().date()
    optionchain_path = get_optionchain_path(target_date=ref_date)
    if optionchain_path.exists():
        count = contract_mgr.load_from_optionchain(optionchain_path, target_date=ref_date)
        logger.info("已从 optionchain 加载 %d 条合约信息", count)
    else:
        logger.warning("optionchain 文件不存在: %s，请先抓取当日期权链", optionchain_path)
        logger.warning("将无法进行 Call/Put 配对，回测功能受限")

    logger.info("=" * 60)
    logger.info("Step 2: 加载 Tick 数据")
    logger.info("=" * 60)

    loader = TickLoader()
    data_path = Path(data_dir)
    if not data_path.exists():
        logger.error("数据目录不存在: %s", data_path)
        return

    option_ticks = loader.load_directory(data_path, start_month=start_month, end_month=end_month)
    if not option_ticks:
        logger.error("未加载到任何 Tick 数据")
        return

    for code, ticks in option_ticks.items():
        logger.info("  合约 %s: %d 条 Tick", code, len(ticks))

    logger.info("=" * 60)
    logger.info("Step 3: 匹配合约信息")
    logger.info("=" * 60)

    contracts: Dict[str, ContractInfo] = {}
    underlying_codes = set()

    for code in option_ticks:
        info = contract_mgr.get_info(code)
        if info is not None:
            contracts[code] = info
            underlying_codes.add(info.underlying_code)
            logger.info(
                "  %s -> %s | %s | K=%.4f | 到期=%s",
                code,
                info.short_name,
                info.option_type.value,
                info.strike_price,
                info.expiry_date,
            )
        else:
            logger.warning("  %s: 未找到合约信息", code)

    if not contracts:
        logger.error("没有任何合约能匹配到基本信息，无法继续")
        return

    all_pairs: List[Tuple[ContractInfo, ContractInfo]] = []
    for underlying in underlying_codes:
        expiries = contract_mgr.get_available_expiries(underlying)
        for expiry in expiries:
            pairs = contract_mgr.find_call_put_pairs(underlying, expiry=expiry)
            active_pairs = [
                (c, p) for c, p in pairs if c.contract_code in option_ticks or p.contract_code in option_ticks
            ]
            all_pairs.extend(active_pairs)

    logger.info("找到 %d 组 Call/Put 配对（有 Tick 数据）", len(all_pairs))

    logger.info("=" * 60)
    logger.info("Step 4: 构建 ETF 事件流（真实 K 线 或 GBM 模拟）")
    logger.info("=" * 60)

    etf_ticks_map: Dict[str, List[ETFTickData]] = {}

    if etf_data_dir and Path(etf_data_dir).is_dir():
        logger.info("加载真实 ETF K 线数据: %s（模式: %s）", etf_data_dir, bar_mode)
        bar_loader = BarDataLoader(mode=bar_mode)
        etf_ticks_map = bar_loader.load_directory(
            etf_data_dir,
            pattern="*.csv",
            start_date=start_month,
            end_date=end_month,
        )
        pq_ticks = bar_loader.load_directory(
            etf_data_dir,
            pattern="*.parquet",
            start_date=start_month,
            end_date=end_month,
        )
        for code, ticks in pq_ticks.items():
            etf_ticks_map.setdefault(code, []).extend(ticks)
    elif etf_data_dir:
        logger.warning("ETF 数据目录不存在: %s，将使用 GBM 模拟", etf_data_dir)

    missing_etf = underlying_codes - set(etf_ticks_map.keys())
    if missing_etf:
        logger.warning("以下标的无 K 线数据，将使用 GBM 模拟: %s", missing_etf)
        simulator = ETFSimulator(
            volatility=config.simulation_volatility,
            drift=config.simulation_drift,
            risk_free_rate=config.risk_free_rate,
            seed=42,
        )
        for underlying in missing_etf:
            related_contracts = {
                code: info for code, info in contracts.items() if info.underlying_code == underlying
            }
            related_ticks = {code: ticks for code, ticks in option_ticks.items() if code in related_contracts}
            strike_prices = [info.strike_price for info in related_contracts.values()]
            initial_price = sum(strike_prices) / len(strike_prices) if strike_prices else 3.0
            etf_data = simulator.simulate_from_option_ticks(
                related_ticks,
                related_contracts,
                underlying,
                initial_price,
            )
            etf_ticks_map[underlying] = etf_data
            logger.info("  %s: GBM 模拟 %d 条 ETF Tick（初始价≈%.4f）", underlying, len(etf_data), initial_price)

    all_etf_ticks: List[ETFTickData] = []
    for code, ticks in etf_ticks_map.items():
        logger.info("  %s: %d 条 ETF 事件", code, len(ticks))
        all_etf_ticks.extend(ticks)
    all_etf_ticks.sort(key=lambda t: t.timestamp)

    logger.info("=" * 60)
    logger.info("Step 5: 执行 Tick-by-Tick 回测")
    logger.info("=" * 60)

    aligner = TickAligner()
    pcp_strategy = PCPArbitrageStrategy(config)
    pcp_strategy.set_pairs(all_pairs)
    engine = BacktestEngine(config)
    underlying_close = all_etf_ticks[0].price if all_etf_ticks else 3.0

    def strategy_callback(mtick: MergedTick, bt_engine: BacktestEngine) -> List[ArbitrageSignal]:
        if mtick.tick_type == "option" and mtick.option_tick is not None:
            aligner.update_option(mtick.option_tick)
        elif mtick.tick_type == "etf" and mtick.etf_tick is not None:
            aligner.update_etf(mtick.etf_tick)
        snapshot = aligner.snapshot()
        return pcp_strategy.generate_signals(snapshot)

    results = engine.run(
        option_ticks=option_ticks,
        etf_ticks=all_etf_ticks,
        contracts=contracts,
        strategy_callback=strategy_callback,
        underlying_close=underlying_close,
    )

    logger.info("=" * 60)
    logger.info("Step 6: 生成分析报告")
    logger.info("=" * 60)

    analyzer = PnLAnalyzer(
        risk_free_rate=config.risk_free_rate,
        trading_days_per_year=config.trading_days_per_year,
    )
    metrics = analyzer.analyze(
        trade_history=results["trade_history"],
        signals=results["signals"],
        equity_curve=results["equity_curve"],
        initial_capital=config.initial_capital,
    )
    attribution = analyzer.calc_greeks_attribution(
        trade_history=results["trade_history"],
        signals=results["signals"],
    )
    report = analyzer.print_report(metrics, attribution)
    print(report)

    if output_chart and results["equity_curve"]:
        analyzer.plot_equity_curve(
            results["equity_curve"],
            title="DeltaZero — PCP 套利回测 权益曲线",
            save_path=output_chart,
        )

    if results["signals"]:
        print("\n--- 套利信号摘要（前10条） ---")
        for i, sig in enumerate(results["signals"][:10]):
            direction = "正向" if sig.direction == 1 else "反向"
            print(
                f"  [{i + 1}] {sig.ts} | {direction} | "
                f"K={sig.strike:.4f} | 净利润={sig.net_profit:.2f}元"
            )


def run_backtest_parquet(
    config: TradingConfig,
    market_data_dir: str = r"D:\MARKET_DATA",
    underlyings: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    output_chart: Optional[str] = None,
) -> None:
    """
    Parquet 数据回测入口（DataBus 落盘格式）。

    通过 BacktestService 执行，与 Web UI 共用核心逻辑。
    """
    from web.backtest_service import BacktestService, BacktestParams

    if underlyings is None:
        from config.settings import UNDERLYINGS
        underlyings = list(UNDERLYINGS)

    params = BacktestParams(
        underlyings=underlyings,
        start_date=start_date or "20260101",
        end_date=end_date or "20261231",
        initial_capital=config.initial_capital,
        min_profit=config.min_profit_threshold,
        max_position_per_signal=config.max_position_per_signal,
        market_data_dir=market_data_dir,
    )

    def _cli_progress(msg):
        stage = msg.get("stage", "")
        if stage == "running":
            logger.info(
                "回测中: %s (%d/%d) | %d ticks",
                msg.get("current_date"), msg.get("done_dates", 0),
                msg.get("total_dates", 0), msg.get("ticks_processed", 0),
            )
        elif stage == "done":
            logger.info("回测完成")
        elif "message" in msg:
            logger.info(msg["message"])

    service = BacktestService()
    result = service.run(params, progress_callback=_cli_progress)

    # 打印核心指标
    m = result["metrics"]
    print("\n" + "=" * 60)
    print("DeltaZero PCP 套利回测结果")
    print("=" * 60)
    print(f"  品种:     {', '.join(underlyings)}")
    print(f"  日期范围: {start_date} ~ {end_date}")
    print(f"  交易天数: {m['trading_days']}")
    print(f"  总收益:   {m['total_pnl']:,.2f} 元")
    print(f"  总收益率: {m['total_return']:.2f}%")
    print(f"  年化收益: {m['annualized_return']:.2f}%")
    print(f"  最大回撤: {m['max_drawdown_pct']:.2f}%")
    print(f"  夏普比率: {m['sharpe_ratio']:.2f}")
    print(f"  胜率:     {m['win_rate']:.1f}%")
    plr = m['profit_loss_ratio']
    print(f"  盈亏比:   {plr:.2f}" if plr is not None else "  盈亏比:   ∞（无亏损）")
    print(f"  总信号:   {m['total_signals']}")
    print(f"  总成交:   {m['total_trades']}")
    print(f"  总手续费: {m['total_commission']:,.2f} 元")
    print("=" * 60)

    if result["signals"]:
        print(f"\n--- 套利信号摘要（前10条，共 {len(result['signals'])} 条） ---")
        for sig in result["signals"][:10]:
            action = sig["action"]
            print(
                f"  [{sig['idx']+1}] {sig['ts'][:19]} | {action} | "
                f"K={sig['strike']:.4f} | 净利润={sig['net_profit']:.2f}元"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeltaZero 回测入口（python -m backtest）")
    parser.add_argument("--data-dir", default="sample_data", help="期权 Tick CSV 数据目录（旧模式）")
    parser.add_argument("--market-data-dir", default=None, help="Parquet 根目录（默认 D:\\MARKET_DATA）")
    parser.add_argument("--underlyings", nargs="+", default=None, help="回测品种（如 510050.SH 510300.SH）")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--min-profit", type=float, default=50, help="最小利润阈值（元/组）")
    parser.add_argument("--max-position", type=int, default=10, help="单信号最大组数")
    parser.add_argument("--start-date", default=None, help="回测起始日期（YYYYMMDD 或 YYYY-MM）")
    parser.add_argument("--end-date", default=None, help="回测结束日期（YYYYMMDD 或 YYYY-MM）")
    parser.add_argument("--output-chart", default=None, help="权益曲线图输出路径")
    parser.add_argument("--etf-data-dir", default=None, help="ETF K 线目录（CSV 旧模式）")
    parser.add_argument("--bar-mode", choices=["close", "ohlc"], default="close", help="K 线展开模式（旧模式）")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = get_default_config()
    config.initial_capital = args.capital
    config.min_profit_threshold = args.min_profit
    config.max_position_per_signal = args.max_position

    # 判断模式：如果指定了 --market-data-dir 则走 Parquet 模式
    if args.market_data_dir:
        logger.info("运行模式: Parquet 回测")
        logger.info("初始资金: {:,.0f} 元".format(config.initial_capital))
        run_backtest_parquet(
            config=config,
            market_data_dir=args.market_data_dir,
            underlyings=args.underlyings,
            start_date=args.start_date,
            end_date=args.end_date,
            output_chart=args.output_chart,
        )
    else:
        logger.info("运行模式: CSV 回测（旧模式）")
        logger.info("初始资金: {:,.0f} 元".format(config.initial_capital))
        if args.start_date or args.end_date:
            logger.info("回测日期范围: %s ~ %s", args.start_date or "最早", args.end_date or "最新")
        run_backtest(
            config=config,
            data_dir=args.data_dir,
            start_month=args.start_date,
            end_month=args.end_date,
            output_chart=args.output_chart,
            etf_data_dir=args.etf_data_dir,
            bar_mode=args.bar_mode,
        )


if __name__ == "__main__":
    main()
