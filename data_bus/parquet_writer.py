# -*- coding: utf-8 -*-
"""
Parquet 分片写入器

写入策略（崩溃安全设计）：
  - 内存缓冲区按品种分别保存，每 flush_interval_secs 秒写一个完整 Parquet 分片
  - 每个分片完全关闭后才开始下一个，保证每个分片文件均可独立读取
  - 崩溃最多丢失最近 flush_interval_secs 秒的数据
  - 每次刷新同步更新 snapshot_latest.parquet（每个合约只保留最新一条）

关于 15:00 后仍产生分片的原因：
  Wind wsq Push 在收盘后仍会推送 tick（如收盘价、心跳等），recorder 不做交易时间过滤，
  所有接收到的数据都会写入。因此 15:10 合并后，若 recorder 继续运行，会持续产生新分片。
  日终合并时会过滤掉非交易时间（9:30-11:30、13:00-15:00）的 tick。

目录结构：
    {output_dir}/
    ├── chunks/
    │   ├── 510050/
    │   │   ├── options_20260303_093000.parquet   ← 期权分片（按品种 + 时间命名）
    │   │   └── etf_20260303_093000.parquet
    │   ├── 510300/
    │   └── 510500/
    ├── 510050/
    │   ├── options_20260303.parquet              ← 日终合并后的日文件
    │   └── etf_20260303.parquet
    ├── 510300/
    ├── 510500/
    └── snapshot_latest.parquet                   ← 全量快照（供策略冷启动使用）

Parquet Schema：
    options: ts(int64), code(str), underlying(str),
             last(float32), ask1(float32), bid1(float32),
             askv1(int16), bidv1(int16),
             oi(int32), vol(int32), high(float32), low(float32),
             is_adjusted(bool), multiplier(int32)
    etf:     ts(int64), code(str),
             last(float32), ask1(float32), bid1(float32),
             askv1(int32), bidv1(int32)             ← ETF 档量以股计，保留 int32
    snapshot: type(str), code(str), underlying(str),
              ts(int64), last(float32), ask1(float32), bid1(float32),
              askv1(int16), bidv1(int16),
              oi(int32), vol(int32), high(float32), low(float32),
              is_adjusted(bool), multiplier(int32)

压缩：zstd（压缩比优于 snappy，读速相近）
"""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
from utils.time_utils import bj_now_naive, bj_today

# ──────────────────────────────────────────────────────────────────────
# PyArrow Schema 定义（延迟导入，不强制依赖）
# ──────────────────────────────────────────────────────────────────────

def _get_option_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("ts",          pa.int64()),
        pa.field("code",        pa.string()),
        pa.field("underlying",  pa.string()),
        pa.field("last",        pa.float32()),
        pa.field("ask1",        pa.float32()),
        pa.field("bid1",        pa.float32()),
        pa.field("askv1",       pa.int16()),   # 期权每档量极少超 32767 手
        pa.field("bidv1",       pa.int16()),
        pa.field("oi",          pa.int32()),
        pa.field("vol",         pa.int32()),
        pa.field("high",        pa.float32()),
        pa.field("low",         pa.float32()),
        pa.field("is_adjusted", pa.bool_()),   # True = ETF 分红后调整型合约
        pa.field("multiplier", pa.int32()),   # 真实合约乘数（标准 10000 / 调整型如 10265）
    ])

def _get_etf_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("ts",   pa.int64()),
        pa.field("code", pa.string()),
        pa.field("last", pa.float32()),
        pa.field("ask1", pa.float32()),
        pa.field("bid1", pa.float32()),
        pa.field("askv1", pa.int32()),
        pa.field("bidv1", pa.int32()),
    ])

def _get_snapshot_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("type",        pa.string()),   # "option" or "etf"
        pa.field("code",        pa.string()),
        pa.field("underlying",  pa.string()),
        pa.field("ts",          pa.int64()),
        pa.field("last",        pa.float32()),
        pa.field("ask1",        pa.float32()),
        pa.field("bid1",        pa.float32()),
        pa.field("askv1",       pa.int16()),   # 期权每档量极少超 32767 手
        pa.field("bidv1",       pa.int16()),
        pa.field("oi",          pa.int32()),
        pa.field("vol",         pa.int32()),
        pa.field("high",        pa.float32()),
        pa.field("low",         pa.float32()),
        pa.field("is_adjusted", pa.bool_()),    # True = 调整型合约
        pa.field("multiplier", pa.int32()),   # 合约乘数
    ])


# ──────────────────────────────────────────────────────────────────────
# ParquetWriter
# ──────────────────────────────────────────────────────────────────────

class ParquetWriter:
    """
    带分片写入、崩溃安全保证和日终合并的 Parquet 写入器。

    线程安全：buffer 和 snapshot 的访问由 _lock 保护，
    供 Wind 回调线程（写入）和定时刷新线程（读取+清空）共同使用。
    """

    def __init__(self, output_dir: str, flush_interval_secs: int = 30) -> None:
        self._root = Path(output_dir)
        self._chunks_dir = self._root / "chunks"
        self._flush_interval = flush_interval_secs

        self._root.mkdir(parents=True, exist_ok=True)
        self._chunks_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓冲区（列表存 dict，按类型分开）
        self._opt_buffer:  List[Dict[str, Any]] = []
        self._etf_buffer:  List[Dict[str, Any]] = []

        # 最新快照字典：code → snapshot_row_dict
        self._snapshot:    Dict[str, Dict[str, Any]] = {}

        self._lock = threading.Lock()
        self._last_flush_time = bj_now_naive()

        logger.info("ParquetWriter 初始化：%s  刷新间隔 %ds", self._root, flush_interval_secs)

    # ──────────────────────────────────────────────────────────
    # 数据写入接口（由 Wind 回调线程调用）
    # ──────────────────────────────────────────────────────────

    def on_option_tick(self, tick_row: Dict[str, Any]) -> None:
        """
        缓冲一条期权 tick。

        Args:
            tick_row: 包含 ts/code/underlying/last/ask1/bid1/oi/vol/high/low 的字典
        """
        with self._lock:
            self._opt_buffer.append(tick_row)
            self._snapshot[tick_row["code"]] = {**tick_row, "type": "option"}

    def on_etf_tick(self, tick_row: Dict[str, Any]) -> None:
        """
        缓冲一条 ETF tick。

        Args:
            tick_row: 包含 ts/code/last/ask1/bid1 的字典
        """
        with self._lock:
            self._etf_buffer.append(tick_row)
            self._snapshot[tick_row["code"]] = {
                **tick_row,
                "type":        "etf",
                "underlying":  tick_row["code"],
                "askv1":       0,
                "bidv1":       0,
                "oi":          0,
                "vol":         0,
                "high":        tick_row.get("last", 0.0),
                "low":         tick_row.get("last", 0.0),
                "is_adjusted": False,
                "multiplier":  0,
            }

    # ──────────────────────────────────────────────────────────
    # 刷新（定时由主线程调用）
    # ──────────────────────────────────────────────────────────

    def should_flush(self) -> bool:
        """是否到了刷新时间"""
        elapsed = (bj_now_naive() - self._last_flush_time).total_seconds()
        return elapsed >= self._flush_interval

    def flush(self, dt: Optional[datetime] = None) -> int:
        """
        将内存缓冲区写入分片文件并更新 snapshot_latest.parquet。

        Returns:
            本次写入的总 tick 行数
        """
        if dt is None:
            dt = bj_now_naive()

        with self._lock:
            opt_rows  = self._opt_buffer[:]
            etf_rows  = self._etf_buffer[:]
            snap_rows = list(self._snapshot.values())
            self._opt_buffer.clear()
            self._etf_buffer.clear()

        total = 0
        ts_str = dt.strftime("%H%M%S")
        date_str = dt.strftime("%Y%m%d")

        if opt_rows:
            by_ul: Dict[str, List[Dict]] = defaultdict(list)
            for row in opt_rows:
                by_ul[row["underlying"].replace(".SH", "")].append(row)
            for ul, rows in by_ul.items():
                ul_dir = self._chunks_dir / ul
                ul_dir.mkdir(parents=True, exist_ok=True)
                path = ul_dir / f"options_{date_str}_{ts_str}.parquet"
                _write_rows(rows, path, _get_option_schema(), _option_row_to_arrays)
                total += len(rows)
                logger.debug("写入期权分片 %s/%s (%d 行)", ul, path.name, len(rows))

        if etf_rows:
            by_ul = defaultdict(list)
            for row in etf_rows:
                by_ul[row["code"].replace(".SH", "")].append(row)
            for ul, rows in by_ul.items():
                ul_dir = self._chunks_dir / ul
                ul_dir.mkdir(parents=True, exist_ok=True)
                path = ul_dir / f"etf_{date_str}_{ts_str}.parquet"
                _write_rows(rows, path, _get_etf_schema(), _etf_row_to_arrays)
                total += len(rows)
                logger.debug("写入 ETF 分片 %s/%s (%d 行)", ul, path.name, len(rows))

        # 更新最新快照文件（仅在收盘前，15:00 后停止覆盖）
        if snap_rows and dt.hour < 15:
            snap_path = self._root / "snapshot_latest.parquet"
            _write_rows(snap_rows, snap_path, _get_snapshot_schema(), _snapshot_row_to_arrays)

        self._last_flush_time = dt

        if total:
            logger.info("[%s] 刷新完成：%d 条 tick 已写入磁盘", dt.strftime("%H:%M:%S"), total)
        return total

    # ──────────────────────────────────────────────────────────
    # 日终合并（主线程调用，在 15:10 或程序退出时执行）
    # ──────────────────────────────────────────────────────────

    def merge_daily(self, target_date: Optional[date] = None) -> None:
        """
        合并当日所有分片为两个日文件：options_YYYYMMDD.parquet 和 etf_YYYYMMDD.parquet。
        合并成功后删除分片文件。

        仅保留交易时间内的 tick：上交所 9:30-11:30、13:00-15:00。
        Wind 在盘前/盘后仍会推送数据，合并时过滤掉这些无效 tick。
        """
        if target_date is None:
            target_date = bj_today()
        date_str = target_date.strftime("%Y%m%d")

        import pyarrow.parquet as pq
        import pyarrow as pa
        import pyarrow.compute as pc
        import pandas as pd

        for ul_dir in sorted(self._chunks_dir.iterdir()):
            if not ul_dir.is_dir():
                continue
            ul = ul_dir.name  # e.g. "510050"
            out_ul_dir = self._root / ul
            out_ul_dir.mkdir(parents=True, exist_ok=True)

            for prefix in ("options", "etf"):
                chunks = sorted(ul_dir.glob(f"{prefix}_{date_str}_*.parquet"))
                if not chunks:
                    continue

                try:
                    tables = [pq.read_table(str(c)) for c in chunks]
                    merged = pa.concat_tables(tables)
                    idx = pc.sort_indices(merged, sort_keys=[("ts", "ascending")])
                    merged = merged.take(idx)
                    original_count = merged.num_rows

                    # 过滤：仅保留交易时间 9:30-11:30、13:00-15:00
                    if original_count > 0 and "ts" in merged.column_names:
                        df = merged.to_pandas()
                        df["_dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                        df["_dt"] = df["_dt"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
                        df["_h"] = df["_dt"].dt.hour
                        df["_m"] = df["_dt"].dt.minute
                        df["_date_ok"] = df["_dt"].dt.date == target_date
                        mask = df["_date_ok"] & (
                            ((df["_h"] == 9) & (df["_m"] >= 30))
                            | ((df["_h"] == 10))
                            | ((df["_h"] == 11) & (df["_m"] <= 30))
                            | ((df["_h"] == 13))
                            | ((df["_h"] == 14))
                            | ((df["_h"] == 15) & (df["_m"] == 0))
                        )
                        df = df[mask].drop(columns=["_dt", "_h", "_m", "_date_ok"])
                        merged = pa.Table.from_pandas(df, preserve_index=False)
                        n_dropped = original_count - merged.num_rows
                        if n_dropped > 0:
                            logger.info("过滤非交易时间 tick [%s/%s]: %d 条", ul, prefix, n_dropped)

                    out_path = out_ul_dir / f"{prefix}_{date_str}.parquet"
                    pq.write_table(merged, str(out_path), compression="zstd")
                    logger.info("日终合并完成：%s/%s (%d 行，%d 个分片)",
                                ul, out_path.name, merged.num_rows, len(chunks))

                    for c in chunks:
                        c.unlink(missing_ok=True)
                except Exception as e:
                    logger.error("日终合并失败 [%s/%s]: %s", ul, prefix, e)

    # ──────────────────────────────────────────────────────────
    # 快照读取（供策略模块冷启动恢复状态）
    # ──────────────────────────────────────────────────────────

    def load_snapshot(self) -> Optional[object]:
        """
        读取 snapshot_latest.parquet，返回 pandas DataFrame 或 None。

        DataFrame 列：type, code, underlying, ts, last, ask1, bid1, oi, vol, high, low
        """
        snap_path = self._root / "snapshot_latest.parquet"
        if not snap_path.exists():
            return None
        try:
            import pandas as pd
            return pd.read_parquet(str(snap_path))
        except Exception as e:
            logger.warning("快照读取失败: %s", e)
            return None

    @property
    def snapshot_path(self) -> Path:
        return self._root / "snapshot_latest.parquet"

    @property
    def opt_buffer_len(self) -> int:
        with self._lock:
            return len(self._opt_buffer)

    @property
    def etf_buffer_len(self) -> int:
        with self._lock:
            return len(self._etf_buffer)


# ──────────────────────────────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────────────────────────────

def _nan_to_none(v: Any) -> Any:
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _int_or_zero(v: Any) -> int:
    try:
        if v is None:
            return 0
        f = float(v)
        if math.isnan(f):
            return 0
        return int(round(f))
    except Exception:
        return 0


def _write_rows(rows: List[Dict], path: Path, schema, row_to_arrays_fn) -> None:
    """将 dict 列表写入一个完整的 Parquet 文件"""
    import pyarrow as pa
    import pyarrow.parquet as pq
    arrays = row_to_arrays_fn(rows)
    table = pa.table(arrays, schema=schema)
    pq.write_table(table, str(path), compression="zstd")


def _option_row_to_arrays(rows: List[Dict]) -> Dict[str, list]:
    return {
        "ts":          [r["ts"]         for r in rows],
        "code":        [r["code"]       for r in rows],
        "underlying":  [r["underlying"] for r in rows],
        "last":        [_nan_to_none(r.get("last"))  for r in rows],
        "ask1":        [_nan_to_none(r.get("ask1"))  for r in rows],
        "bid1":        [_nan_to_none(r.get("bid1"))  for r in rows],
        "askv1":       [_int_or_zero(r.get("askv1", 0))  for r in rows],
        "bidv1":       [_int_or_zero(r.get("bidv1", 0))  for r in rows],
        "oi":          [r.get("oi",  0) for r in rows],
        "vol":         [r.get("vol", 0) for r in rows],
        "high":        [_nan_to_none(r.get("high")) for r in rows],
        "low":         [_nan_to_none(r.get("low"))  for r in rows],
        "is_adjusted": [r.get("is_adjusted", False) for r in rows],
        "multiplier":  [r.get("multiplier", 10000)  for r in rows],
    }


def _etf_row_to_arrays(rows: List[Dict]) -> Dict[str, list]:
    return {
        "ts":   [r["ts"]   for r in rows],
        "code": [r["code"] for r in rows],
        "last": [_nan_to_none(r.get("last")) for r in rows],
        "ask1": [_nan_to_none(r.get("ask1")) for r in rows],
        "bid1": [_nan_to_none(r.get("bid1")) for r in rows],
        "askv1": [_int_or_zero(r.get("askv1", 0)) for r in rows],
        "bidv1": [_int_or_zero(r.get("bidv1", 0)) for r in rows],
    }


def _snapshot_row_to_arrays(rows: List[Dict]) -> Dict[str, list]:
    return {
        "type":        [r.get("type",       "option") for r in rows],
        "code":        [r["code"]                      for r in rows],
        "underlying":  [r.get("underlying", r["code"]) for r in rows],
        "ts":          [r["ts"]                        for r in rows],
        "last":        [_nan_to_none(r.get("last"))    for r in rows],
        "ask1":        [_nan_to_none(r.get("ask1"))    for r in rows],
        "bid1":        [_nan_to_none(r.get("bid1"))    for r in rows],
        "askv1":       [_int_or_zero(r.get("askv1", 0)) for r in rows],
        "bidv1":       [_int_or_zero(r.get("bidv1", 0)) for r in rows],
        "oi":          [r.get("oi",  0)                for r in rows],
        "vol":         [r.get("vol", 0)                for r in rows],
        "high":        [_nan_to_none(r.get("high"))    for r in rows],
        "low":         [_nan_to_none(r.get("low"))     for r in rows],
        "is_adjusted": [r.get("is_adjusted", False)    for r in rows],
        "multiplier":  [r.get("multiplier", 0)         for r in rows],
    }
