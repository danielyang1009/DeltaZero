#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线自检：验证 DDE 健康检测链路（ACTIVE -> STALE -> 熔断 -> 恢复）。
不依赖真实 DDE 连接。
"""

from __future__ import annotations

import time
from queue import Queue
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_bus.dde_subscriber import DDESubscriber
from data_engine.dde_adapter import RouteEntry


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    sub = DDESubscriber(products=["510050.SH"], tick_queue=Queue(), poll_interval=0.5, staleness_timeout=1.0)

    sub._routes = {
        "510050.SH": RouteEntry(
            contract_code="510050.SH",
            server="QD",
            topic="ETF_TOPIC",
            option_type="ETF",
            strike="",
            source_file="mock",
            underlying="510050.SH",
        ),
        "10000001": RouteEntry(
            contract_code="10000001",
            server="QD",
            topic="OPT_CALL",
            option_type="CALL",
            strike="2.90",
            source_file="mock",
            underlying="510050.SH",
        ),
        "10000002": RouteEntry(
            contract_code="10000002",
            server="QD",
            topic="OPT_PUT",
            option_type="PUT",
            strike="2.90",
            source_file="mock",
            underlying="510050.SH",
        ),
    }
    sub._build_code_maps_from_routes()

    now = time.time()
    for code in sub._routes:
        sub._last_change_ts[code] = now
        sub._contract_status[code] = "ACTIVE"
    sub._product_fused["510050.SH"] = False

    # 第 1 轮：初始化为 ACTIVE
    data_1 = {
        "510050.SH": {"LASTPRICE": 2.90, "BIDPRICE1": 2.89, "ASKPRICE1": 2.91},
        "10000001": {"LASTPRICE": 0.10, "BIDPRICE1": 0.09, "ASKPRICE1": 0.11},
        "10000002": {"LASTPRICE": 0.12, "BIDPRICE1": 0.11, "ASKPRICE1": 0.13},
    }
    sub._update_staleness(data_1)
    _assert(sub.is_trading_safe("510050.SH"), "初始化后应可交易")

    # 第 2 轮：超时且数值不变 -> STALE + 熔断
    time.sleep(1.2)
    sub._update_staleness(data_1)
    report = sub.get_health_report()
    _assert(report["stale_seconds"], "应返回 stale 秒数")
    _assert(not sub.is_trading_safe("510050.SH"), "陈旧后应触发熔断，不可交易")

    # 第 3 轮：核心合约数值恢复跳动 -> 解除熔断
    data_2 = {
        "510050.SH": {"LASTPRICE": 2.90, "BIDPRICE1": 2.89, "ASKPRICE1": 2.91},
        "10000001": {"LASTPRICE": 0.11, "BIDPRICE1": 0.10, "ASKPRICE1": 0.12},
        "10000002": {"LASTPRICE": 0.13, "BIDPRICE1": 0.12, "ASKPRICE1": 0.14},
    }
    sub._update_staleness(data_2)
    _assert(sub.is_trading_safe("510050.SH"), "恢复跳动后应解除熔断")

    print("OK: DDE health guard self-check passed.")


if __name__ == "__main__":
    main()
