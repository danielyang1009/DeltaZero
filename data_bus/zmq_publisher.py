# -*- coding: utf-8 -*-
"""
ZeroMQ 行情广播器

将 Wind 推送的 tick 数据以 PUB/SUB 模式实时广播给所有订阅方（策略进程、监控进程等）。

主题（topic）格式：
    "OPT_510050"  —— 510050.SH 标的下的期权 tick
    "OPT_510300"  —— 510300.SH 标的下的期权 tick
    "OPT_510500"  —— 510500.SH 标的下的期权 tick
    "ETF_510050"  —— 50ETF 自身 tick
    "ETF_510300"  —— 300ETF 自身 tick
    "ETF_510500"  —— 500ETF 自身 tick

消息格式（JSON 字符串，紧跟在 topic 之后，空格分隔）：
    "OPT_510050 {"type":"option","code":"10000001.SH","underlying":"510050.SH",
                 "ts":1709430000000,"last":0.3456,"ask1":0.3460,"bid1":0.3450,
                 "oi":12345,"vol":678,"high":0.3500,"low":0.3400}"
"""

from __future__ import annotations

import json
import logging
import math
from typing import Optional

from models import ETFTickData, TickData

logger = logging.getLogger(__name__)


class ZMQPublisher:
    """
    ZeroMQ PUB 广播器

    绑定到 tcp://127.0.0.1:{port}，策略进程用 SUB socket connect 到同一地址。
    广播器生命周期应与数据记录进程一致。
    """

    def __init__(self, port: int = 5555) -> None:
        try:
            import zmq
            self._zmq = zmq
            self._ctx = zmq.Context.instance()
            self._sock = self._ctx.socket(zmq.PUB)
            self._sock.bind(f"tcp://127.0.0.1:{port}")
            self._enabled = True
            logger.info("ZMQ PUB 绑定端口 %d", port)
        except ImportError:
            logger.warning("pyzmq 未安装，ZMQ 广播已禁用。请执行：pip install pyzmq")
            self._enabled = False

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def publish_option(self, tick: TickData, underlying_code: str) -> None:
        """广播期权 tick"""
        if not self._enabled:
            return
        prefix = underlying_code.split(".")[0]
        msg = {
            "type":       "option",
            "code":       tick.contract_code,
            "underlying": underlying_code,
            "ts":         int(tick.timestamp.timestamp() * 1000),
            "last":       _safe_float(tick.current),
            "ask1":       _safe_float(tick.ask_prices[0]),
            "bid1":       _safe_float(tick.bid_prices[0]),
            "oi":         tick.position,
            "vol":        tick.volume,
            "high":       _safe_float(tick.high),
            "low":        _safe_float(tick.low),
        }
        self._send(f"OPT_{prefix}", msg)

    def publish_etf(self, tick: ETFTickData) -> None:
        """广播 ETF tick"""
        if not self._enabled:
            return
        prefix = tick.etf_code.split(".")[0]
        msg = {
            "type": "etf",
            "code": tick.etf_code,
            "ts":   int(tick.timestamp.timestamp() * 1000),
            "last": _safe_float(tick.price),
            "ask1": _safe_float(tick.ask_price),
            "bid1": _safe_float(tick.bid_price),
        }
        self._send(f"ETF_{prefix}", msg)

    def close(self) -> None:
        """关闭 ZMQ socket"""
        if self._enabled:
            try:
                self._sock.close(linger=100)
            except Exception:
                pass
            self._enabled = False
            logger.info("ZMQ PUB 已关闭")

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────

    def _send(self, topic: str, payload: dict) -> None:
        try:
            self._sock.send_string(f"{topic} {json.dumps(payload)}", flags=self._zmq.NOBLOCK)
        except self._zmq.Again:
            pass  # 无订阅者时不阻塞
        except Exception as e:
            logger.warning("ZMQ 发送失败: %s", e)


def _safe_float(v: float) -> Optional[float]:
    """将 NaN 转换为 None，方便 JSON 序列化"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)
