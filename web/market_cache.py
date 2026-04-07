# -*- coding: utf-8 -*-
"""web/market_cache.py — FastAPI 内存行情缓存层（ZMQ SUB 后台线程）。

维护一份 {code → tick_dict} 的 LKV（Last Known Value），供所有 FastAPI 端点共享。
DataBus 未运行时从 snapshot_latest.parquet 冷启动填充。

线程架构：
  Thread-1 (market-cache-zmq):     ZMQ SUB → _lkv
  Thread-2 (market-cache-compute):  _lkv → 向量化 NR → loop.call_soon_threadsafe → asyncio.Queue
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import defaultdict
from datetime import datetime, time as _time, date, timedelta
from utils.time_utils import trading_days_until
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lkv: Dict[str, Dict[str, Any]] = {}
_lkv_lock = threading.Lock()

_rich_lkv: Dict[str, Any] = {}
_rich_lkv_lock = threading.Lock()

_running = False
_thread: Optional[threading.Thread] = None
_compute_thread: Optional[threading.Thread] = None
_monitor_thread: Optional[threading.Thread] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_update_queue: Optional[asyncio.Queue] = None
_monitor_queue_ref: Optional[asyncio.Queue] = None
_start_time: Optional[float] = None

_monitor_cache: Dict[str, Any] = {}
_monitor_cache_lock = threading.Lock()

_zmq_port: int = 5555
_snapshot_dir: str = ""


def _try_put(q: asyncio.Queue, item: Any) -> None:
    """在事件循环线程内执行，吞掉 QueueFull（满时直接丢弃，不打印异常）。"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


def get_snapshot() -> Dict[str, Dict[str, Any]]:
    """返回当前内存快照的浅拷贝，供 API 端点安全读取。"""
    with _lkv_lock:
        return dict(_lkv)


def get_rich_snapshot() -> Dict[str, Any]:
    """返回最新计算结果浅拷贝（WS 广播兜底用）。"""
    with _rich_lkv_lock:
        return dict(_rich_lkv)


def get_monitor_cache() -> Dict[str, Any]:
    """返回最新 PCP 套利监控结果浅拷贝（WS 广播兜底用）。"""
    with _monitor_cache_lock:
        return dict(_monitor_cache)


def get_status() -> Dict[str, Any]:
    """返回 market_cache 线程运行状态，供 /api/state 展示。"""
    zmq_alive     = _thread is not None and _thread.is_alive()
    compute_alive = _compute_thread is not None and _compute_thread.is_alive()
    uptime: Optional[str] = None
    if _start_time is not None:
        sec = int(max(time.time() - _start_time, 0))
        h, rem = divmod(sec, 3600)
        m, s   = divmod(rem, 60)
        uptime = f"{h}h{m:02d}m" if h > 0 else (f"{m}m{s:02d}s" if m > 0 else f"{s}s")
    return {
        "running": _running and zmq_alive and compute_alive,
        "zmq_alive": zmq_alive,
        "compute_alive": compute_alive,
        "uptime": uptime or "-",
        "lkv_count": len(_lkv),
    }


def _restore_from_parquet(snapshot_path: Path) -> int:
    """从 snapshot_latest.parquet 预填 LKV，返回恢复条数。"""
    if not snapshot_path.exists():
        return 0
    try:
        import pandas as pd
        df = pd.read_parquet(str(snapshot_path))
    except Exception as e:
        logger.warning("market_cache: 冷启动快照读取失败: %s", e)
        return 0

    entries: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = str(row.get("code", "") or "").strip()
        if not code:
            continue
        entries[code] = {
            "code": code,
            "type": str(row.get("type", "") or "").lower(),
            "last": row.get("last"),
            "bid1": row.get("bid1"),
            "ask1": row.get("ask1"),
            "bidv1": row.get("bidv1"),
            "askv1": row.get("askv1"),
            "underlying": str(row.get("underlying", "") or ""),
            "ts": row.get("ts"),
        }

    with _lkv_lock:
        _lkv.update(entries)

    count = len(entries)
    logger.info("market_cache: 冷启动恢复 %d 条记录", count)
    return count


def _zmq_loop(zmq_port: int, snapshot_dir: str) -> None:
    global _running

    # 冷启动：先从 parquet 恢复
    snap_path = Path(snapshot_dir) / "snapshot_latest.parquet"
    _restore_from_parquet(snap_path)

    try:
        import zmq
    except ImportError:
        logger.error("market_cache: 缺少 pyzmq，后台线程退出")
        return

    context = zmq.Context()

    def _new_socket():
        s = context.socket(zmq.SUB)
        s.setsockopt(zmq.CONFLATE, 1)   # 只保最新一条消息，丢弃堆积
        s.setsockopt(zmq.RCVTIMEO, 500)
        s.setsockopt_string(zmq.SUBSCRIBE, "OPT_")
        s.setsockopt_string(zmq.SUBSCRIBE, "ETF_")
        return s

    socket = _new_socket()
    connected = False

    while _running:
        if not connected:
            try:
                socket.connect(f"tcp://127.0.0.1:{zmq_port}")
                connected = True
                logger.info("market_cache: ZMQ 已连接 tcp://127.0.0.1:%d", zmq_port)
            except Exception as e:
                logger.debug("market_cache: ZMQ 连接失败: %s，1秒后重试", e)
                time.sleep(1.0)
                continue

        try:
            raw = socket.recv_string()
        except zmq.Again:
            continue
        except Exception as e:
            logger.warning("market_cache: ZMQ recv 错误: %s，重连中", e)
            connected = False
            try:
                socket.close(linger=0)
            except Exception:
                pass
            socket = _new_socket()
            continue

        try:
            _, _, body = raw.partition(" ")
            d = json.loads(body)
            code = str(d.get("code", "") or "").strip()
            if not code:
                continue
            if code.endswith(".XSHG"):
                code = code[:-5] + ".SH"
            entry: Dict[str, Any] = {
                "code": code,
                "type": str(d.get("type", "") or "").lower(),
                "last": d.get("last"),
                "bid1": d.get("bid1"),
                "ask1": d.get("ask1"),
                "bidv1": d.get("bidv1"),
                "askv1": d.get("askv1"),
                "underlying": str(d.get("underlying", "") or ""),
                "ts": d.get("ts"),
            }
            with _lkv_lock:
                _lkv[code] = entry
        except Exception:
            continue

    try:
        socket.close(linger=0)
        context.term()
    except Exception:
        pass
    logger.info("market_cache: ZMQ 线程已退出")


def _compute_loop() -> None:
    """
    后台计算线程（Thread-2）：每 100ms 微批次。

    数据流：_lkv → 向量化 NR → loop.call_soon_threadsafe(queue.put_nowait, data)
    绝对禁止直接调用 asyncio API，全部通过 call_soon_threadsafe 跨域传递。
    """
    import numpy as np
    from config.settings import UNDERLYINGS
    from data_engine.contract_catalog import ContractInfoManager, get_optionchain_path
    from models import OptionType
    from calculators.iv_calculator import calc_implied_forward
    from calculators.vectorized_pricer import VectorizedIVCalculator

    pricer = VectorizedIVCalculator()
    catalog: dict = {}
    catalog_mtime = None
    curve = None
    curve_refresh_ts = 0.0
    r_default = 0.02

    while _running:
        time.sleep(0.1)   # 100ms 微批次间隔

        # ── 刷新合约目录 ──────────────────────────────────
        try:
            path = get_optionchain_path()
            mtime = path.stat().st_mtime if path.exists() else None
            if mtime != catalog_mtime:
                mgr = ContractInfoManager()
                mgr.load_from_optionchain(path)
                catalog = mgr.contracts
                catalog_mtime = mtime
        except Exception:
            pass
        if not catalog:
            continue

        # ── 刷新利率曲线（每 60s 尝试一次）──────────────
        now_ts = time.time()   # [GUARD-3] Unix 时间戳（毫秒精度）
        if now_ts - curve_refresh_ts > 60.0:
            try:
                from calculators.yield_curve import BoundedCubicSplineRate
                curve = BoundedCubicSplineRate.from_cgb_daily(require_exists=True)
            except Exception:
                curve = None
            curve_refresh_ts = now_ts

        snap = get_snapshot()
        if not snap:
            continue

        result: Dict[str, Any] = {}

        for underlying in UNDERLYINGS:
            etf_rec = snap.get(underlying, {})
            spot_raw = etf_rec.get("last")
            spot = float(spot_raw) if spot_raw is not None else float("nan")

            # 按 (到期日, is_adjusted) 分组，避免标准/调整合约行权价互相覆盖
            expiry_adj_map: Dict = defaultdict(lambda: {"calls": {}, "puts": {}})
            for code, info in catalog.items():
                if info.underlying_code != underlying:
                    continue
                rec = snap.get(code)
                if not rec:
                    continue
                try:
                    b = float(rec.get("bid1") or "nan")
                    a = float(rec.get("ask1") or "nan")
                except Exception:
                    continue
                if not (b > 0 and a > 0):
                    continue
                mid = (b + a) / 2.0
                key = "calls" if info.option_type == OptionType.CALL else "puts"
                is_adj = (getattr(info, "contract_unit", 10000) != 10000)
                expiry_adj_map[(info.expiry_date, is_adj)][key][info.strike_price] = {
                    "code": code, "mid": mid, "bid": b, "ask": a,
                }

            expiries_std: Dict[str, Any] = {}
            expiries_adj: Dict[str, Any] = {}
            for (expiry_date, is_adj), grp in expiry_adj_map.items():
                calls, puts = grp["calls"], grp["puts"]

                # ── [GUARD-3] T 毫秒级动态对齐 ─────────────
                expiry_ts = datetime.combine(expiry_date, _time(15, 0)).timestamp()
                T = pricer.calc_T(expiry_ts)   # max((expiry_ts - time.time())/年秒, 1e-6)
                if T < 1e-4:   # 不足约 53 分钟（末日轮），跳过
                    continue

                try:
                    r = curve.get_rate(T * 365) if curve else r_default
                except Exception:
                    r = r_default

                common = sorted(set(calls) & set(puts))
                if not common:
                    continue
                K_atm = min(common, key=lambda k: abs(calls[k]["mid"] - puts[k]["mid"]))
                F = calc_implied_forward(K_atm, calls[K_atm]["mid"], puts[K_atm]["mid"], T, r)

                disc = math.exp(-r * T)
                contracts_out = []
                call_iv_map: Dict[float, float] = {}
                put_iv_map:  Dict[float, float] = {}
                call_spread_map: Dict[float, float] = {}
                put_spread_map:  Dict[float, float] = {}

                for flag_val, side, label in ((+1, calls, "C"), (-1, puts, "P")):
                    strikes = sorted(side.keys())
                    if not strikes:
                        continue
                    K_arr    = np.array(strikes)
                    mid_arr  = np.array([side[k]["mid"] for k in strikes])
                    bid_arr  = np.array([side[k]["bid"] for k in strikes])
                    ask_arr  = np.array([side[k]["ask"] for k in strikes])
                    flag_arr = np.full(len(strikes), float(flag_val))

                    # ── [GUARD-3] 微观流动性防线：剔除宽口和废纸合约 ──
                    spread_arr     = ask_arr - bid_arr
                    max_spread_arr = np.maximum(0.0020, mid_arr * 0.30)
                    liquidity_mask = (mid_arr >= 0.0010) & (spread_arr <= max_spread_arr)
                    mid_arr[~liquidity_mask] = np.nan
                    bid_arr[~liquidity_mask] = np.nan
                    ask_arr[~liquidity_mask] = np.nan

                    iv_arr     = pricer.calc_iv(F, K_arr, T, r, mid_arr,  flag_arr)
                    bid_iv_arr = pricer.calc_iv(F, K_arr, T, r, bid_arr,  flag_arr)
                    ask_iv_arr = pricer.calc_iv(F, K_arr, T, r, ask_arr,  flag_arr)

                    if label == "C":
                        for k, iv in zip(strikes, iv_arr):
                            call_iv_map[k] = float(iv)
                            call_spread_map[k] = side[k]["ask"] - side[k]["bid"]
                    else:
                        for k, iv in zip(strikes, iv_arr):
                            put_iv_map[k] = float(iv)
                            put_spread_map[k] = side[k]["ask"] - side[k]["bid"]

                    for i, k in enumerate(strikes):
                        iv_v   = None if math.isnan(iv_arr[i])     else round(float(iv_arr[i]), 6)
                        bid_iv = None if math.isnan(bid_iv_arr[i]) else round(float(bid_iv_arr[i]), 6)
                        ask_iv = None if math.isnan(ask_iv_arr[i]) else round(float(ask_iv_arr[i]), 6)

                        pcp_dev = None
                        if label == "C":
                            p_mid = puts.get(k, {}).get("mid")
                            if p_mid:
                                pcp_dev = round(side[k]["mid"] + k * disc - p_mid - F * disc, 6)

                        iv_skew = None
                        if label == "P" and iv_v is not None:
                            c_iv = call_iv_map.get(k, float("nan"))
                            if not math.isnan(c_iv):
                                iv_skew = round(c_iv - float(iv_arr[i]), 6)

                        contracts_out.append({
                            "code": side[k]["code"], "strike": k, "type": label,
                            "mid": round(side[k]["mid"], 6),
                            "iv": iv_v, "bid_iv": bid_iv, "ask_iv": ask_iv,
                            "pcp_dev": pcp_dev, "iv_skew": iv_skew,
                        })

                # ── 流动性拼接：生成主力 IV 曲线 ──────────────────────
                primary_ivs = []
                for k in sorted(set(call_iv_map) & set(put_iv_map)):
                    c_iv = call_iv_map[k]
                    p_iv = put_iv_map[k]
                    c_sp = call_spread_map.get(k, float("inf"))
                    p_sp = put_spread_map.get(k, float("inf"))

                    if k < F * 0.995:
                        # Put 是虚值（干净），Call 是深度实值（脏）
                        piv, flag = p_iv, "P"
                    elif k > F * 1.005:
                        # Call 是虚值（干净），Put 是深度实值（脏）
                        piv, flag = c_iv, "C"
                    else:
                        # 平值附近：按买卖价差选流动性更好的一侧
                        if c_sp < p_sp:
                            piv, flag = c_iv, "C"
                        elif p_sp < c_sp:
                            piv, flag = p_iv, "P"
                        else:
                            if not math.isnan(c_iv) and not math.isnan(p_iv):
                                piv, flag = (c_iv + p_iv) / 2, "AVG"
                            elif not math.isnan(c_iv):
                                piv, flag = c_iv, "C"
                            else:
                                piv, flag = p_iv, "P"

                    if not math.isnan(piv):
                        primary_ivs.append({"strike": k, "iv": round(piv, 6), "flag": flag})

                entry = {
                    "F": round(F, 6), "T_days": round(T * 365.25, 4),
                    "r": round(r, 6), "atm_strike": K_atm,
                    "contracts": contracts_out,
                    "primary_ivs": primary_ivs,
                }
                key_str = expiry_date.strftime("%Y-%m-%d")
                (expiries_adj if is_adj else expiries_std)[key_str] = entry

            result[underlying] = {
                "spot": round(spot, 6) if not math.isnan(spot) else None,
                "ts": int(now_ts * 1000),
                "expiries": expiries_std,
                "adj_expiries": expiries_adj,
            }

        # ── 写入 _rich_lkv（HTTP 兜底用）────────────────
        with _rich_lkv_lock:
            _rich_lkv.update(result)

        # ── 线程 → 协程安全传递 ──────────────────────────────────
        # 必须通过 call_soon_threadsafe 调度到 FastAPI 事件循环执行。
        # _try_put 吞掉 QueueFull：队列满时丢弃本次结果（下次计算会覆盖），
        # 避免 asyncio 打印 QueueFull traceback + handle repr 刷屏。
        if _event_loop is not None and _update_queue is not None:
            _event_loop.call_soon_threadsafe(_try_put, _update_queue, result)


def _monitor_compute_loop() -> None:
    """
    后台 PCP 套利计算线程（Thread-3，Phase 3 重构版）

    架构变更：
      旧：strategy.on_xxx_tick(tick) → strategy.scan_pairs_for_display(pairs)
      新：aligner.update_tick(tick)  → pcp_strategy.scan_pairs_for_display(snapshot, pairs)

    数据流：独立 ZMQ SUB（无CONFLATE）→ TickAligner 更新 LKV → PCPArbitrageStrategy.scan
         → asyncio.Queue → WebSocket 推送
    顶层 try-except 确保任何未捕获异常只记录日志，线程不会静默退出。
    """
    try:
        import zmq as _zmq
    except ImportError:
        logger.error("market_cache_monitor: 缺少 pyzmq，线程退出")
        return

    from config.settings import UNDERLYINGS, ETF_CODE_TO_NAME, DEFAULT_EXPIRY_DAYS, DEFAULT_MIN_PROFIT
    from monitors.common import (
        init_strategy_and_contracts, select_pairs_by_atm,
        parse_zmq_message,
    )
    from data_engine.contract_catalog import get_optionchain_path
    from data_engine.tick_aligner import TickAligner as _TickAligner
    from models import ETFTickData
    from strategies.pcp_arbitrage import PCPArbitrageStrategy

    # ── 初始化：加载合约元数据 + 创建 TickAligner / PCPArbitrageStrategy ──
    def _init_components():
        snap = get_snapshot()
        etf_prices: Dict[str, float] = {}
        for code, rec in snap.items():
            if str(rec.get("type", "")).lower() == "etf":
                last = rec.get("last")
                if isinstance(last, (int, float)) and last > 0:
                    etf_prices[code] = float(last)

        path = get_optionchain_path()
        mtime = path.stat().st_mtime if path.exists() else None

        # 用 init_strategy_and_contracts 取合约元数据（忽略返回的旧策略实例）
        from config.settings import get_default_config
        _, contract_mgr, active, pairs, option_codes, etf_codes = (
            init_strategy_and_contracts(
                min_profit=DEFAULT_MIN_PROFIT,
                expiry_days=DEFAULT_EXPIRY_DAYS,
                atm_range_pct=1.0,
                etf_prices=etf_prices,
            )
        )

        # Phase 3：创建新架构的状态引擎 + 无状态策略
        aligner  = _TickAligner()
        pcp      = PCPArbitrageStrategy(get_default_config())

        logger.info("market_cache_monitor: 初始化成功，配对 %d 组", len(pairs))
        return pcp, aligner, contract_mgr, pairs, etf_prices, mtime

    pcp_strategy   = None
    aligner        = None
    contract_mgr   = None
    pairs          = None
    optionchain_mtime = None
    mtime_check_ts = 0.0
    sock           = None

    # 等待 LKV 有数据后再初始化
    _init_wait = 0
    while _running and not get_snapshot():
        time.sleep(0.5)
        _init_wait += 1
        if _init_wait > 20:
            break

    while _running:
        # ── 首次 / 文件变更后初始化 ────────────────────────────────
        if pcp_strategy is None:
            try:
                pcp_strategy, aligner, contract_mgr, pairs, etf_display, optionchain_mtime = (
                    _init_components()
                )
                mtime_check_ts = time.time()
            except Exception as e:
                logger.warning("market_cache_monitor: 初始化失败: %s，5s 后重试", e)
                time.sleep(5.0)
                continue

            # 建立独立 ZMQ SUB socket（无 CONFLATE，需要每条都处理）
            if sock is not None:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass
            sock = _zmq.Context.instance().socket(_zmq.SUB)
            sock.setsockopt(_zmq.RCVTIMEO, 100)
            sock.setsockopt_string(_zmq.SUBSCRIBE, "OPT_")
            sock.setsockopt_string(_zmq.SUBSCRIBE, "ETF_")
            try:
                sock.connect(f"tcp://127.0.0.1:{_zmq_port}")
                logger.info("market_cache_monitor: ZMQ 已连接 tcp://127.0.0.1:%d", _zmq_port)
            except Exception as e:
                logger.warning("market_cache_monitor: ZMQ 连接失败: %s", e)

        stream_underlyings: set = set()
        last_scan = datetime.now()
        etf_display: Dict[str, float] = {}   # etf_code → last price（用于 ATM 筛选）

        while _running and pcp_strategy is not None:
            try:
                # ── 每 60s 检查 optionchain 文件变更 ──────────────
                now_ts = time.time()
                if now_ts - mtime_check_ts > 60.0:
                    mtime_check_ts = now_ts
                    try:
                        path = get_optionchain_path()
                        mtime = path.stat().st_mtime if path.exists() else None
                        if mtime != optionchain_mtime:
                            logger.info("market_cache_monitor: optionchain 已变更，触发重新初始化")
                            pcp_strategy = None
                            break
                    except Exception:
                        pass

                # ── 批量接收 ZMQ 消息（最多 200 条/cycle）──────────
                # Phase 3：用 aligner.update_tick(tick) 替代旧的 strategy.on_xxx_tick(tick)
                msgs_recv = 0
                while msgs_recv < 200:
                    try:
                        raw = sock.recv_string()
                    except _zmq.Again:
                        break
                    except Exception as e:
                        logger.warning("market_cache_monitor: ZMQ recv 错误: %s", e)
                        break

                    tick = parse_zmq_message(raw)
                    if tick is None:
                        msgs_recv += 1
                        continue

                    # ── 核心变更：更新 TickAligner，不再调用 strategy.on_xxx_tick ──
                    aligner.update_tick(tick)

                    if isinstance(tick, ETFTickData):
                        etf_display[tick.etf_code] = tick.price
                        stream_underlyings.add(tick.etf_code)
                    else:
                        if contract_mgr is not None:
                            info = contract_mgr.contracts.get(tick.contract_code)
                            if info:
                                stream_underlyings.add(info.underlying_code)
                    msgs_recv += 1

                # ── 判断是否需要刷新 ────────────────────────────────
                now = datetime.now()
                elapsed = (now - last_scan).total_seconds()
                should_refresh = msgs_recv > 0 or elapsed >= 2.0
                if not should_refresh:
                    continue

                last_scan = now
                now_ts = time.time()

                # ── 筛选配对 & 计算信号 ─────────────────────────────
                if stream_underlyings:
                    pairs_for_scan = [p for p in (pairs or []) if p[0].underlying_code in stream_underlyings]
                    etf_view = {k: v for k, v in etf_display.items() if k in stream_underlyings}
                else:
                    pairs_for_scan = list(pairs or [])
                    etf_view = dict(etf_display)

                display_pairs  = select_pairs_by_atm(pairs_for_scan, etf_view, n_each_side=0)
                snapshot       = aligner.snapshot()

                # ── 核心变更：传入 snapshot 调用无状态策略 ────────────
                try:
                    signals = pcp_strategy.scan_pairs_for_display(
                        snapshot, display_pairs, current_time=now,
                    )
                except Exception as e:
                    logger.warning("market_cache_monitor: scan_pairs_for_display 异常: %s", e)
                    continue

                # ── 序列化（适配 ArbitrageSignal 字段名）───────────────
                # ArbitrageSignal 字段与旧 TradeSignal 的差异：
                #   sig.underlying     (旧: sig.underlying_code)
                #   sig.net_profit     (旧: sig.net_profit_estimate)
                #   sig.obi_call       (旧: sig.obi_c)
                #   sig.obi_spot       (旧: sig.obi_s)
                #   sig.obi_put        (旧: sig.obi_p)
                #   sig.call_bid       (新增，直接可用)
                #   sig.put_ask        (新增，直接可用)
                #   sig.etf_ask        (新增，直接可用)
                ul_groups: Dict[str, list] = {}
                for sig in signals:
                    ul_groups.setdefault(sig.underlying, []).append(sig)

                underlyings_data: Dict[str, Any] = {}
                for ul in UNDERLYINGS:
                    sigs = ul_groups.get(ul, [])
                    etf_price = etf_display.get(ul)
                    n_pairs_ul = sum(1 for p in (pairs or []) if p[0].underlying_code == ul)
                    sigs_sorted = sorted(sigs, key=lambda s: (s.expiry, s.multiplier, s.strike))
                    today = now.date()
                    expiry_info: Dict[str, Any] = {}
                    for sig in sigs_sorted:
                        exp_str = sig.expiry.strftime("%Y-%m-%d")
                        if exp_str not in expiry_info:
                            expiry_info[exp_str] = {
                                "cal_days":   (sig.expiry - today).days + 1,
                                "trade_days": trading_days_until(sig.expiry, today),
                            }
                    signals_out = []
                    for sig in sigs_sorted:
                        signals_out.append({
                            "strike":      sig.strike,
                            "expiry":      sig.expiry.strftime("%Y-%m-%d"),
                            "multiplier":  sig.multiplier,
                            "is_adjusted": sig.is_adjusted,
                            "net_profit":  int(round(sig.net_profit)),          # ← sig.net_profit
                            "net_1tick":   int(round(sig.net_1tick))  if sig.net_1tick   is not None else None,
                            "tolerance":   round(sig.tolerance, 2)    if sig.tolerance   is not None else None,
                            "max_qty":     round(sig.max_qty, 1)       if sig.max_qty     is not None else None,
                            "spread_ratio":round(sig.spread_ratio, 4)  if sig.spread_ratio is not None else None,
                            "obi_c":       round(sig.obi_call, 3)      if sig.obi_call    is not None else None,  # ← obi_call
                            "obi_s":       round(sig.obi_spot, 3)      if sig.obi_spot    is not None else None,  # ← obi_spot
                            "obi_p":       round(sig.obi_put, 3)       if sig.obi_put     is not None else None,  # ← obi_put
                            "call_bid":    sig.call_bid,
                            "put_ask":     sig.put_ask,
                            "etf_ask":     sig.etf_ask,
                        })
                    underlyings_data[ul] = {
                        "name": ETF_CODE_TO_NAME.get(ul, ul),
                        "spot": round(etf_price, 4) if etf_price else None,
                        "n_pairs": n_pairs_ul,
                        "n_quoted": len(sigs),
                        "n_positive": sum(1 for s in sigs if s.net_profit >= 0),
                        "signals": signals_out,
                        "expiry_info": expiry_info,
                        "ivs": {},
                    }

                # ── 提取各品种各到期日 ATM IV ──────────────────────
                try:
                    rich_snap = get_rich_snapshot()
                    for ul in UNDERLYINGS:
                        ul_rich = rich_snap.get(ul, {})
                        ivs_out: Dict[str, Any] = {}
                        for exp_str, exp_data in ul_rich.get("expiries", {}).items():
                            atm_strike = exp_data.get("atm_strike")
                            primary_ivs = exp_data.get("primary_ivs", [])
                            T_days = exp_data.get("T_days")
                            if not primary_ivs or atm_strike is None:
                                continue
                            closest = min(primary_ivs, key=lambda x: abs(x["strike"] - atm_strike))
                            ivs_out[exp_str] = {
                                "atm_iv": round(closest["iv"], 4),
                                "T_days": round(T_days, 1) if T_days is not None else None,
                            }
                        underlyings_data[ul]["ivs"] = ivs_out
                except Exception as e:
                    logger.debug("market_cache_monitor: IV 提取异常: %s", e)

                result = {"ts": int(now_ts * 1000), "underlyings": underlyings_data}

                with _monitor_cache_lock:
                    _monitor_cache.clear()
                    _monitor_cache.update(result)

                if _event_loop is not None and _monitor_queue_ref is not None:
                    _event_loop.call_soon_threadsafe(_try_put, _monitor_queue_ref, result)

            except Exception as _exc:
                logger.exception("market_cache_monitor: 顶层未捕获异常（线程继续）: %s", _exc)

    if sock is not None:
        try:
            sock.close(linger=0)
        except Exception:
            pass


def start(
    zmq_port: int = 5555,
    snapshot_dir: Optional[str] = None,
    event_loop: Optional[asyncio.AbstractEventLoop] = None,
    update_queue: Optional[asyncio.Queue] = None,
    monitor_queue: Optional[asyncio.Queue] = None,
) -> None:
    """启动 ZMQ 订阅 + 计算后台线程（幂等，重复调用无副作用）。"""
    global _running, _thread, _compute_thread, _monitor_thread, _event_loop, _update_queue, _monitor_queue_ref
    global _zmq_port, _snapshot_dir

    if _running and _thread is not None and _thread.is_alive():
        return

    if snapshot_dir is None:
        from config.settings import DEFAULT_MARKET_DATA_DIR
        snapshot_dir = DEFAULT_MARKET_DATA_DIR

    _event_loop        = event_loop
    _update_queue      = update_queue
    _monitor_queue_ref = monitor_queue
    _zmq_port          = zmq_port
    _snapshot_dir      = snapshot_dir
    _running = True
    _start_time = time.time()

    _thread = threading.Thread(
        target=_zmq_loop,
        args=(zmq_port, snapshot_dir),
        daemon=True,
        name="market-cache-zmq",
    )
    _compute_thread = threading.Thread(
        target=_compute_loop,
        daemon=True,
        name="market-cache-compute",
    )
    _monitor_thread = threading.Thread(
        target=_monitor_compute_loop,
        daemon=True,
        name="market-cache-monitor",
    )
    _thread.start()
    _compute_thread.start()
    _monitor_thread.start()
    logger.info("market_cache: 已启动 zmq + compute + monitor 线程 (port=%d)", zmq_port)


def stop() -> None:
    """停止后台线程。"""
    global _running
    _running = False
    for t in (_thread, _compute_thread, _monitor_thread):
        if t is not None:
            t.join(timeout=2.0)
    logger.info("market_cache: 已停止")
