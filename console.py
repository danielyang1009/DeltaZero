#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeltaZero 统一控制台入口。"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser

from web.dashboard import main as dashboard_main
from web.process_manager import find_monitor_processes, find_recorder_processes, spawn_module


def _open_default_browser(path: str = "/") -> None:
    # 给 uvicorn 一点启动时间，再自动打开默认浏览器
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:8787{path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeltaZero 控制台统一入口")
    parser.add_argument(
        "--bus-source",
        choices=["none", "wind", "dde"],
        default="none",
        help="可选：启动 DataBus（none/wind/dde，默认 none）",
    )
    parser.add_argument(
        "--start-monitor-zmq",
        action="store_true",
        help="可选：启动 Monitor（source=zmq）",
    )
    parser.add_argument(
        "--start-dde-pipeline",
        action="store_true",
        help="一键启动 DDE 链路（等价于 --bus-source dde --start-monitor-zmq）",
    )
    parser.add_argument("--zmq-port", type=int, default=5555, help="ZMQ 端口（默认 5555）")
    parser.add_argument(
        "--open",
        choices=["index", "dde"],
        default="index",
        help="自动打开页面（index 或 dde）",
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    # 透传给 dashboard 的参数
    parser.add_argument("--host", default="127.0.0.1", help="Web 监听地址")
    parser.add_argument("--port", type=int, default=8787, help="Web 监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    return parser.parse_args()


def _bootstrap_processes(args: argparse.Namespace) -> None:
    if args.start_dde_pipeline:
        args.bus_source = "dde"
        args.start_monitor_zmq = True

    if args.bus_source != "none":
        if find_recorder_processes():
            print("[console] DataBus 已在运行，跳过启动")
        else:
            started = spawn_module("data_bus.bus", ["--source", args.bus_source, "--port", str(args.zmq_port)])
            print(f"[console] 已启动 {args.bus_source.upper()} DataBus，PID={started['pid']}")

    if args.start_monitor_zmq:
        if find_monitor_processes():
            print("[console] Monitor 已在运行，跳过启动")
        else:
            started = spawn_module(
                "monitors.monitor",
                ["--zmq-port", str(args.zmq_port), "--refresh", "3"],
            )
            print(f"[console] 已启动 Monitor，PID={started['pid']}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    args = _parse_args()

    _bootstrap_processes(args)

    if not args.no_browser:
        target = "/" if args.open == "index" else "/dde"
        threading.Thread(target=_open_default_browser, args=(target,), daemon=True).start()

    # 重写 argv，只保留 dashboard 识别的参数，避免 argparse 冲突
    sys.argv = [
        sys.argv[0],
        "--host",
        args.host,
        "--port",
        str(args.port),
    ] + (["--reload"] if args.reload else [])
    dashboard_main()
