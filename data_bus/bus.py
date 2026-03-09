# -*- coding: utf-8 -*-
"""
数据记录主进程

职责：
  1. 启动 Wind 订阅（Push 回调模式）
  2. 从 tick 队列消费数据：写 Parquet 缓冲区 + ZMQ 广播
  3. 每 flush_interval_secs 秒将缓冲区刷入磁盘（分片 Parquet）
  4. 更新 snapshot_latest.parquet（供策略进程冷启动恢复）
  5. 15:10 触发日终合并，将当日所有分片合并为日文件
  6. Ctrl+C 优雅退出：先刷新剩余数据，再合并

运行方式：
    python -m data_bus.bus                              # 默认配置
    python -m data_bus.bus --port 5556                  # 自定义 ZMQ 端口
    python -m data_bus.bus --flush 60                   # 每60秒刷新一次
    python -m data_bus.bus --output D:\\MARKET_DATA     # 自定义存储目录
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

# 将项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from monitors.common import fix_windows_encoding
fix_windows_encoding()

from config.settings import get_recorder_config, RecorderConfig
from data_bus.parquet_writer import ParquetWriter
from data_bus.wind_subscriber import WindSubscriber
from data_bus.dde_subscriber import DDESubscriber
from data_bus.zmq_publisher import ZMQPublisher
from models import TickPacket
from utils.time_utils import bj_now_naive

# ──────────────────────────────────────────────────────────────────────
# 日志配置
# ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("databus")


def _in_trading_hours(now: datetime) -> bool:
    t = now.time()
    return ((t.hour == 9 and t.minute >= 30) or (10 <= t.hour < 11) or (t.hour == 11 and t.minute <= 30)
            or (13 <= t.hour < 15))


# ──────────────────────────────────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────────────────────────────────

def run(config: RecorderConfig, source: str = "wind", persist: bool = True, dde_mode: str = "advise") -> None:
    """数据记录主循环"""

    logger.info("=" * 60)
    logger.info("%s 数据记录进程启动", source.upper())
    logger.info("  配置品种: %s", config.products)
    logger.info("  存储目录: %s", config.output_dir)
    logger.info("  落盘模式: %s", "开启" if persist else "关闭（仅总线广播）")
    logger.info("  ZMQ 端口: %d", config.zmq_port)
    logger.info("  刷新间隔: %d 秒", config.flush_interval_secs)
    logger.info("  合并时间: %02d:%02d", config.merge_hour, config.merge_minute)
    logger.info("=" * 60)

    # ── 1. 初始化各组件 ──────────────────────────────────────────────
    tick_queue = Queue(maxsize=config.queue_maxsize)

    writer: Optional[ParquetWriter] = (
        ParquetWriter(config.output_dir, config.flush_interval_secs) if persist else None
    )
    publisher = ZMQPublisher(config.zmq_port)
    if source == "dde":
        subscriber = DDESubscriber(
            products=config.products,
            tick_queue=tick_queue,
            poll_interval=max(1.0, config.flush_interval_secs / 10),
            mode=dde_mode,
        )
    else:
        subscriber = WindSubscriber(
            products        = config.products,
            tick_queue      = tick_queue,
            batch_size      = config.batch_size,
            max_expiry_days = config.max_expiry_days,
        )

    # ── 2. 启动数据订阅 ──────────────────────────────────────────────
    logger.info("正在启动 %s 数据订阅...", source.upper())
    if not subscriber.start():
        logger.error("%s 订阅启动失败，退出", source.upper())
        publisher.close()
        return

    if source == "dde":
        etf_count = getattr(subscriber, "etf_count", 0)
        active_underlyings = getattr(subscriber, "active_underlyings", [])
        logger.info(
            "订阅完成：%d 个期权合约 + %d 个 ETF（实际标的: %s）",
            subscriber.option_count,
            etf_count,
            active_underlyings or "N/A",
        )
    else:
        logger.info(
            "订阅完成：%d 个期权合约 + %d 个 ETF",
            subscriber.option_count,
            len(config.products),
        )
    logger.info("开始接收行情，按 Ctrl+C 退出...")

    # ── 3. 主循环 ──────────────────────────────────────────────────────
    stats_received   = 0        # 本次刷新周期内收到的 tick 数
    stats_total      = 0        # 累计 tick 数
    merge_done_today = False    # 日终合并是否已在今日执行
    start_ts = bj_now_naive()
    dde_self_check_done = False
    dde_underlyings_seen = set()
    dde_etf_codes_seen = set()

    try:
        while True:
            now = bj_now_naive()

            # 3a. 从队列消费 tick（每次最多处理 1000 条，避免单次循环过长）
            processed = 0
            while processed < 1000:
                try:
                    pkt: TickPacket = tick_queue.get_nowait()
                except Empty:
                    break

                in_hours = _in_trading_hours(now)
                if pkt.is_etf:
                    if writer is not None and (source != "dde" or in_hours):
                        writer.on_etf_tick(pkt.tick_row)
                    publisher.publish_etf(pkt.tick_obj)
                    if source == "dde":
                        dde_etf_codes_seen.add(str(pkt.tick_row.get("code", "")))
                else:
                    if writer is not None and (source != "dde" or in_hours):
                        writer.on_option_tick(pkt.tick_row)
                    publisher.publish_option(pkt.tick_obj, pkt.underlying_code)
                    if source == "dde":
                        dde_underlyings_seen.add(str(pkt.tick_row.get("underlying", "")))

                processed += 1

            stats_received += processed
            stats_total    += processed

            # 3b. 定时刷新 Parquet 分片
            if writer is not None and writer.should_flush():
                written = writer.flush(now)
                if written:
                    logger.info(
                        "分片写入: %d 条 | 本周期接收: %d | 累计: %d | 队列剩余: %d",
                        written, stats_received, stats_total, tick_queue.qsize(),
                    )
                stats_received = 0

            # 3c. 日终合并（触发一次后当天不再重复）
            if (not merge_done_today
                    and now.hour == config.merge_hour
                    and now.minute >= config.merge_minute):
                if writer is not None:
                    logger.info("触发日终合并...")
                    writer.flush(now)          # 先把剩余数据写入最后一个分片
                    writer.merge_daily(now.date())
                    logger.info("日终合并完成")
                merge_done_today = True

            # 日期切换：重置合并标志
            if merge_done_today and now.hour == 9:
                merge_done_today = False

            # 3d. 状态心跳（每 60 秒一次）
            _maybe_heartbeat(stats_total, tick_queue.qsize(), writer, now)

            # 3e. DDE 启动后 30 秒自检（仅打印一次）
            if (
                source == "dde"
                and (not dde_self_check_done)
                and (now - start_ts).total_seconds() >= 30
            ):
                dde_self_check_done = True
                logger.info(
                    "DDE 自检(30s): 累计=%d tick, 期权标的=%s, ETF代码=%s",
                    stats_total,
                    sorted([u for u in dde_underlyings_seen if u]),
                    sorted([c for c in dde_etf_codes_seen if c]),
                )

            # 没有 tick 时短暂休眠，避免空转
            if processed == 0:
                time.sleep(0.05)

    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，开始优雅退出...")

    finally:
        # ── 4. 优雅退出 ───────────────────────────────────────────────
        if writer is not None:
            logger.info("正在刷新剩余 %d 条 tick 到磁盘...", tick_queue.qsize())
        else:
            logger.info("正在清空剩余 %d 条 tick（不落盘）...", tick_queue.qsize())

        # 清空队列剩余 tick
        while not tick_queue.empty():
            try:
                pkt = tick_queue.get_nowait()
                if writer is not None:
                    if pkt.is_etf:
                        writer.on_etf_tick(pkt.tick_row)
                    else:
                        writer.on_option_tick(pkt.tick_row)
            except Empty:
                break

        if writer is not None:
            writer.flush(bj_now_naive())

        # 如果在交易时间后退出，自动执行日终合并
        now = bj_now_naive()
        if writer is not None and now.hour >= config.merge_hour and not merge_done_today:
            logger.info("退出时触发日终合并...")
            writer.merge_daily(now.date())

        subscriber.stop()
        publisher.close()
        logger.info("数据记录进程已退出。累计接收 %d 条 tick。", stats_total)


# ──────────────────────────────────────────────────────────────────────
# 心跳状态输出
# ──────────────────────────────────────────────────────────────────────

_last_heartbeat: datetime = datetime.min

def _maybe_heartbeat(total: int, queue_size: int, writer: Optional[ParquetWriter], now: datetime) -> None:
    global _last_heartbeat
    if (now - _last_heartbeat).total_seconds() < 60:
        return
    _last_heartbeat = now
    if writer is None:
        logger.info(
            "心跳 %s | 累计 %d tick | 队列 %d | 落盘关闭",
            now.strftime("%H:%M:%S"),
            total,
            queue_size,
        )
        return
    logger.info(
        "心跳 %s | 累计 %d tick | 队列 %d | 缓冲 opt=%d etf=%d",
        now.strftime("%H:%M:%S"),
        total,
        queue_size,
        writer.opt_buffer_len,
        writer.etf_buffer_len,
    )


# ──────────────────────────────────────────────────────────────────────
# 命令行入口
# ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="实时数据记录进程（支持 wind / dde）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m data_bus.bus
  python -m data_bus.bus --flush 60 --output D:\\MARKET_DATA
  python -m data_bus.bus --port 5556
  python -m data_bus.bus --new-window
        """,
    )
    parser.add_argument("--output", type=str, default=None,
                        help="Parquet 存储目录（默认 D:\\MARKET_DATA，可覆盖）")
    parser.add_argument("--port",  type=int, default=None,
                        help="ZMQ PUB 端口（默认: 5555）")
    parser.add_argument("--flush", type=int, default=None,
                        help="分片刷新间隔秒数（默认: 30）")
    parser.add_argument("--batch", type=int, default=None,
                        help="wsq 每批代码数（默认: 80）")
    parser.add_argument("--source", choices=["wind", "dde"], default="wind",
                        help="数据源类型（默认: wind）")
    parser.add_argument("--mode", choices=["advise", "request"], default="advise",
                        help="DDE 模式: advise=热链接推送, request=轮询（默认: advise）")
    parser.add_argument("--no-persist", action="store_true",
                        help="仅做总线广播，不写 Parquet 磁盘文件")
    parser.add_argument("--new-window", action="store_true",
                        help="在新终端窗口中启动（仅 Windows）")
    return parser.parse_args()


def _relaunch_in_new_window() -> bool:
    """若 sys.argv 含 --new-window，在新 cmd 窗口重启本脚本并退出。仅 Windows 有效。"""
    if "--new-window" not in sys.argv:
        return False
    if sys.platform == "win32":
        import subprocess
        cmd = [sys.executable, str(Path(__file__).resolve())] + [
            a for a in sys.argv[1:] if a != "--new-window"
        ]
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


def main() -> None:
    if _relaunch_in_new_window():
        return

    args = _parse_args()
    config = get_recorder_config()

    if args.output:
        config.output_dir = args.output
    if args.port:
        config.zmq_port = args.port
    if args.flush:
        config.flush_interval_secs = args.flush
    if args.batch:
        config.batch_size = args.batch

    config.persist = not args.no_persist
    run(config, source=args.source, persist=config.persist, dde_mode=args.mode)


if __name__ == "__main__":
    main()
