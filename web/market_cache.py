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
from datetime import datetime, time as _time
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
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_update_queue: Optional[asyncio.Queue] = None
_start_time: Optional[float] = None


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

                for flag_val, side, label in ((+1, calls, "C"), (-1, puts, "P")):
                    strikes = sorted(side.keys())
                    if not strikes:
                        continue
                    K_arr    = np.array(strikes)
                    mid_arr  = np.array([side[k]["mid"] for k in strikes])
                    bid_arr  = np.array([side[k]["bid"] for k in strikes])
                    ask_arr  = np.array([side[k]["ask"] for k in strikes])
                    flag_arr = np.full(len(strikes), float(flag_val))

                    iv_arr     = pricer.calc_iv(F, K_arr, T, r, mid_arr,  flag_arr)
                    bid_iv_arr = pricer.calc_iv(F, K_arr, T, r, bid_arr,  flag_arr)
                    ask_iv_arr = pricer.calc_iv(F, K_arr, T, r, ask_arr,  flag_arr)

                    if label == "C":
                        for k, iv in zip(strikes, iv_arr):
                            call_iv_map[k] = float(iv)

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

                entry = {
                    "F": round(F, 6), "T_days": round(T * 365.25, 4),
                    "r": round(r, 6), "atm_strike": K_atm,
                    "contracts": contracts_out,
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


def start(
    zmq_port: int = 5555,
    snapshot_dir: Optional[str] = None,
    event_loop: Optional[asyncio.AbstractEventLoop] = None,
    update_queue: Optional[asyncio.Queue] = None,
) -> None:
    """启动 ZMQ 订阅 + 计算后台线程（幂等，重复调用无副作用）。"""
    global _running, _thread, _compute_thread, _event_loop, _update_queue

    if _running and _thread is not None and _thread.is_alive():
        return

    if snapshot_dir is None:
        from config.settings import DEFAULT_MARKET_DATA_DIR
        snapshot_dir = DEFAULT_MARKET_DATA_DIR

    _event_loop   = event_loop
    _update_queue = update_queue
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
    _thread.start()
    _compute_thread.start()
    logger.info("market_cache: 已启动 zmq + compute 线程 (port=%d)", zmq_port)


def stop() -> None:
    """停止后台线程。"""
    global _running
    _running = False
    for t in (_thread, _compute_thread):
        if t is not None:
            t.join(timeout=2.0)
    logger.info("market_cache: 已停止")
