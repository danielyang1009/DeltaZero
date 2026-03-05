# -*- coding: utf-8 -*-
"""
多频率 Bar 数据加载器

将 K 线（1m / 5m / 日线等）加载为 ETFTickData 列表，
与 tick 级期权数据一起供 BacktestEngine 混合频率回测。

支持格式：
  - CSV: 需包含 datetime, open, high, low, close, volume 列
  - Parquet: 同上

典型用法：
    loader = BarDataLoader()
    etf_ticks = loader.load_csv("data/510050_1m.csv", etf_code="510050.SH")
    # 直接传入 BacktestEngine.run(..., etf_ticks=etf_ticks)
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from models import ETFTickData

logger = logging.getLogger(__name__)

# 从文件名推断 ETF 代码的正则（如 510050_1m.csv → 510050）
_CODE_RE = re.compile(r"(5\d{5})")


def _infer_etf_code(filepath: Path) -> Optional[str]:
    """从文件名推断 ETF 代码（如 510050.SH）"""
    m = _CODE_RE.search(filepath.stem)
    return f"{m.group(1)}.SH" if m else None


# 常见列名映射 → 标准列名
_COL_ALIASES = {
    "datetime": "datetime",
    "date_time": "datetime",
    "time": "datetime",
    "timestamp": "datetime",
    "trade_time": "datetime",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "last": "close",
    "volume": "volume",
    "vol": "volume",
    "amount": "amount",
    "turnover": "amount",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """将常见列名变体映射为标准名称"""
    rename_map = {}
    lower_cols = {c.lower().strip(): c for c in df.columns}
    for alias, standard in _COL_ALIASES.items():
        if alias in lower_cols and standard not in rename_map.values():
            rename_map[lower_cols[alias]] = standard
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


class BarDataLoader:
    """
    K 线数据加载器，将 OHLC 数据转换为 ETFTickData 序列。

    每根 K 线可展开为 1 条或 4 条 ETFTickData：
      - mode="close": 仅生成 close 价格的 tick（默认，最快）
      - mode="ohlc":  生成 open→high→low→close 四个事件（更精细的路径模拟）

    Attributes:
        mode: 展开模式
        spread_bps: 模拟买卖价差（基点），用于生成 ask/bid
    """

    def __init__(
        self,
        mode: str = "close",
        spread_bps: float = 5.0,
    ) -> None:
        """
        Args:
            mode: "close" 或 "ohlc"
            spread_bps: 模拟价差 basis points（5 bps = 万分之五）
        """
        assert mode in ("close", "ohlc"), f"mode must be 'close' or 'ohlc', got {mode!r}"
        self.mode = mode
        self.spread_bps = spread_bps

    def load_csv(
        self,
        filepath: str | Path,
        etf_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[ETFTickData]:
        """
        从 CSV 加载 K 线并转换为 ETFTickData 列表。

        Args:
            filepath: CSV 文件路径
            etf_code: ETF 代码（如 510050.SH），不传则从文件名推断
            start_date: 起始日期 YYYY-MM-DD（含），可选
            end_date: 结束日期 YYYY-MM-DD（含），可选

        Returns:
            按时间排序的 ETFTickData 列表
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        df = pd.read_csv(path)
        return self._convert(df, path, etf_code, start_date, end_date)

    def load_parquet(
        self,
        filepath: str | Path,
        etf_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[ETFTickData]:
        """从 Parquet 加载 K 线并转换为 ETFTickData 列表。"""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")

        df = pd.read_parquet(path)
        return self._convert(df, path, etf_code, start_date, end_date)

    def load_directory(
        self,
        directory: str | Path,
        etf_code: Optional[str] = None,
        pattern: str = "*.csv",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, List[ETFTickData]]:
        """
        从目录批量加载 K 线文件，按 ETF 代码分组返回。

        Args:
            directory: 目录路径
            etf_code: 指定 ETF 代码（不传则从文件名推断）
            pattern: 文件匹配模式
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            {etf_code: List[ETFTickData]}
        """
        dirpath = Path(directory)
        if not dirpath.is_dir():
            raise FileNotFoundError(f"目录不存在: {dirpath}")

        result: Dict[str, List[ETFTickData]] = {}
        files = sorted(dirpath.glob(pattern))

        for f in files:
            code = etf_code or _infer_etf_code(f)
            if code is None:
                logger.warning("无法推断 ETF 代码，跳过: %s", f.name)
                continue

            try:
                if f.suffix.lower() == ".parquet":
                    ticks = self.load_parquet(f, code, start_date, end_date)
                else:
                    ticks = self.load_csv(f, code, start_date, end_date)

                if ticks:
                    result.setdefault(code, []).extend(ticks)
                    logger.info("  %s (%s): %d 条 ETF 事件", f.name, code, len(ticks))
            except Exception as e:
                logger.warning("加载失败 %s: %s", f.name, e)

        for code in result:
            result[code].sort(key=lambda t: t.timestamp)

        return result

    def _convert(
        self,
        df: pd.DataFrame,
        source_path: Path,
        etf_code: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[ETFTickData]:
        """核心转换逻辑：DataFrame → List[ETFTickData]"""
        code = etf_code or _infer_etf_code(source_path)
        if code is None:
            raise ValueError(f"无法确定 ETF 代码，请通过 etf_code 参数指定: {source_path}")

        df = _normalize_columns(df)

        if "datetime" not in df.columns:
            raise ValueError(
                f"CSV 缺少时间列，需要 datetime/timestamp/time 列之一。"
                f"当前列: {list(df.columns)}"
            )

        for col in ("close",):
            if col not in df.columns:
                raise ValueError(f"CSV 缺少必需列 '{col}'。当前列: {list(df.columns)}")

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)

        if start_date:
            df = df[df["datetime"] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df["datetime"] <= pd.Timestamp(end_date) + pd.Timedelta(days=1)]

        if df.empty:
            logger.warning("日期过滤后无数据: %s", source_path.name)
            return []

        half_spread = self.spread_bps / 10000.0 / 2.0

        ticks: List[ETFTickData] = []

        if self.mode == "ohlc":
            has_ohlc = all(c in df.columns for c in ("open", "high", "low"))
            if not has_ohlc:
                logger.warning("OHLC 模式但缺少 open/high/low 列，回退到 close 模式")
            else:
                for _, row in df.iterrows():
                    ts = row["datetime"].to_pydatetime()
                    for price_col in ("open", "high", "low", "close"):
                        px = float(row[price_col])
                        if px <= 0 or math.isnan(px):
                            continue
                        ticks.append(ETFTickData(
                            timestamp=ts,
                            etf_code=code,
                            price=px,
                            volume=int(row.get("volume", 0) or 0) if price_col == "close" else 0,
                            ask_price=round(px * (1 + half_spread), 4),
                            bid_price=round(px * (1 - half_spread), 4),
                            is_simulated=False,
                        ))
                if ticks:
                    logger.info(
                        "加载 %s: %d 根 K 线 → %d 条 OHLC 事件 (%s)",
                        source_path.name, len(df), len(ticks), code,
                    )
                    return ticks

        for _, row in df.iterrows():
            px = float(row["close"])
            if px <= 0 or math.isnan(px):
                continue
            ts = row["datetime"].to_pydatetime()
            ticks.append(ETFTickData(
                timestamp=ts,
                etf_code=code,
                price=px,
                volume=int(row.get("volume", 0) or 0),
                ask_price=round(px * (1 + half_spread), 4),
                bid_price=round(px * (1 - half_spread), 4),
                is_simulated=False,
            ))

        logger.info(
            "加载 %s: %d 根 K 线 → %d 条 close 事件 (%s)",
            source_path.name, len(df), len(ticks), code,
        )
        return ticks
