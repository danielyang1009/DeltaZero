# -*- coding: utf-8 -*-
"""web/market_cache.py — FastAPI 内存行情缓存层（ZMQ SUB 后台线程）。

维护一份 {code → tick_dict} 的 LKV（Last Known Value），供所有 FastAPI 端点共享。
DataBus 未运行时从 snapshot_latest.parquet 冷启动填充。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lkv: Dict[str, Dict[str, Any]] = {}
_lkv_lock = threading.Lock()
_running = False
_thread: Optional[threading.Thread] = None


def get_snapshot() -> Dict[str, Dict[str, Any]]:
    """返回当前内存快照的浅拷贝，供 API 端点安全读取。"""
    with _lkv_lock:
        return dict(_lkv)


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
    logger.info("market_cache: 后台线程已退出")


def start(zmq_port: int = 5555, snapshot_dir: Optional[str] = None) -> None:
    """启动 ZMQ 订阅后台线程（幂等，重复调用无副作用）。"""
    global _running, _thread

    if _running and _thread is not None and _thread.is_alive():
        return

    if snapshot_dir is None:
        from config.settings import DEFAULT_MARKET_DATA_DIR
        snapshot_dir = DEFAULT_MARKET_DATA_DIR

    _running = True
    _thread = threading.Thread(
        target=_zmq_loop,
        args=(zmq_port, snapshot_dir),
        daemon=True,
        name="market-cache-zmq",
    )
    _thread.start()
    logger.info("market_cache: 后台线程已启动 (ZMQ port=%d)", zmq_port)


def stop() -> None:
    """停止后台线程。"""
    global _running
    _running = False
    if _thread is not None:
        _thread.join(timeout=2.0)
    logger.info("market_cache: 已停止")
