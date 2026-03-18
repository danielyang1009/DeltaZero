# -*- coding: utf-8 -*-
"""
monitor_common — monitor 共享逻辑

提供：
  - Windows 终端 UTF-8 编码修复
  - 常量（ETF名称映射、品种排序列表、合约信息路径等）
  - 活跃合约加载
  - Call/Put 配对构建
  - snapshot_latest.parquet 恢复
  - ZMQ 消息解析
  - 信号序列化
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from models import (
    ArbitrageSignal,
    ContractInfo,
    ETFTickData,
    OptionType,
    SignalType,
    TickData,
    normalize_code,
)
from config.settings import ETF_CODE_TO_NAME, UNDERLYINGS, TradingConfig, get_default_config
from data_engine.contract_catalog import ContractInfoManager, get_optionchain_path
from data_engine.tick_aligner import TickAligner
from strategies.pcp_arbitrage import PCPArbitrageStrategy

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# Windows 编码修复
# ══════════════════════════════════════════════════════════════════════

def fix_windows_encoding() -> None:
    """将 Windows 控制台切换到 UTF-8，必须在其他 import 之前调用。"""
    if sys.platform != "win32":
        return
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace"
        )
    os.environ["PYTHONIOENCODING"] = "utf-8"


# ══════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════

ETF_NAME_MAP: Dict[str, str] = {
    code.split(".")[0]: name for code, name in ETF_CODE_TO_NAME.items()
}

ETF_ORDER: List[str] = list(UNDERLYINGS)

MONITOR_UNDERLYINGS = set(UNDERLYINGS)


def _safe_int(v: object) -> int:
    try:
        if v is None:
            return 0
        f = float(v)
        if math.isnan(f):
            return 0
        return int(round(f))
    except Exception:
        return 0



# ══════════════════════════════════════════════════════════════════════
# 合约加载 & 配对
# ══════════════════════════════════════════════════════════════════════

def load_active_contracts(
    contract_mgr: ContractInfoManager,
    max_expiry_days: int,
) -> List[ContractInfo]:
    """
    筛选当日活跃且在 max_expiry_days 天内到期的三大品种合约。
    包含调整型合约（is_adjusted=True），由显示层分区展示。
    """
    today = date.today()
    return [
        info
        for info in contract_mgr.contracts.values()
        if info.underlying_code in MONITOR_UNDERLYINGS
        and info.list_date <= today <= info.expiry_date
        and (info.expiry_date - today).days <= max_expiry_days
    ]


def build_pairs_and_codes(
    contract_mgr: ContractInfoManager,
    active: List[ContractInfo],
    etf_prices: Dict[str, float],
    atm_range_pct: float = 0.20,
) -> Tuple[List[Tuple[ContractInfo, ContractInfo]], List[str]]:
    """
    构建 Call/Put 配对并按 ATM 距离过滤。

    Returns:
        (配对列表, 期权代码列表)
    """
    by_underlying: Dict[str, Dict[date, List[ContractInfo]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for info in active:
        by_underlying[info.underlying_code][info.expiry_date].append(info)

    pairs: List[Tuple[ContractInfo, ContractInfo]] = []
    option_codes: set = set()

    for u_code, expiry_map in by_underlying.items():
        etf_px = etf_prices.get(u_code, 0.0)

        for expiry, contracts in expiry_map.items():
            calls = {
                c.strike_price: c
                for c in contracts
                if c.option_type == OptionType.CALL
            }
            puts = {
                c.strike_price: c
                for c in contracts
                if c.option_type == OptionType.PUT
            }
            common_strikes = sorted(set(calls) & set(puts))

            for strike in common_strikes:
                if etf_px > 0:
                    dist_pct = abs(strike - etf_px) / etf_px
                    if dist_pct > atm_range_pct:
                        continue

                call_info = calls[strike]
                put_info = puts[strike]
                pairs.append((call_info, put_info))
                option_codes.add(call_info.contract_code)
                option_codes.add(put_info.contract_code)

    return pairs, sorted(option_codes)


# ══════════════════════════════════════════════════════════════════════
# 快照恢复
# ══════════════════════════════════════════════════════════════════════

def restore_from_snapshot(
    aligner: TickAligner,
    snapshot_dir: str,
    etf_prices: Dict[str, float],
) -> int:
    """
    从 snapshot_latest.parquet 恢复 TickAligner 状态。

    Returns:
        恢复的 tick 条数
    """
    snap = Path(snapshot_dir) / "snapshot_latest.parquet"
    if not snap.exists():
        return 0
    try:
        import pandas as pd

        df = pd.read_parquet(str(snap))
    except Exception as e:
        logger.warning("快照读取失败（跳过）：%s", e)
        return 0

    ts = datetime.now()
    count = 0
    for _, row in df.iterrows():
        try:
            raw_ts = row.get("ts", 0) or 0
            if raw_ts > 1e10:
                tick_ts = datetime.fromtimestamp(raw_ts / 1000)
            else:
                tick_ts = datetime.fromtimestamp(raw_ts) if raw_ts > 0 else ts

            last = float(row.get("last") or 0)
            ask1 = float(row.get("ask1") or math.nan)
            bid1 = float(row.get("bid1") or math.nan)
            code = str(row["code"])

            if row.get("type") == "etf":
                tick = ETFTickData(
                    timestamp=tick_ts,
                    etf_code=code,
                    price=last,
                    ask_price=ask1,
                    bid_price=bid1,
                    ask_volume=_safe_int(row.get("askv1")),
                    bid_volume=_safe_int(row.get("bidv1")),
                    is_simulated=False,
                )
                aligner.update_etf(tick)
                if last > 0:
                    etf_prices[code] = last
            else:
                if last <= 0 or math.isnan(ask1) or math.isnan(bid1):
                    continue
                askv1 = _safe_int(row.get("askv1"))
                bidv1 = _safe_int(row.get("bidv1"))
                tick = TickData(
                    timestamp=tick_ts,
                    contract_code=code,
                    current=last,
                    volume=int(row.get("vol") or 0),
                    high=float(row.get("high") or last),
                    low=float(row.get("low") or last),
                    money=0.0,
                    position=int(row.get("oi") or 0),
                    ask_prices=[ask1] + [math.nan] * 4,
                    ask_volumes=[askv1] + [0] * 4,
                    bid_prices=[bid1] + [math.nan] * 4,
                    bid_volumes=[bidv1] + [0] * 4,
                )
                aligner.update_option(tick)
            count += 1
        except Exception:
            continue
    return count


# ══════════════════════════════════════════════════════════════════════
# ZMQ 消息解析
# ══════════════════════════════════════════════════════════════════════

def parse_zmq_message(
    raw: str,
) -> Optional[Union[ETFTickData, TickData]]:
    """
    解析一条 ZMQ 广播消息，返回 ETFTickData 或 TickData，解析失败返回 None。
    """
    try:
        _, _, body = raw.partition(" ")
        d = json.loads(body)
        ts = datetime.fromtimestamp(d["ts"] / 1000)

        if d.get("type") == "etf":
            last = d.get("last") or 0
            if last <= 0:
                return None
            return ETFTickData(
                timestamp=ts,
                etf_code=normalize_code(d["code"], ".SH"),
                price=float(last),
                ask_price=float(d.get("ask1") or math.nan),
                bid_price=float(d.get("bid1") or math.nan),
                ask_volume=_safe_int(d.get("askv1")),
                bid_volume=_safe_int(d.get("bidv1")),
                is_simulated=False,
            )
        else:
            last = d.get("last") or 0
            ask1 = d.get("ask1")
            bid1 = d.get("bid1")
            askv1 = _safe_int(d.get("askv1"))
            bidv1 = _safe_int(d.get("bidv1"))
            if last <= 0:
                return None
            # 与 data_bus 回退逻辑保持一致：盘口缺失时用 last 兜底
            ask1 = float(ask1) if ask1 is not None else float(last)
            bid1 = float(bid1) if bid1 is not None else float(last)
            if math.isnan(ask1):
                ask1 = float(last)
            if math.isnan(bid1):
                bid1 = float(last)
            return TickData(
                timestamp=ts,
                contract_code=normalize_code(d["code"], ".SH"),
                current=float(last),
                volume=d.get("vol") or 0,
                high=float(d.get("high") or last),
                low=float(d.get("low") or last),
                money=0.0,
                position=d.get("oi") or 0,
                ask_prices=[float(ask1)] + [math.nan] * 4,
                ask_volumes=[askv1] + [0] * 4,
                bid_prices=[float(bid1)] + [math.nan] * 4,
                bid_volumes=[bidv1] + [0] * 4,
            )
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# ETF 价格回退估算
# ══════════════════════════════════════════════════════════════════════

def estimate_etf_fallback_prices(
    etf_prices: Dict[str, float],
    active: List[ContractInfo],
    etf_codes: List[str],
) -> None:
    """对没有实时价格的 ETF，用行权价中位数估算（in-place 更新 etf_prices）。"""
    for code in etf_codes:
        if code not in etf_prices:
            strikes = [c.strike_price for c in active if c.underlying_code == code]
            if strikes:
                etf_prices[code] = (min(strikes) + max(strikes)) / 2


# ══════════════════════════════════════════════════════════════════════
# 信号序列化
# ══════════════════════════════════════════════════════════════════════

def signal_to_dict(sig: ArbitrageSignal) -> dict:
    """将 ArbitrageSignal 序列化为前端友好的字典。"""
    return {
        "expiry": sig.expiry.strftime("%m-%d"),
        "strike": sig.strike,
        "direction": "正向" if sig.direction == SignalType.FORWARD else "反向",
        "is_forward": sig.direction == SignalType.FORWARD,
        "call_bid": sig.call_bid,
        "put_ask": sig.put_ask,
        "spot": sig.spot_ask,
        "profit": round(sig.net_profit, 0),
        "underlying": sig.underlying,
        "call_code": sig.call_code,
        "put_code": sig.put_code,
        "multiplier": sig.multiplier,
        "is_adjusted": sig.is_adjusted,
        "calc_detail": sig.calc_detail,
        "max_qty": sig.max_qty,
        "spread_ratio": sig.spread_ratio,
        "obi_c": sig.obi_call,
        "obi_s": sig.obi_spot,
        "obi_p": sig.obi_put,
        "net_1tick": sig.net_1tick,
        "tolerance": sig.tolerance,
    }


# ══════════════════════════════════════════════════════════════════════
# 展示行筛选
# ══════════════════════════════════════════════════════════════════════

def select_pairs_by_atm(
    pairs: List[Tuple["ContractInfo", "ContractInfo"]],
    etf_prices: Dict[str, float],
    n_each_side: int,
) -> List[Tuple["ContractInfo", "ContractInfo"]]:
    """
    从完整配对列表中，按 (underlying, expiry, multiplier) 分组，
    各组独立找 ATM ± n_each_side 行权价，返回筛选后的配对列表。

    n_each_side=1 → 每组最多 3 个行权价（ATM + 上下各 1）
    n_each_side=0 → 返回全部，不做限制
    """
    if n_each_side <= 0:
        return list(pairs)

    groups: Dict[Tuple, List] = defaultdict(list)
    for pair in pairs:
        call_info = pair[0]
        key = (call_info.underlying_code, call_info.expiry_date, call_info.contract_unit)
        groups[key].append(pair)

    result = []
    for (underlying, _expiry, _mult), group_pairs in groups.items():
        etf_px = etf_prices.get(underlying, 0.0)
        unique_strikes = sorted({p[0].strike_price for p in group_pairs})
        if not unique_strikes:
            continue
        if etf_px <= 0:
            result.extend(group_pairs)
            continue
        atm_strike = min(unique_strikes, key=lambda x: abs(x - etf_px))
        below = set(sorted([k for k in unique_strikes if k < atm_strike], reverse=True)[:n_each_side])
        above = set(sorted([k for k in unique_strikes if k > atm_strike])[:n_each_side])
        selected = below | {atm_strike} | above
        result.extend(p for p in group_pairs if p[0].strike_price in selected)

    return result


# ══════════════════════════════════════════════════════════════════════
# 策略初始化便捷函数
# ══════════════════════════════════════════════════════════════════════

def init_strategy_and_contracts(
    min_profit: float,
    expiry_days: int,
    atm_range_pct: float,
    etf_prices: Dict[str, float],
    *,
    etf_fee_rate: float = 0.0002,
    option_round_trip_fee: float = 3.0,
    include_interest: bool = False,
    log_fn=None,
) -> Tuple[
    PCPArbitrageStrategy,
    ContractInfoManager,
    List[ContractInfo],
    List[Tuple[ContractInfo, ContractInfo]],
    List[str],
    List[str],
]:
    """
    封装策略 + 合约加载 + 乘数查询 + 配对构建的完整初始化流程。

    Returns:
        (strategy, contract_mgr, active, pairs, option_codes, etf_codes)

    Raises:
        FileNotFoundError: 合约信息文件不存在
        RuntimeError: 无活跃合约
    """
    _log = log_fn or logger.info

    config = get_default_config()
    config.min_profit_threshold = min_profit
    config.etf_fee_rate = etf_fee_rate
    config.option_round_trip_fee = option_round_trip_fee
    config.include_interest = include_interest
    strategy = PCPArbitrageStrategy(config)

    contract_mgr = ContractInfoManager()
    optionchain_csv = get_optionchain_path(target_date=date.today())
    if not optionchain_csv.exists():
        raise FileNotFoundError(
            f"optionchain 文件不存在: {optionchain_csv}，请开盘前执行 python fetch_optionchain.py"
        )
    n = contract_mgr.load_from_optionchain(optionchain_csv, target_date=date.today())
    _log(f"已从 optionchain 加载 {n} 条合约信息")

    active = load_active_contracts(contract_mgr, expiry_days)
    if not active:
        raise RuntimeError("无活跃合约")
    _log(f"当前活跃合约（{expiry_days}天内到期）: {len(active)} 个")

    etf_codes = sorted(set(c.underlying_code for c in active))
    estimate_etf_fallback_prices(etf_prices, active, etf_codes)

    pairs, option_codes = build_pairs_and_codes(
        contract_mgr, active, etf_prices, atm_range_pct
    )
    _log(
        f"Call/Put 配对: {len(pairs)} 组  监控期权: {len(option_codes)} 个"
    )

    return strategy, contract_mgr, active, pairs, option_codes, etf_codes
