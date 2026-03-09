# -*- coding: utf-8 -*-
"""
DeltaZero — PCP 正向套利实时监控

运行方法:
    python monitor.py                          # 从 data_bus 进程读取（默认）
    python monitor.py --min-profit 150         # 净利润阈值
    python monitor.py --expiry-days 30         # 只看近月合约
    python monitor.py --refresh 3              # 每3秒刷新
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Windows UTF-8 修复必须在 rich 之前
from monitors.common import fix_windows_encoding
fix_windows_encoding()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich import box
from rich.console import Console, Group as RenderGroup
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from utils.time_utils import bj_today
from monitors.common import (
    ETF_NAME_MAP,
    ETF_ORDER,
    build_pairs_and_codes,
    estimate_etf_fallback_prices,
    init_strategy_and_contracts,
    parse_zmq_message,
    restore_from_snapshot,
    select_display_pairs,
)
from config.settings import DEFAULT_MARKET_DATA_DIR, UNDERLYINGS
from models import (
    ContractInfo,
    ETFTickData,
    TradeSignal,
)
from calculators.vix_engine import VIXEngine, VIXResult
from calculators.yield_curve import BoundedCubicSplineRate
from strategies.pcp_arbitrage import PCPArbitrage

console = Console(legacy_windows=False, highlight=True)

import logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

# ──────────────────────────────────────────────────────────────────────
# Rich 显示构建
# ──────────────────────────────────────────────────────────────────────

_TRADE_DATE_SET: Optional[set] = None


def _get_trade_date_set() -> set:
    """懒加载 A 股交易日历（akshare），失败时回退到纯工作日计算。"""
    global _TRADE_DATE_SET
    if _TRADE_DATE_SET is None:
        try:
            import akshare as ak
            cal = ak.tool_trade_date_hist_sina()
            _TRADE_DATE_SET = set(cal["trade_date"].tolist())  # datetime.date 对象
        except Exception:
            _TRADE_DATE_SET = set()  # 空集触发回退
    return _TRADE_DATE_SET


def _trading_days_until(expiry: date, today: date) -> int:
    """从 today 到 expiry（含）的 A 股交易日数；无法获取日历时回退到工作日数。"""
    trade_set = _get_trade_date_set()
    count = 0
    d = today
    while d <= expiry:
        if trade_set:
            if d in trade_set:
                count += 1
        else:
            if d.weekday() < 5:
                count += 1
        d += timedelta(days=1)
    return count


_ETF_BORDER = {
    "510050.SH": "bright_cyan",
    "510300.SH": "bright_blue",
    "510500.SH": "bright_magenta",
}

_VIX_TARGETS = list(UNDERLYINGS)


def _build_etf_table(
    underlying: str,
    sigs: List[TradeSignal],
    price: float,
    min_profit: float,
    vix_value: Optional[float] = None,
    *,
    n_pairs: int = 0,
    n_opts: int = 0,
    n_positive: int = 0,
    n_profitable: int = 0,
) -> Panel:
    """为单个品种构建信号面板，每个到期日组显示跨列全宽横幅标题。"""
    u_name = ETF_NAME_MAP.get(underlying.split(".")[0], underlying)
    n_profitable_val = sum(1 for s in sigs if s.net_profit_estimate >= min_profit)
    border = "bright_green" if n_profitable_val > 0 else (_ETF_BORDER.get(underlying, "dim") if sigs else "dim")

    # 面板标题：品种名、价格、VIX
    title_parts = [f"[bold]{u_name}[/bold]"]
    if price > 0:
        title_parts.append(f"[yellow]{price:.4f}[/yellow]")
    if vix_value is not None:
        title_parts.append(f"[magenta]VIX {vix_value:.2f}[/magenta]")
    if not sigs:
        title_parts.append("[dim]暂无数据[/dim]")
    panel_title = "  ".join(title_parts)

    # 面板副标题：统计信息（显示在面板底部边框）
    panel_subtitle = (
        f"[dim]监控配对: {n_pairs} 组  订阅期权: {n_opts} 个  "
        f"有报价: {len(sigs)} 条  正向机会: {n_positive}  (≥{min_profit:.0f}元: {n_profitable})[/dim]"
    )

    def _make_table(show_header: bool) -> Table:
        tbl = Table(
            box=box.SIMPLE,
            show_header=show_header,
            header_style="bold cyan",
            show_edge=False,
            expand=True,
            padding=(0, 0),
        )
        tbl.add_column("行权价", width=6,  justify="left")
        tbl.add_column("方向",   width=4,  justify="center")
        tbl.add_column("净利润", width=7,  justify="right")
        tbl.add_column("Net_1T", width=7,  justify="right")
        tbl.add_column("TOL",    width=5,  justify="right")
        tbl.add_column("Max_Qty", width=5, justify="right")
        tbl.add_column("SPRD",   width=5,  justify="right")
        tbl.add_column("OBI_C",  width=4,  justify="right")
        tbl.add_column("OBI_P",  width=4,  justify="right")
        tbl.add_column("OBI_S",  width=4,  justify="right")
        tbl.add_column("C_b",    width=6,  justify="right")
        tbl.add_column("P_a",    width=6,  justify="right")
        tbl.add_column("S_a",    width=6,  justify="right")
        # 乘数列已移至每组 Rule 标题，此处不再单独列出
        return tbl

    def _add_sig_row(tbl: Table, sig: TradeSignal) -> None:
        profit = sig.net_profit_estimate
        if profit >= min_profit:
            profit_str = f"[bold green]{profit:.0f}[/bold green]"
            dir_str = "[bold green]正向[/bold green]"
        elif profit >= 0:
            profit_str = f"[white]{profit:.0f}[/white]"
            dir_str = "[white]正向[/white]"
        else:
            profit_str = f"[dim]{profit:.0f}[/dim]"
            dir_str = ""
        adj_tag = " [dim]A[/dim]" if sig.is_adjusted else ""
        tbl.add_row(
            f"{sig.strike:.2f}{adj_tag}",
            dir_str,
            profit_str,
            f"{sig.net_1tick:.0f}" if sig.net_1tick is not None else "--",
            f"{sig.tolerance:.2f}" if sig.tolerance is not None else "--",
            f"{sig.max_qty:.2f}" if sig.max_qty is not None else "--",
            f"{sig.spread_ratio * 100:.1f}%" if sig.spread_ratio is not None else "--",
            f"{sig.obi_c:.2f}" if sig.obi_c is not None else "--",
            f"{sig.obi_p:.2f}" if sig.obi_p is not None else "--",
            f"{sig.obi_s:.2f}" if sig.obi_s is not None else "--",
            f"{sig.call_bid:.4f}",
            f"{sig.put_ask:.4f}",
            f"{sig.spot_price:.4f}",
        )

    if not sigs:
        tbl = _make_table(show_header=True)
        tbl.add_row(*["—"] * 13)
        return Panel(tbl, title=panel_title, subtitle=panel_subtitle,
                     border_style=border, expand=True, padding=(0, 0))

    today = bj_today()

    # 按 (到期日, 乘数) 分组——同一到期日的标准合约与调整型合约各为独立组
    by_expiry_mult: Dict[Tuple[date, int], List[TradeSignal]] = defaultdict(list)
    for sig in sigs:
        by_expiry_mult[(sig.expiry, sig.multiplier)].append(sig)

    renderables: List = []

    # 全局共用列名，置于面板最顶部，仅显示一次
    renderables.append(_make_table(show_header=True))

    for (expiry, mult), group in sorted(by_expiry_mult.items()):
        cal_days = (expiry - today).days + 1  # 含今天和到期日两端
        trade_days = _trading_days_until(expiry, today)

        # 居中 Rule 标题，含到期日、自然日、交易日、乘数
        rule_title = (
            f"[bold]{expiry.strftime('%Y-%m-%d')}[/bold]"
            f"  [dim]自然日 {cal_days}天  交易日 {trade_days}天  ×{mult}[/dim]"
        )
        renderables.append(Rule(rule_title, style="cyan"))        # 居中（默认 align="center"）

        data_tbl = _make_table(show_header=False)
        for sig in sorted(group, key=lambda s: s.strike):
            _add_sig_row(data_tbl, sig)
        renderables.append(data_tbl)

    return Panel(
        RenderGroup(*renderables),
        title=panel_title,
        subtitle=panel_subtitle,
        border_style=border,
        expand=True,
        padding=(0, 0),
    )


def build_display(
    all_display_signals: List[TradeSignal],
    ts: datetime,
    etf_prices: Dict[str, float],
    vix_values: Dict[str, Optional[float]],
    pairs_for_scan: List[Tuple[ContractInfo, ContractInfo]],
    iteration: int,
    min_profit: float,
    n_each_side: int = 10,
    no_data_hint: Optional[str] = None,
) -> RenderGroup:
    """构建套利信号布局，按品种分块显示（每品种固定 ATM 上下各 n_each_side 行）"""
    header = Panel(
        f"[bold bright_green]⚡ DeltaZero 正向套利监控[/bold bright_green]  "
        f"[dim]{ts.strftime('%H:%M:%S')}[/dim]  第 {iteration} 次刷新",
        box=box.MINIMAL,
        padding=(0, 1),
    )

    # 按品种分组，再各自按 ATM 上下各取 n_each_side
    groups: Dict[str, List[TradeSignal]] = defaultdict(list)
    for sig in all_display_signals:
        groups[sig.underlying_code].append(sig)

    underlying_list = [c for c in ETF_ORDER if c in etf_prices]
    tables = []
    for u in underlying_list:
        etf_px = etf_prices.get(u, 0.0)
        u_sigs = groups.get(u, [])
        display_sigs = select_display_pairs(u_sigs, etf_px, n_each_side) if u_sigs else []
        # 第二行统计均在显示范围内（ATM 上下各 n_each_side），与表格内容一致
        n_pairs_u = len(display_sigs)  # 显示配对 = 表格行数
        n_opts_u = 2 * len(display_sigs)  # 每配对 2 个期权（Call+Put）
        n_positive_u = sum(1 for s in display_sigs if s.net_profit_estimate >= 0)
        n_profitable_u = sum(1 for s in display_sigs if s.net_profit_estimate >= min_profit)
        tables.append(
            _build_etf_table(
                u, display_sigs, etf_px, min_profit, vix_values.get(u),
                n_pairs=n_pairs_u, n_opts=n_opts_u,
                n_positive=n_positive_u, n_profitable=n_profitable_u,
            )
        )

    if no_data_hint:
        warn_panel = Panel(no_data_hint, title="[yellow]提示[/yellow]", border_style="yellow")
        return RenderGroup(warn_panel, header, *tables)
    return RenderGroup(header, *tables)


# ──────────────────────────────────────────────────────────────────────
# 主逻辑：ZMQ 模式
# ──────────────────────────────────────────────────────────────────────

def run_monitor(
    min_profit: float = 30.0,
    expiry_days: int = 90,
    refresh_secs: int = 3,
    n_each_side: int = 10,
    zmq_port: int = 5555,
    snapshot_dir: str = DEFAULT_MARKET_DATA_DIR,
    etf_fee_rate: float = 0.0002,
    option_one_side_fee: float = 1.5,
) -> None:
    """ZMQ 订阅模式监控：从 data_bus 进程接收实时行情。"""
    try:
        import zmq
    except ImportError:
        console.print("[red]pyzmq 未安装，请执行：pip install pyzmq[/red]")
        return

    etf_prices: Dict[str, float] = {}

    # 从快照恢复
    n_snap = 0
    from config.settings import get_default_config
    tmp_config = get_default_config()
    tmp_config.min_profit_threshold = min_profit
    tmp_config.etf_fee_rate = etf_fee_rate
    tmp_config.option_round_trip_fee = option_one_side_fee * 2
    tmp_strategy = PCPArbitrage(tmp_config)
    n_snap = restore_from_snapshot(tmp_strategy, snapshot_dir, etf_prices)
    if n_snap:
        console.print(f"[green]已从快照恢复 {n_snap} 条 tick[/green]")
    else:
        console.print("[yellow]未找到快照文件，等待第一批实时数据填充...[/yellow]")

    try:
        strategy, contract_mgr, active, pairs, option_codes, etf_codes = (
            init_strategy_and_contracts(
                min_profit, expiry_days, 1.0, etf_prices,  # 1.0=全量配对，显示由 n_each_side 控制
                etf_fee_rate=etf_fee_rate,
                option_round_trip_fee=option_one_side_fee * 2,
                log_fn=lambda msg: console.print(msg),
            )
        )
    except (FileNotFoundError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        return

    if n_snap > 0:
        restore_from_snapshot(strategy, snapshot_dir, etf_prices)

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.connect(f"tcp://127.0.0.1:{zmq_port}")
    sock.setsockopt_string(zmq.SUBSCRIBE, "OPT_")
    sock.setsockopt_string(zmq.SUBSCRIBE, "ETF_")
    sock.setsockopt(zmq.RCVTIMEO, 100)

    console.print(
        f"\n[bold green]ZMQ 模式监控已启动[/bold green]  "
        f"连接 tcp://127.0.0.1:{zmq_port}  "
        f"收到新数据即刷新（空闲时每 {refresh_secs}s）  最小利润 {min_profit:.0f} 元  按 Ctrl+C 退出"
    )
    console.print(
        f"[dim]手续费：ETF {etf_fee_rate*10000:.1f}‱  期权单边 {option_one_side_fee:.2f} 元/张"
        f"（双边 {option_one_side_fee*2:.2f} 元）[/dim]\n"
    )

    iteration = 0
    last_scan = datetime.now()
    last_signals: List[TradeSignal] = []
    etf_display = dict(etf_prices)
    no_data_cycles = 0  # 连续未收到 ZMQ 消息的刷新次数
    # 仅展示“当前实时流里出现过”的标的，避免历史快照把未订阅品种带出来
    stream_underlyings: set[str] = set()
    try:
        yield_curve = BoundedCubicSplineRate.from_cgb_daily(
            base_dir=DEFAULT_MARKET_DATA_DIR,
            require_exists=True,
        )
        vix_engine = VIXEngine(risk_free_rate=yield_curve)
        console.print("[dim]VIX 使用插值国债收益率曲线[/dim]")
    except FileNotFoundError:
        vix_engine = VIXEngine(risk_free_rate=strategy.config.risk_free_rate)
        console.print("[yellow]未找到中债曲线，VIX 使用固定利率 {:.2%}[/yellow]".format(strategy.config.risk_free_rate))
    vix_pairs, vix_option_codes = build_pairs_and_codes(
        contract_mgr, active, etf_prices, atm_range_pct=1.0
    )
    option_codes = sorted(set(option_codes) | set(vix_option_codes))
    vix_pairs_by_underlying: Dict[str, list] = {
        u: [p for p in vix_pairs if p[0].underlying_code == u] for u in _VIX_TARGETS
    }
    vix_last: Dict[str, VIXResult] = {}
    vix_display: Dict[str, Optional[float]] = {u: None for u in _VIX_TARGETS}

    def render() -> RenderGroup:
        return build_display(
            last_signals, datetime.now(), etf_display, vix_display,
            pairs, iteration, min_profit,
        )

    try:
        with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
            while True:
                msgs_recv = 0
                while msgs_recv < 200:
                    try:
                        raw = sock.recv_string()
                    except zmq.Again:
                        break

                    tick = parse_zmq_message(raw)
                    if tick is not None:
                        if isinstance(tick, ETFTickData):
                            strategy.on_etf_tick(tick)
                            etf_display[tick.etf_code] = tick.price
                            stream_underlyings.add(tick.etf_code)
                        else:
                            strategy.on_option_tick(tick)
                            info = contract_mgr.get_info(tick.contract_code)
                            if info is not None:
                                stream_underlyings.add(info.underlying_code)
                    msgs_recv += 1

                now = datetime.now()
                if msgs_recv == 0:
                    no_data_cycles += 1
                else:
                    no_data_cycles = 0
                # 收到新数据即刷新；无数据时按 refresh_secs 周期刷新
                should_refresh = msgs_recv > 0 or (now - last_scan).total_seconds() >= refresh_secs
                if should_refresh:
                    if stream_underlyings:
                        pairs_for_scan = [p for p in pairs if p[0].underlying_code in stream_underlyings]
                        etf_display_view = {k: v for k, v in etf_display.items() if k in stream_underlyings}
                    else:
                        # 尚未收到实时流时，维持原行为（通常只持续很短时间）
                        pairs_for_scan = pairs
                        etf_display_view = etf_display

                    last_signals = strategy.scan_pairs_for_display(pairs_for_scan, current_time=now)
                    for u in [x for x in _VIX_TARGETS if x in etf_display_view]:
                        result = vix_engine.compute_for_underlying(
                            vix_pairs_by_underlying.get(u, []),
                            strategy.aligner,
                            now,
                            last_result=vix_last.get(u),
                            enable_republication=True,
                        )
                        if result is not None:
                            vix_last[u] = result
                            vix_display[u] = result.vix
                    iteration += 1
                    last_scan = now
                    # 订阅期权 = 当前扫描配对中的期权合约数（与监控配对一致）
                    n_opts_scan = len({c.contract_code for p in pairs_for_scan for c in p})
                    no_data_hint: Optional[str] = None
                    if no_data_cycles >= 5 and not stream_underlyings:
                        no_data_hint = (
                            f"未收到 ZMQ 数据，请确认：1) 已先启动 DataBus（DDE 或 Wind）；"
                            f"2) ZMQ 端口与 DataBus 一致（当前 {zmq_port}）；3) 若为 DDE，交易软件已打开且 DDE 有数据。"
                        )
                    def render_filtered() -> RenderGroup:
                        return build_display(
                            last_signals, datetime.now(), etf_display_view, vix_display,
                            pairs_for_scan, iteration, min_profit,
                            n_each_side=n_each_side,
                            no_data_hint=no_data_hint,
                        )
                    live.update(render_filtered())

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        ctx.term()
        console.print("\n[yellow]ZMQ 监控已停止[/yellow]")


# ──────────────────────────────────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DeltaZero — PCP 正向套利实时监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python monitor.py                          # 从 data_bus 进程读取
  python monitor.py --min-profit 150         # 净利润阈值
  python monitor.py --expiry-days 30         # 只看近月合约
  python monitor.py --n-each-side 0          # 显示全部监控期权（0=不限制）
  python monitor.py --refresh 3              # 每3秒刷新
  python monitor.py --zmq-port 5556
""",
    )
    parser.add_argument("--min-profit", type=float, default=30.0, help="最小显示净利润（元/组）")
    parser.add_argument("--expiry-days", type=int, default=90, help="最大到期天数")
    parser.add_argument("--refresh", type=int, default=3, help="刷新间隔（秒），收到新数据会立即刷新")
    parser.add_argument("--n-each-side", type=int, default=10, help="ATM 上下各显示 N 组（0=显示全部）")
    parser.add_argument("--zmq-port", type=int, default=5555, help="ZMQ PUB 端口")
    parser.add_argument("--snapshot-dir", type=str, default=DEFAULT_MARKET_DATA_DIR, help="快照文件目录")
    parser.add_argument("--etf-fee-rate", type=float, default=0.0002, help="ETF 单边手续费率（默认 0.0002 即万2）")
    parser.add_argument("--option-one-side-fee", type=float, default=1.5, help="期权单边手续费（元/张，默认 1.5）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_monitor(
        min_profit=args.min_profit,
        expiry_days=args.expiry_days,
        refresh_secs=args.refresh,
        n_each_side=args.n_each_side,
        zmq_port=args.zmq_port,
        snapshot_dir=args.snapshot_dir,
        etf_fee_rate=args.etf_fee_rate,
        option_one_side_fee=args.option_one_side_fee,
    )
