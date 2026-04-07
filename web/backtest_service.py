# -*- coding: utf-8 -*-
"""
回测任务执行服务

在后台线程中运行回测，通过回调函数推送进度。
Web API 和 CLI 共用核心逻辑。
"""

from __future__ import annotations

import gc
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.settings import TradingConfig, get_default_config
from models import ArbitrageSignal, ContractInfo, ETFTickData, SignalAction

logger = logging.getLogger(__name__)


class _BacktestCancelled(Exception):
    """回测被用户取消。"""


@dataclass
class BacktestTask:
    """回测任务状态"""
    task_id: str
    status: str = "pending"  # pending | running | done | error | cancelled
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancel_requested: bool = False


@dataclass
class BacktestParams:
    """回测请求参数"""
    underlyings: List[str]
    start_date: str            # YYYYMMDD
    end_date: str              # YYYYMMDD
    initial_capital: float = 1_000_000.0
    min_profit: float = 50.0
    max_position_per_signal: int = 10
    market_data_dir: str = r"D:\MARKET_DATA"
    # 收敛止盈：平仓净利润 >= 此值时强制平仓（0 = 不强制，使用策略默认值）
    close_profit_threshold: float = 0.0
    # 临期规避：距到期日 <= 此天数时无脑平仓（0 = 不启用）
    close_before_dte: int = 0
    # 止损：当前平仓操作会产生的净亏损 >= 此值时强制平仓（0 = 不启用）
    stop_loss_per_set: float = 0.0
    # 信号生成模式：every_tick（每 tick）| etf_tick（仅 ETF tick 时）
    signal_mode: str = "every_tick"
    # ── 开仓质量过滤 ─────────────────────────────────────────────
    min_tolerance_ticks: float = 0.0   # 容错空间下限（tick数），0=不限
    max_spread_ratio: float = 0.0      # 价差率上限（如0.03=3%），0=不限
    min_max_qty: int = 0               # 盘口容量下限（组数），0=不限
    # ── 信号雪崩防护 ─────────────────────────────────────────────
    signal_cooldown_seconds: float = 1.0   # 开仓冷却（秒），0=不限
    max_total_open_sets: int = 0           # 全局最大持仓组数，0=不限
    # ── 交易成本设定 ──────────────────────────────────────────────
    option_commission: float = 1.7      # 期权手续费（元/张，单边）
    etf_commission_rate: float = 0.00006  # ETF 佣金费率（万0.6）
    etf_min_commission: float = 0.1     # ETF 最低佣金（元/笔）
    option_slippage_ticks: int = 1      # 期权滑点（最小变动单位数）
    etf_slippage_ticks: int = 1         # ETF 滑点（最小变动单位数）
    call_margin_ratio: float = 0.12     # 认购保证金比例1（上交所）
    put_margin_ratio: float = 0.12      # 认沽保证金比例1（上交所）
    # 次日开盘强制平仓（T+1 约束下的日内策略模拟）
    close_next_open: bool = False       # True=次日开盘首个有效报价时强制平仓
    # 开仓最小剩余天数（0=不限；建议 3，避免在末日轮阶段新开仓）
    min_dte_for_open: int = 0


# 全局任务注册表
_tasks: Dict[str, BacktestTask] = {}


def get_task(task_id: str) -> Optional[BacktestTask]:
    return _tasks.get(task_id)


class BacktestService:
    """回测执行服务（线程安全，单次执行）"""

    def run(
        self,
        params: BacktestParams,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        task: Optional[BacktestTask] = None,
    ) -> Dict[str, Any]:
        """
        执行回测主流程（在调用线程中同步执行）。

        步骤:
        1. 加载合约信息 (ContractInfoManager)
        2. 发现 Parquet 文件，逐日加载
        3. 构建 Call/Put 配对
        4. 逐日循环: load → aligner.reset() → engine.run() → gc
        5. PnLAnalyzer 分析
        6. 组装结果
        """
        def _progress(msg: Dict[str, Any]) -> None:
            if progress_callback:
                progress_callback(msg)

        def _check_cancelled() -> None:
            """检查是否收到取消请求，抛出异常中断回测。"""
            if task and task.cancel_requested:
                raise _BacktestCancelled()

        # --- Step 0: 抑制回测子模块的 INFO 日志（避免刷屏 console 终端）---
        _noisy_loggers = [
            "backtest.engine", "backtest.broker", "backtest.portfolio",
            "data_engine.tick_data_loader", "data_engine.tick_aligner",
            "data_engine.contract_catalog", "strategies.pcp_arbitrage",
        ]
        _saved_levels = {}
        for name in _noisy_loggers:
            lg = logging.getLogger(name)
            _saved_levels[name] = lg.level
            lg.setLevel(logging.WARNING)

        try:
            return self._run_inner(params, _progress, _check_cancelled, task)
        except _BacktestCancelled:
            logger.info("回测任务已取消")
            raise
        finally:
            # 恢复日志级别
            for name, level in _saved_levels.items():
                logging.getLogger(name).setLevel(level)

    def _run_inner(self, params, _progress, _check_cancelled, task):
        from analysis.pnl import PnLAnalyzer
        from backtest.engine import BacktestEngine, MergedTick
        from data_engine.contract_catalog import ContractInfoManager, get_optionchain_path
        from data_engine.tick_aligner import TickAligner
        from data_engine.tick_data_loader import TickLoader
        from strategies.pcp_arbitrage import PCPArbitrageStrategy

        # --- Step 1: 配置 ---
        config = get_default_config()
        config.initial_capital = params.initial_capital
        config.min_profit_threshold = params.min_profit
        config.max_position_per_signal = params.max_position_per_signal
        # 开仓质量过滤（在 strategy_callback 中应用，无需写 config）
        # 记录到 params 即可，见 strategy_callback 内的 filtered 逻辑
        # 信号雪崩防护
        config.signal_cooldown_seconds = params.signal_cooldown_seconds
        config.max_total_open_sets     = params.max_total_open_sets
        config.min_dte_for_open        = params.min_dte_for_open
        # 交易成本
        config.fee.option_commission_per_contract = params.option_commission
        config.fee.etf_commission_rate            = params.etf_commission_rate
        config.fee.etf_min_commission             = params.etf_min_commission
        config.slippage.option_slippage_ticks     = params.option_slippage_ticks
        config.slippage.etf_slippage_ticks        = params.etf_slippage_ticks
        config.margin.call_margin_ratio_1         = params.call_margin_ratio
        config.margin.put_margin_ratio_1          = params.put_margin_ratio

        _progress({"stage": "init", "message": "正在加载合约信息..."})

        # --- Step 2: 合约信息 ---
        contract_mgr = ContractInfoManager()
        ref_date_str = params.start_date
        ref_date = date(int(ref_date_str[:4]), int(ref_date_str[4:6]), int(ref_date_str[6:8]))
        optionchain_path = get_optionchain_path(target_date=ref_date)

        if optionchain_path.exists():
            count = contract_mgr.load_from_optionchain(optionchain_path, target_date=ref_date)
            logger.info("已从 optionchain 加载 %d 条合约信息", count)
        else:
            raise RuntimeError(f"optionchain 文件不存在: {optionchain_path}")

        # --- Step 3: 发现 Parquet 文件 ---
        _progress({"stage": "discover", "message": "正在扫描 Parquet 文件..."})
        loader = TickLoader()
        root = Path(params.market_data_dir)

        # 收集所有交易日
        trading_dates: List[date] = []
        date_files: Dict[str, Dict[str, List[Path]]] = {}  # date_str -> {underlying -> [opt_path, etf_path]}
        import re
        _date_re = re.compile(r"(\d{8})")

        for underlying in params.underlyings:
            ul_dir = root / underlying.replace(".SH", "")
            if not ul_dir.is_dir():
                logger.warning("品种目录不存在: %s", ul_dir)
                continue

            for fpath in sorted(ul_dir.glob("options_*.parquet")):
                m = _date_re.search(fpath.stem)
                if not m:
                    continue
                fdate = m.group(1)
                if fdate < params.start_date or fdate > params.end_date:
                    continue
                date_files.setdefault(fdate, {}).setdefault(underlying, {})["opt"] = fpath

            for fpath in sorted(ul_dir.glob("etf_*.parquet")):
                m = _date_re.search(fpath.stem)
                if not m:
                    continue
                fdate = m.group(1)
                if fdate < params.start_date or fdate > params.end_date:
                    continue
                date_files.setdefault(fdate, {}).setdefault(underlying, {})["etf"] = fpath

        sorted_date_strs = sorted(date_files.keys())
        total_dates = len(sorted_date_strs)

        if total_dates == 0:
            raise RuntimeError(f"未找到 {params.start_date}~{params.end_date} 范围内的 Parquet 文件")

        _progress({"stage": "discover", "message": f"发现 {total_dates} 个交易日"})

        # --- Step 4: 构建配对（只保留 Parquet 中实际出现过的合约）---
        # 先扫描所有日期文件，收集出现过的合约代码集合，避免扫描 optionchain 里有
        # 但数据库里无 tick 的到期月（会导致每 tick 做无效扫描）
        _present_codes: set = set()
        for _fdate, _underlying_paths in date_files.items():
            for _underlying, _paths in _underlying_paths.items():
                if "opt" in _paths:
                    try:
                        import pyarrow.parquet as _pq
                        _pf = _pq.read_table(_paths["opt"], columns=["code"])
                        _present_codes.update(_pf["code"].to_pylist())
                    except Exception:
                        pass

        all_pairs: List[Tuple[ContractInfo, ContractInfo]] = []
        underlying_codes = set(params.underlyings)
        for underlying in underlying_codes:
            expiries = contract_mgr.get_available_expiries(underlying)
            for expiry in expiries:
                pairs = contract_mgr.find_call_put_pairs(underlying, expiry=expiry)
                # 过滤：至少 call 或 put 之一在 Parquet 里有 tick 数据
                active = [
                    (c, p) for c, p in pairs
                    if c.contract_code in _present_codes or p.contract_code in _present_codes
                ]
                all_pairs.extend(active)

        logger.info("找到 %d 组 Call/Put 配对（已过滤无数据到期月）", len(all_pairs))

        # --- Step 5: 构建引擎 ---
        aligner = TickAligner()
        config.min_profit_threshold = params.min_profit
        # 收敛止盈阈值：透传给策略的 close_profit_threshold
        pcp_strategy = PCPArbitrageStrategy(
            config, close_profit_threshold=params.close_profit_threshold
        )
        pcp_strategy.set_pairs(all_pairs)
        engine = BacktestEngine(config)

        # 日内 tick 计数器（list 以便闭包可写）
        _tick_ctr = [0]
        _day_state: Dict[str, Any] = {
            "date_str": "",
            "day_idx": 0,
            "day_tick_total": 0,
        }

        etf_tick_mode = (params.signal_mode == "etf_tick")

        def strategy_callback(mtick: MergedTick, bt_engine: BacktestEngine) -> List[ArbitrageSignal]:
            if mtick.tick_type == "option" and mtick.option_tick is not None:
                aligner.update_option(mtick.option_tick)
            elif mtick.tick_type == "etf" and mtick.etf_tick is not None:
                aligner.update_etf(mtick.etf_tick)

            # ETF tick 模式：只在 ETF 价格跳动时生成信号
            if etf_tick_mode and mtick.tick_type != "etf":
                _tick_ctr[0] += 1
                if _tick_ctr[0] % 1000 == 0 and task and task.cancel_requested:
                    raise _BacktestCancelled()
                if _tick_ctr[0] % 5000 == 0:
                    day_total = _day_state["day_tick_total"]
                    pct_day = round(_tick_ctr[0] / day_total * 100, 1) if day_total else 0
                    _progress({
                        "stage": "running",
                        "current_date": _day_state["date_str"],
                        "done_dates": _day_state["day_idx"],
                        "total_dates": total_dates,
                        "ticks_processed": total_ticks_processed + _tick_ctr[0],
                        "day_ticks_done": _tick_ctr[0],
                        "day_tick_total": day_total,
                        "pct_day": pct_day,
                        "message": f"回测 {_day_state['date_str']}... {_tick_ctr[0]:,}/{day_total:,} ({pct_day:.0f}%)",
                        "day_phase": "engine",
                    })
                return []

            # 传入 tick 时间戳，确保信号 ts 反映历史时间（而非系统当前时间）
            snapshot = aligner.snapshot(mtick.timestamp)
            signals = pcp_strategy.generate_signals(snapshot)

            # ── 开仓质量过滤（只对 OPEN 信号生效）──────────────────
            if params.min_tolerance_ticks > 0 or params.max_spread_ratio > 0 or params.min_max_qty > 0:
                filtered = []
                for sig in signals:
                    if sig.action != SignalAction.OPEN:
                        filtered.append(sig)
                        continue
                    if params.min_tolerance_ticks > 0:
                        tol = getattr(sig, "tolerance", None)
                        if tol is None or tol < params.min_tolerance_ticks:
                            continue
                    if params.max_spread_ratio > 0:
                        sr = getattr(sig, "spread_ratio", None)
                        if sr is not None and sr > params.max_spread_ratio:
                            continue
                    if params.min_max_qty > 0:
                        mq = getattr(sig, "max_qty", None)
                        if mq is not None and mq < params.min_max_qty:
                            continue
                    filtered.append(sig)
                signals = filtered

            _tick_ctr[0] += 1
            ctr = _tick_ctr[0]

            # --- 每 5000 tick 推送一次日内进度 ---
            if ctr % 5000 == 0:
                day_total = _day_state["day_tick_total"]
                pct_day = round(ctr / day_total * 100, 1) if day_total else 0
                _progress({
                    "stage": "running",
                    "current_date": _day_state["date_str"],
                    "done_dates": _day_state["day_idx"],
                    "total_dates": total_dates,
                    "ticks_processed": total_ticks_processed + ctr,
                    "day_ticks_done": ctr,
                    "day_tick_total": day_total,
                    "pct_day": pct_day,
                    "message": f"回测 {_day_state['date_str']}... {ctr:,}/{day_total:,} ({pct_day:.0f}%)",
                    "day_phase": "engine",
                })

            # --- 每 1000 tick 检查一次取消请求 ---
            if ctr % 1000 == 0 and task and task.cancel_requested:
                raise _BacktestCancelled()

            # --- 止损平仓（基于当前快照的平仓净利润）---
            if params.stop_loss_per_set > 0:
                extra = BacktestService._generate_stop_loss_signals(
                    bt_engine, snapshot, params.stop_loss_per_set, mtick.timestamp
                )
                signals.extend(extra)

            # --- 临期规避平仓 ---
            if params.close_before_dte > 0:
                current_date_inner = mtick.timestamp.date()
                extra = BacktestService._generate_dte_close_signals(
                    bt_engine, snapshot, current_date_inner,
                    params.close_before_dte, mtick.timestamp
                )
                signals.extend(extra)

            # --- 次日开盘强制平仓（T+1 约束下的日内策略）---
            if params.close_next_open:
                extra = BacktestService._generate_next_open_close_signals(
                    bt_engine, snapshot, mtick.timestamp.date(),
                    _day_state["next_open_closed_pairs"], mtick.timestamp,
                )
                signals.extend(extra)

            return signals

        # --- Step 6: 逐日回测 ---
        total_ticks_processed = 0
        prev_close: Dict[str, float] = {}  # Fix 2: 跨日保留各品种前收盘价（交易所保证金基价）

        for day_idx, date_str in enumerate(sorted_date_strs):
            _check_cancelled()
            _tick_ctr[0] = 0  # 每日重置

            trade_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
            trading_dates.append(trade_date)

            # 加载当日数据
            _progress({
                "stage": "running",
                "current_date": date_str,
                "done_dates": day_idx,
                "total_dates": total_dates,
                "ticks_processed": total_ticks_processed,
                "message": f"正在加载 {date_str} 数据...",
                "day_phase": "loading",
            })

            day_option_ticks: Dict[str, List] = {}
            day_etf_ticks: List[ETFTickData] = []
            day_last_prices: Dict[str, float] = {}  # Fix 2: 当日各品种最后一个 ETF 价格

            for underlying, paths in date_files[date_str].items():
                if "opt" in paths:
                    opt_ticks = loader.load_option_parquet(paths["opt"])
                    for tick in opt_ticks:
                        day_option_ticks.setdefault(tick.contract_code, []).append(tick)

                if "etf" in paths:
                    etf_ticks = loader.load_etf_parquet(paths["etf"])
                    day_etf_ticks.extend(etf_ticks)
                    if etf_ticks:
                        day_last_prices[underlying] = etf_ticks[-1].price

            day_etf_ticks.sort(key=lambda t: t.timestamp)
            day_tick_count = sum(len(v) for v in day_option_ticks.values()) + len(day_etf_ticks)
            total_ticks_processed += day_tick_count

            # 更新日内状态供 strategy_callback 读取
            _day_state["date_str"] = date_str
            _day_state["day_idx"] = day_idx
            _day_state["day_tick_total"] = day_tick_count
            _day_state["next_open_closed_pairs"] = set()  # 每日重置，记录当日已生成次日平仓信号的配对

            # 跨日重置 aligner，防止前日 LKV 污染
            aligner.reset()

            # 匹配当日合约信息
            contracts: Dict[str, ContractInfo] = {}
            for code in day_option_ticks:
                info = contract_mgr.get_info(code)
                if info is not None:
                    contracts[code] = info

            if not contracts:
                logger.warning("日期 %s: 无合约匹配，跳过", date_str)
                del day_option_ticks, day_etf_ticks
                gc.collect()
                continue

            # Fix 2: 优先使用前日收盘价（无则退化为当日首 tick，首日警告）
            if prev_close:
                underlying_close = next(iter(prev_close.values()))  # 取任一品种的前收（单品种最常见）
            elif day_etf_ticks:
                underlying_close = day_etf_ticks[0].price
                logger.warning("日期 %s 无前收盘价，回退为当日首 tick: %.4f", date_str, underlying_close)
            else:
                underlying_close = 3.0

            _progress({
                "stage": "running",
                "current_date": date_str,
                "done_dates": day_idx,
                "total_dates": total_dates,
                "ticks_processed": total_ticks_processed,
                "message": f"正在回测 {date_str}... ({day_tick_count:,} ticks)",
                "day_phase": "engine",
            })

            # 执行当日回测
            engine.run(
                option_ticks=day_option_ticks,
                etf_ticks=day_etf_ticks,
                contracts=contracts,
                strategy_callback=strategy_callback,
                underlying_close=underlying_close,
                prev_close=prev_close if prev_close else None,
            )

            logger.info(
                "日期 %s: %d ticks, 累计信号 %d, 累计成交 %d",
                date_str, day_tick_count,
                len(engine.signals_generated), len(engine.portfolio.trade_history),
            )

            # Fix 2: 更新前收盘价（下一交易日保证金基价）
            prev_close.update(day_last_prices)

            # 显式释放当日数据，防 Pandas C 内存泄漏
            del day_option_ticks, day_etf_ticks, contracts, day_last_prices
            gc.collect()

        # --- Fix 3: 回测结束强平残余头寸 ---
        open_positions = {k: v for k, v in engine.portfolio.positions.items() if v.quantity != 0}
        if open_positions:
            last_ts = engine.equity_curve[-1][0] if engine.equity_curve else datetime.now()
            logger.info("回测结束，发现 %d 个未平仓位，开始强平...", len(open_positions))
            BacktestService._force_liquidate_all(engine, config, last_ts)

        # --- Step 7: PnL 分析 ---
        _progress({
            "stage": "analyzing",
            "message": "正在分析回测结果...",
            "done_dates": total_dates,
            "total_dates": total_dates,
            "ticks_processed": total_ticks_processed,
        })

        analyzer = PnLAnalyzer(
            risk_free_rate=config.risk_free_rate,
            trading_days_per_year=config.trading_days_per_year,
        )
        metrics = analyzer.analyze(
            trade_history=engine.portfolio.trade_history,
            signals=engine.signals_generated,
            equity_curve=engine.equity_curve,
            initial_capital=config.initial_capital,
        )

        # --- Step 8: 组装结果 ---
        result = self._build_result(
            engine=engine,
            metrics=metrics,
            params=params,
            trading_dates=trading_dates,
            total_ticks=total_ticks_processed,
        )

        _progress({
            "stage": "done",
            "message": "回测完成",
            "done_dates": total_dates,
            "total_dates": total_dates,
            "ticks_processed": total_ticks_processed,
        })

        return result

    @staticmethod
    def _make_close_signal(
        engine: Any, snapshot: Any, call_code: str, put_code: str,
        underlying: str, ts: Any,
    ) -> Optional[Any]:
        """
        从当前快照构造一个 CLOSE 信号。
        利用 snapshot 的买一价（平仓方向）填充字段。
        """
        from models.order import ArbitrageSignal, SignalAction

        call_pos = engine.portfolio.positions.get(call_code)
        put_pos  = engine.portfolio.positions.get(put_code)
        etf_pos  = engine.portfolio.positions.get(underlying)
        if not (call_pos and call_pos.quantity < 0
                and put_pos and put_pos.quantity > 0
                and etf_pos and etf_pos.quantity > 0):
            return None

        call_info = engine.contracts.get(call_code)
        if call_info is None:
            return None

        call_ask1 = snapshot.option_ask1(call_code)
        put_bid1  = snapshot.option_bid1(put_code)
        etf_bid1  = snapshot.etf_bid1(underlying)

        if not call_ask1 or not put_bid1 or not etf_bid1:
            return None

        mult = call_info.contract_unit
        K    = call_info.strike_price

        # 粗估平仓净利润（简化费用）
        close_per_share = etf_bid1 + put_bid1 - call_ask1 - K
        close_net = close_per_share * mult

        return ArbitrageSignal(
            ts=ts,
            action=SignalAction.CLOSE,
            direction=1,
            underlying=underlying,
            call_code=call_code,
            put_code=put_code,
            expiry=call_info.expiry_date,
            strike=K,
            net_profit=round(close_net, 2),
            # CLOSE 语义：各字段明确对应平仓方向盘口
            etf_bid=etf_bid1,
            put_bid=put_bid1,
            call_ask=call_ask1,
            etf_ask=etf_bid1,   # 展示兜底
            put_ask=put_bid1,   # 展示兜底
            call_bid=call_ask1, # 展示兜底
            multiplier=mult,
            max_qty=None,
        )

    @staticmethod
    def _generate_stop_loss_signals(
        engine: Any, snapshot: Any, stop_loss_per_set: float, ts: Any,
    ) -> List[Any]:
        """
        止损：若现在平仓会产生净亏损 >= stop_loss_per_set，生成 CLOSE 信号。
        （净亏损 = 开仓时已付出成本 - 当前平仓所得，用持仓均价近似）
        """
        from models.order import SignalAction
        signals = []
        seen_pairs: set = set()

        for code, pos in list(engine.portfolio.positions.items()):
            if pos.quantity >= 0:  # 只看 Call 空头（卖出开仓腿）
                continue
            call_info = engine.contracts.get(code)
            if call_info is None:
                continue

            underlying = call_info.underlying_code
            # 找对应 Put 多头（同标的、同到期、同行权价）
            put_code = None
            for pcode, ppos in engine.portfolio.positions.items():
                pi = engine.contracts.get(pcode)
                if (pi and pi.underlying_code == underlying
                        and pi.expiry_date == call_info.expiry_date
                        and abs(pi.strike_price - call_info.strike_price) < 1e-6
                        and pi.option_type.value == "put"
                        and ppos.quantity > 0):
                    put_code = pcode
                    break
            if put_code is None:
                continue

            pair_key = (code, put_code)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            sig = BacktestService._make_close_signal(
                engine, snapshot, code, put_code, underlying, ts
            )
            if sig is None:
                continue

            # stop_loss_per_set 触发条件：平仓净利润过负
            if sig.net_profit < -stop_loss_per_set:
                signals.append(sig)

        return signals

    @staticmethod
    def _generate_next_open_close_signals(
        engine: Any, snapshot: Any, current_date: Any,
        done_pairs: set, ts: Any,
    ) -> List[Any]:
        """
        次日开盘强制平仓：对所有 T+1 可平仓的持仓，在首个有效报价时生成 CLOSE 信号。

        T+1 判断：ETF 买入日期 < current_date（昨日或更早买入，今日可卖）。
        done_pairs 记录当日已生成过 CLOSE 信号的配对，避免重复触发。
        """
        signals = []

        for code, pos in list(engine.portfolio.positions.items()):
            if pos.quantity >= 0:  # 只看 Call 空头（卖出腿）
                continue
            call_info = engine.contracts.get(code)
            if call_info is None:
                continue

            underlying = call_info.underlying_code

            # T+1 检查：ETF 必须是昨日或更早买入
            etf_buy_date = engine.portfolio._etf_buy_dates.get(underlying)
            if etf_buy_date is None or etf_buy_date >= current_date:
                continue  # 今日买入或无 ETF 持仓，不可平

            # 找同到期日同行权价的 Put 多头
            put_code = None
            for pcode, ppos in engine.portfolio.positions.items():
                pi = engine.contracts.get(pcode)
                if (pi and pi.underlying_code == underlying
                        and pi.expiry_date == call_info.expiry_date
                        and abs(pi.strike_price - call_info.strike_price) < 1e-6
                        and pi.option_type.value == "put"
                        and ppos.quantity > 0):
                    put_code = pcode
                    break
            if put_code is None:
                continue

            pair_key = (code, put_code)
            if pair_key in done_pairs:
                continue  # 今日已生成过该配对的平仓信号

            sig = BacktestService._make_close_signal(
                engine, snapshot, code, put_code, underlying, ts
            )
            if sig is not None:
                done_pairs.add(pair_key)
                signals.append(sig)

        return signals

    @staticmethod
    def _generate_dte_close_signals(
        engine: Any, snapshot: Any, current_date: Any,
        close_before_dte: int, ts: Any,
    ) -> List[Any]:
        """临期规避：距到期日 <= close_before_dte 天时生成强制 CLOSE 信号。"""
        from datetime import timedelta
        signals = []
        seen_pairs: set = set()

        for code, pos in list(engine.portfolio.positions.items()):
            if pos.quantity >= 0:
                continue
            call_info = engine.contracts.get(code)
            if call_info is None:
                continue

            dte = (call_info.expiry_date - current_date).days
            if dte > close_before_dte:
                continue

            underlying = call_info.underlying_code
            put_code = None
            for pcode, ppos in engine.portfolio.positions.items():
                pi = engine.contracts.get(pcode)
                if (pi and pi.underlying_code == underlying
                        and pi.expiry_date == call_info.expiry_date
                        and abs(pi.strike_price - call_info.strike_price) < 1e-6
                        and pi.option_type.value == "put"
                        and ppos.quantity > 0):
                    put_code = pcode
                    break
            if put_code is None:
                continue

            pair_key = (code, put_code)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            sig = BacktestService._make_close_signal(
                engine, snapshot, code, put_code, underlying, ts
            )
            if sig is not None:
                signals.append(sig)

        return signals

    @staticmethod
    def _force_liquidate_all(engine: Any, config: Any, last_ts: Any) -> None:
        """
        Fix 3: 强平所有未平 PCP 组合（回测结束时调用）。

        绕过 T+1 和 Broker FOK，直接构造 TradeRecord 写入 Portfolio。
        按架构师要求：扣除单边平仓手续费和滑点，不能零成本平仓。
        """
        from models import AssetType, OrderSide, TradeRecord, ArbitrageSignal, SignalAction

        portfolio = engine.portfolio
        price_cache = engine._price_cache
        fee = config.fee
        slp = config.slippage
        seen_calls: set = set()

        for call_code, call_pos in list(portfolio.positions.items()):
            if call_pos.quantity >= 0 or call_code in seen_calls:
                continue

            call_info = engine.contracts.get(call_code)
            if call_info is None:
                continue

            underlying = call_info.underlying_code

            # 找同标的、同到期日、同行权价的 Put 多头
            put_code = None
            for pcode, ppos in portfolio.positions.items():
                pi = engine.contracts.get(pcode)
                if (pi and pi.underlying_code == underlying
                        and pi.expiry_date == call_info.expiry_date
                        and abs(pi.strike_price - call_info.strike_price) < 1e-6
                        and pi.option_type.value == "put"
                        and ppos.quantity > 0):
                    put_code = pcode
                    break
            if put_code is None:
                continue

            etf_pos = portfolio.positions.get(underlying)
            if not etf_pos or etf_pos.quantity <= 0:
                continue

            unit = call_info.contract_unit
            num_sets = min(
                abs(call_pos.quantity),
                portfolio.positions[put_code].quantity,
                etf_pos.quantity // unit,
            )
            if num_sets <= 0:
                continue

            # 取最后已知价格（无则退化为持仓均价）
            call_price = price_cache.get(call_code, call_pos.avg_cost)
            put_price  = price_cache.get(put_code, portfolio.positions[put_code].avg_cost)
            etf_price  = price_cache.get(underlying, etf_pos.avg_cost)

            # 平仓方向滑点（卖出 ETF/Put 向下，买入 Call 向上）
            etf_exec  = max(etf_price  - slp.etf_slippage_ticks    * slp.etf_tick_size,    0.0001)
            put_exec  = max(put_price  - slp.option_slippage_ticks * slp.option_tick_size, 0.0001)
            call_exec = max(call_price + slp.option_slippage_ticks * slp.option_tick_size, 0.0001)

            # 单边手续费（平仓）
            etf_quantity = num_sets * unit
            etf_comm  = max(etf_exec * etf_quantity * fee.etf_commission_rate, fee.etf_min_commission)
            put_comm  = fee.option_commission_per_contract * num_sets
            call_comm = fee.option_commission_per_contract * num_sets

            # 滑点成本
            etf_slip  = slp.etf_slippage_ticks    * slp.etf_tick_size    * etf_quantity
            opt_slip  = slp.option_slippage_ticks * slp.option_tick_size * unit * num_sets

            # 构造强平信号（仅用于 signal_id 关联，供 _build_result 统计 actual_pnl）
            sig_idx = len(engine.signals_generated)
            fl_signal = ArbitrageSignal(
                ts=last_ts,
                action=SignalAction.CLOSE,
                direction=-1,
                underlying=underlying,
                call_code=call_code,
                put_code=put_code,
                expiry=call_info.expiry_date,
                strike=call_info.strike_price,
                net_profit=0.0,
                etf_bid=etf_exec,
                put_bid=put_exec,
                call_ask=call_exec,
                etf_ask=etf_price,
                multiplier=unit,
                calc_detail="[FORCE_LIQUIDATION]",
            )
            engine.signals_generated.append(fl_signal)

            trades = [
                TradeRecord(
                    trade_id=0,
                    timestamp=last_ts,
                    asset_type=AssetType.ETF,
                    contract_code=underlying,
                    side=OrderSide.SELL,
                    price=etf_exec,
                    quantity=etf_quantity,
                    commission=etf_comm,
                    slippage_cost=etf_slip,
                    signal_id=sig_idx,
                    direction=-1,
                    multiplier=1,
                    margin_reserved=0.0,
                ),
                TradeRecord(
                    trade_id=0,
                    timestamp=last_ts,
                    asset_type=AssetType.OPTION,
                    contract_code=put_code,
                    side=OrderSide.SELL,
                    price=put_exec,
                    quantity=num_sets,
                    commission=put_comm,
                    slippage_cost=opt_slip,
                    signal_id=sig_idx,
                    direction=-1,
                    multiplier=unit,
                    margin_reserved=0.0,
                ),
                TradeRecord(
                    trade_id=0,
                    timestamp=last_ts,
                    asset_type=AssetType.OPTION,
                    contract_code=call_code,
                    side=OrderSide.BUY,
                    price=call_exec,
                    quantity=num_sets,
                    commission=call_comm,
                    slippage_cost=opt_slip,
                    signal_id=sig_idx,
                    direction=1,
                    multiplier=unit,
                    margin_reserved=0.0,
                ),
            ]

            portfolio.process_trades(trades)
            seen_calls.add(call_code)
            logger.info(
                "强平: %s Strike=%.4f 组数=%d ETF=%.4f Put=%.4f Call=%.4f",
                call_code, call_info.strike_price, num_sets, etf_exec, put_exec, call_exec,
            )

    def _build_result(
        self,
        engine: Any,
        metrics: Any,
        params: BacktestParams,
        trading_dates: List[date],
        total_ticks: int,
    ) -> Dict[str, Any]:
        """将引擎结果组装为 JSON-serializable 字典。"""
        # 权益曲线
        equity_curve = [
            {"ts": ts.isoformat(), "equity": round(eq, 2)}
            for ts, eq in engine.equity_curve
        ]

        # 只保留已成交信号（signal_id 在 trade_history 中出现过）
        from models import AssetType as _AT, OrderSide as _OS

        # 按 signal_id 预聚合成交记录，避免 O(n²) 扫描
        trades_by_signal: Dict[int, list] = {}
        for t in engine.portfolio.trade_history:
            if t.signal_id is not None:
                trades_by_signal.setdefault(t.signal_id, []).append(t)

        executed_ids = set(trades_by_signal.keys())

        def _r(v, digits=2):
            return round(v, digits) if v is not None else None

        def _cash_pnl(trades_list) -> Optional[float]:
            """
            核心资金流公式（OPEN+CLOSE 成交合并传入才有意义）：
              cash_flow = Σ price × qty × multiplier × (BUY=+1 / SELL=-1)
              net_pnl   = -(cash_flow + commission + slippage)
            单独传入 CLOSE 成交会返回"本金回收"（虚高），必须与对应 OPEN 合并使用。
            """
            if not trades_list:
                return None
            cash_flow  = sum(t.price * t.quantity * t.multiplier *
                             (1 if t.side == _OS.BUY else -1)
                             for t in trades_list)
            commission = sum(t.commission for t in trades_list)
            slippage   = sum(t.slippage_cost for t in trades_list)
            return round(-(cash_flow + commission + slippage), 2)

        # 为往返利润计算：建立 (call_code, put_code) → [执行OPEN的idx] 映射
        _open_by_pair: Dict[tuple, list] = {}
        for _idx, _sig in enumerate(engine.signals_generated):
            if _sig.action == SignalAction.OPEN and _idx in executed_ids:
                _pair = (getattr(_sig, "call_code", ""), getattr(_sig, "put_code", ""))
                _open_by_pair.setdefault(_pair, []).append(_idx)

        def _sets_for_trades(trades_list) -> int:
            """提取成交列表中的期权组数（BUY方向期权数量 = 组数）"""
            return next(
                (t.quantity for t in trades_list
                 if t.asset_type == _AT.OPTION and t.direction == 1),
                0,
            ) or 0

        def _roundtrip_pnl(close_idx: int, close_sig) -> Optional[float]:
            """
            往返净利润：按本次平仓组数占全部开仓组数的比例，分摊 OPEN 成本。

            处理分批平仓：若 24 组分 4 次平仓（4+2+2+16），每次只摊取当次
            比例的 OPEN 成本，避免将未平仓部分的 ETF 本金计入亏损。
            """
            pair   = (getattr(close_sig, "call_code", ""), getattr(close_sig, "put_code", ""))
            priors = [j for j in _open_by_pair.get(pair, []) if j < close_idx]
            if not priors:
                return None

            open_trades  = [t for idx in priors for t in trades_by_signal.get(idx, [])]
            close_trades = trades_by_signal.get(close_idx, [])

            open_sets  = sum(_sets_for_trades(trades_by_signal.get(oi, [])) for oi in priors)
            close_sets = _sets_for_trades(close_trades)
            if open_sets == 0:
                return None
            ratio = close_sets / open_sets  # 本次平仓占全部开仓的比例

            open_cf   = sum(t.price * t.quantity * t.multiplier *
                            (1 if t.side == _OS.BUY else -1) for t in open_trades)
            open_fee  = sum(t.commission    for t in open_trades)
            open_slip = sum(t.slippage_cost for t in open_trades)

            close_cf   = sum(t.price * t.quantity * t.multiplier *
                             (1 if t.side == _OS.BUY else -1) for t in close_trades)
            close_fee  = sum(t.commission    for t in close_trades)
            close_slip = sum(t.slippage_cost for t in close_trades)

            total_cf  = ratio * open_cf  + close_cf
            total_fee = ratio * open_fee + close_fee
            # 注意：slippage_cost 已内嵌在执行价格中，不能再次相加（否则双重计算）
            return round(-(total_cf + total_fee), 2)

        signals = []
        for i, sig in enumerate(engine.signals_generated):
            if i not in executed_ids:
                continue
            sig_trades    = trades_by_signal.get(i, [])
            opt_trade     = next((t for t in sig_trades if t.asset_type == _AT.OPTION and t.direction == 1), None)
            executed_sets = opt_trade.quantity if opt_trade else None
            is_close      = (sig.action == SignalAction.CLOSE)
            actual_pnl    = _roundtrip_pnl(i, sig) if is_close else None

            signals.append({
                "idx": i,
                "ts": sig.ts.isoformat() if sig.ts else "",
                "action": sig.action.value if hasattr(sig.action, "value") else str(sig.action),
                "direction": sig.direction,
                "underlying": getattr(sig, "underlying", ""),
                "strike": getattr(sig, "strike", 0),
                "expiry": str(getattr(sig, "expiry", "")),
                "net_profit": _r(getattr(sig, "net_profit", 0)),
                "actual_pnl": actual_pnl,   # 实际净利润（仅 CLOSE 有值）
                "executed_sets": executed_sets,
                "call_bid": _r(getattr(sig, "call_bid", 0), 4),
                "put_ask": _r(getattr(sig, "put_ask", 0), 4),
                "etf_ask": _r(getattr(sig, "etf_ask", 0), 4),
                "multiplier": getattr(sig, "multiplier", 10000),
                # 信号质量指标
                "max_qty": getattr(sig, "max_qty", None),
                "spread_ratio": _r(getattr(sig, "spread_ratio", None), 4),
                "net_1tick": _r(getattr(sig, "net_1tick", None)),
                "tolerance": _r(getattr(sig, "tolerance", None), 1),
            })

        # 逐笔成交记录
        trades = []
        for t in engine.portfolio.trade_history:
            trades.append({
                "trade_id": t.trade_id,
                "ts": t.timestamp.isoformat() if t.timestamp else "",
                "asset_type": t.asset_type.value if hasattr(t.asset_type, "value") else str(t.asset_type),
                "contract_code": t.contract_code,
                "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                "direction": t.direction,
                "price": round(t.price, 6),
                "quantity": t.quantity,
                "multiplier": t.multiplier,
                "commission": round(t.commission, 4),
                "slippage_cost": round(t.slippage_cost, 4),
                "signal_id": t.signal_id,
            })

        # 持仓快照
        positions = []
        for code, pos in engine.portfolio.positions.items():
            if pos.quantity == 0:
                continue
            positions.append({
                "code": code,
                "quantity": pos.quantity,
                "avg_price": round(pos.avg_cost, 4),
                "realized_pnl": round(pos.realized_pnl, 2),
                "margin_occupied": round(pos.margin_occupied, 2),
            })

        # ── 四项补充指标 ──────────────────────────────────────────
        # 获取已执行的 CLOSE 信号列表
        _close_executed = [
            (i, sig) for i, sig in enumerate(engine.signals_generated)
            if sig.action == SignalAction.CLOSE and i in executed_ids
        ]

        # 1. 强平占比
        _fl_count = sum(
            1 for _, s in _close_executed
            if "[FORCE_LIQUIDATION]" in getattr(s, "calc_detail", "")
        )
        force_liq_ratio = round(_fl_count / len(_close_executed) * 100, 1) if _close_executed else None

        # 2. 实际捕获率：往返净利润 / 本次平仓对应的开仓预期净利润
        #    分批平仓时，分母同样按 close_sets/open_sets 比例缩放，与分子对齐
        _capture_ratios = []
        for i, sig in _close_executed:
            pair   = (getattr(sig, "call_code", ""), getattr(sig, "put_code", ""))
            priors = [j for j in _open_by_pair.get(pair, []) if j < i]
            if not priors:
                continue
            open_sets  = sum(_sets_for_trades(trades_by_signal.get(oi, [])) for oi in priors)
            close_sets = _sets_for_trades(trades_by_signal.get(i, []))
            if open_sets == 0:
                continue
            ratio = close_sets / open_sets
            rt = _roundtrip_pnl(i, sig)
            if rt is None:
                continue
            # 理论总利润 × 本次平仓比例 = 本次期望捕获的目标利润
            theo_total = sum(
                (getattr(engine.signals_generated[oi], "net_profit", 0) or 0) *
                (_sets_for_trades(trades_by_signal.get(oi, [])))
                for oi in priors
            )
            theo_for_this = theo_total * ratio
            if theo_for_this > 0:
                _capture_ratios.append(rt / theo_for_this)
        avg_capture_rate = round(
            sum(_capture_ratios) / len(_capture_ratios) * 100, 1
        ) if _capture_ratios else None

        # 3. 平均持仓时长（小时）：对每个 CLOSE 找最近的同配对 OPEN
        _open_ts: Dict[tuple, list] = {}
        for i, sig in enumerate(engine.signals_generated):
            if sig.action == SignalAction.OPEN and i in executed_ids:
                pair = (getattr(sig, "call_code", ""), getattr(sig, "put_code", ""))
                _open_ts.setdefault(pair, []).append(sig.ts)
        _holding_secs = []
        for _, sig in _close_executed:
            pair    = (getattr(sig, "call_code", ""), getattr(sig, "put_code", ""))
            priors  = [ts for ts in _open_ts.get(pair, []) if ts <= sig.ts]
            if priors:
                _holding_secs.append((sig.ts - max(priors)).total_seconds())
        avg_holding_hours = round(
            sum(_holding_secs) / len(_holding_secs) / 3600, 2
        ) if _holding_secs else None

        # 4. Kelly 仓位建议（正值=有正期望；负值=无套利边际，不应开仓）
        _wr  = metrics.win_rate          # 0~1
        _plr = metrics.profit_loss_ratio # avg_win / avg_loss（None = 无亏损 = ∞）
        if _plr is None:
            # 历史全胜：Kelly = wr（plr → ∞ 时 Kelly → wr）
            kelly_fraction = round(_wr * 100, 1) if _wr > 0 else None
        elif _plr > 0:
            kelly_fraction = round((_wr * (_plr + 1) - 1) / _plr * 100, 1)
        else:
            kelly_fraction = None

        # 核心指标
        metrics_dict = {
            "total_pnl": round(metrics.total_pnl, 2),
            "total_return": round(metrics.total_return * 100, 2),
            "annualized_return": round(metrics.annualized_return * 100, 2),
            "max_drawdown": round(metrics.max_drawdown, 2),
            "max_drawdown_pct": round(metrics.max_drawdown_pct * 100, 2),
            "win_rate": round(metrics.win_rate * 100, 1),
            "profit_loss_ratio": round(_plr, 2) if _plr is not None else None,
            "sharpe_ratio": round(metrics.sharpe_ratio, 2),
            "total_trades": metrics.total_trades,
            "total_signals": metrics.total_signals,
            "total_commission": round(metrics.total_commission, 2),
            "trading_days": metrics.trading_days,
            # 补充指标
            "force_liq_ratio": force_liq_ratio,      # 强平占比 (%)
            "avg_capture_rate": avg_capture_rate,    # 实际捕获率 (%)
            "avg_holding_hours": avg_holding_hours,  # 平均持仓时长 (小时)
            "kelly_fraction": kelly_fraction,        # Kelly 建议仓位 (%)
        }

        return {
            "metrics": metrics_dict,
            "equity_curve": equity_curve,
            "signals": signals,
            "trades": trades,
            "positions": positions,
            "params": {
                # 基本
                "underlyings":             params.underlyings,
                "start_date":              params.start_date,
                "end_date":                params.end_date,
                "initial_capital":         params.initial_capital,
                # 信号过滤
                "min_profit":              params.min_profit,
                "max_position_per_signal": params.max_position_per_signal,
                "min_tolerance_ticks":     params.min_tolerance_ticks,
                "max_spread_ratio":        params.max_spread_ratio,
                "min_max_qty":             params.min_max_qty,
                # 平仓控制
                "close_profit_threshold":  params.close_profit_threshold,
                "close_before_dte":        params.close_before_dte,
                "stop_loss_per_set":       params.stop_loss_per_set,
                "close_next_open":         params.close_next_open,
                # 雪崩防护
                "signal_cooldown_seconds": params.signal_cooldown_seconds,
                "max_total_open_sets":     params.max_total_open_sets,
                "signal_mode":             params.signal_mode,
                # 交易成本
                "option_commission":       params.option_commission,
                "etf_commission_rate":     params.etf_commission_rate,
                "etf_min_commission":      params.etf_min_commission,
                "option_slippage_ticks":   params.option_slippage_ticks,
                "etf_slippage_ticks":      params.etf_slippage_ticks,
                "call_margin_ratio":       params.call_margin_ratio,
                "put_margin_ratio":        params.put_margin_ratio,
            },
            "trading_dates": [d.isoformat() for d in trading_dates],
            "total_ticks": total_ticks,
        }
