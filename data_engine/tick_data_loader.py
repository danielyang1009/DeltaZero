"""
本地 Tick 数据加载器

负责从 CSV 文件加载期权 Tick 级行情数据，处理以下关键问题：
1. 异构 Schema 统一：50ETF（29列/5档盘口）与 300ETF/500ETF（12列/1档盘口）
2. 时间戳解析：兼容 17 位精确整型和科学计数法两种格式（向量化批处理）
3. 合约代码标准化：.XSHG -> .SH
4. 日期范围过滤：按文件名中的 YYYY-MM 过滤，避免加载全量数据

性能设计：
- 使用向量化 numpy/pandas 操作替代逐行 iterrows()，速度提升 10-30x
- 批量解析时间戳，避免每行独立调用 Python 函数
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from models import ETFTickData, OptionTickData, normalize_code

logger = logging.getLogger(__name__)


# 盘口列名映射
_ASK_PRICE_COLS = ["a1_p", "a2_p", "a3_p", "a4_p", "a5_p"]
_ASK_VOL_COLS   = ["a1_v", "a2_v", "a3_v", "a4_v", "a5_v"]
_BID_PRICE_COLS = ["b1_p", "b2_p", "b3_p", "b4_p", "b5_p"]
_BID_VOL_COLS   = ["b1_v", "b2_v", "b3_v", "b4_v", "b5_v"]

# 从文件名中提取 YYYY-MM 的正则
_FILE_DATE_RE = re.compile(r"(\d{4}-\d{2})\.csv$", re.IGNORECASE)


class TickLoader:
    """
    本地 Tick CSV 数据加载器

    支持加载单个 CSV 文件或整个目录，自动处理 Schema 差异和时间戳格式。
    使用向量化批处理，性能比逐行迭代快 10-30 倍。

    Attributes:
        code_suffix: 标准化后的代码后缀，默认 '.SH'
    """

    def __init__(self, code_suffix: str = ".SH") -> None:
        """
        初始化加载器

        Args:
            code_suffix: 目标代码后缀，用于统一不同数据源的合约代码
        """
        self._code_suffix = code_suffix

    def load_csv(self, filepath: str | Path) -> List[OptionTickData]:
        """
        加载单个 CSV 文件并转换为 TickData 列表（向量化实现）

        Args:
            filepath: CSV 文件路径

        Returns:
            按时间排序的 TickData 列表

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 文件格式无法识别
        """
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Tick 数据文件不存在: {filepath}")

        logger.debug("加载 Tick 数据: %s", filepath.name)

        df = pd.read_csv(
            filepath,
            encoding="utf-8",
            dtype={"time": str, "contract_code": str},
            low_memory=False,
        )

        if df.empty:
            return []

        schema_type = self._detect_schema(df.columns.tolist())

        # === 向量化时间戳解析 ===
        timestamps = self._parse_timestamps_batch(df["time"])

        # === 向量化合约代码标准化 ===
        suffix = self._code_suffix
        codes: List[str] = [normalize_code(c.strip(), suffix) for c in df["contract_code"].tolist()]

        # === 向量化数值列提取（numpy array，索引比 Series 快 10x）===
        current_arr  = df["current"].to_numpy(dtype=float, na_value=math.nan)
        volume_arr   = df["volume"].to_numpy(dtype=np.int64, na_value=0)
        high_arr     = df["high"].to_numpy(dtype=float, na_value=math.nan)
        low_arr      = df["low"].to_numpy(dtype=float, na_value=math.nan)
        money_arr    = df["money"].to_numpy(dtype=float, na_value=math.nan)
        position_arr = df["position"].to_numpy(dtype=np.int64, na_value=0)

        # === 向量化盘口列提取 ===
        depth = 5 if schema_type == "depth5" else 1
        nan_col = np.full(len(df), math.nan)
        zero_col = np.zeros(len(df), dtype=np.int64)

        ap_arrs = [df[c].to_numpy(dtype=float, na_value=math.nan) if c in df.columns else nan_col
                   for c in _ASK_PRICE_COLS[:depth]]
        ap_arrs += [nan_col] * (5 - depth)

        av_arrs = [df[c].to_numpy(dtype=np.int64, na_value=0) if c in df.columns else zero_col
                   for c in _ASK_VOL_COLS[:depth]]
        av_arrs += [zero_col] * (5 - depth)

        bp_arrs = [df[c].to_numpy(dtype=float, na_value=math.nan) if c in df.columns else nan_col
                   for c in _BID_PRICE_COLS[:depth]]
        bp_arrs += [nan_col] * (5 - depth)

        bv_arrs = [df[c].to_numpy(dtype=np.int64, na_value=0) if c in df.columns else zero_col
                   for c in _BID_VOL_COLS[:depth]]
        bv_arrs += [zero_col] * (5 - depth)

        # === 构建 TickData 列表（通过数组索引，避免 iterrows 开销）===
        n = len(df)
        ticks: List[OptionTickData] = []
        for i in range(n):
            ticks.append(OptionTickData(
                timestamp=timestamps[i],
                contract_code=codes[i],
                current=float(current_arr[i]),
                volume=int(volume_arr[i]),
                high=float(high_arr[i]),
                low=float(low_arr[i]),
                money=float(money_arr[i]),
                position=int(position_arr[i]),
                ask_prices=[float(ap_arrs[j][i]) for j in range(5)],
                ask_volumes=[int(av_arrs[j][i]) for j in range(5)],
                bid_prices=[float(bp_arrs[j][i]) for j in range(5)],
                bid_volumes=[int(bv_arrs[j][i]) for j in range(5)],
            ))

        ticks.sort(key=lambda t: t.timestamp)
        logger.debug("  加载完毕: %d 条", len(ticks))
        return ticks

    def load_directory(
        self,
        dirpath: str | Path,
        start_month: Optional[str] = None,
        end_month: Optional[str] = None,
    ) -> Dict[str, List[OptionTickData]]:
        """
        递归加载目录下所有 CSV 文件，支持按月份范围过滤

        文件名需包含 YYYY-MM 格式日期（如 50ETF期权_option_ticks_2024-01.csv）
        才能被日期过滤器识别；不匹配命名规则的文件始终被加载。

        Args:
            dirpath: 目录路径（支持多层子目录）
            start_month: 起始月份，格式 'YYYY-MM'，如 '2024-01'（含）
            end_month: 结束月份，格式 'YYYY-MM'，如 '2024-06'（含）

        Returns:
            合约代码 -> TickData 列表的字典，每个合约的数据按时间排序
        """
        dirpath = Path(dirpath)
        if not dirpath.is_dir():
            raise NotADirectoryError(f"目录不存在: {dirpath}")

        all_csv = sorted(dirpath.rglob("*.csv"))
        csv_files = self._filter_by_date(all_csv, start_month, end_month)

        logger.info(
            "共找到 %d 个 CSV 文件，过滤后加载 %d 个（范围: %s ~ %s）",
            len(all_csv), len(csv_files),
            start_month or "最早", end_month or "最新",
        )

        result: Dict[str, List[OptionTickData]] = {}

        for idx, csv_file in enumerate(csv_files, 1):
            logger.info("[%d/%d] 加载: %s", idx, len(csv_files), csv_file.name)
            try:
                ticks = self.load_csv(csv_file)
                for tick in ticks:
                    if tick.contract_code not in result:
                        result[tick.contract_code] = []
                    result[tick.contract_code].append(tick)
            except Exception as e:
                logger.error("加载失败 %s: %s", csv_file.name, e)
                continue

        for code in result:
            result[code].sort(key=lambda t: t.timestamp)

        total_ticks = sum(len(v) for v in result.values())
        logger.info("共加载 %d 个合约、%d 条 Tick", len(result), total_ticks)
        return result

    # ============================================================
    # Parquet 加载方法
    # ============================================================

    def load_option_parquet(self, filepath: str | Path) -> List[OptionTickData]:
        """
        加载 DataBus 落盘的期权 Parquet 文件并转换为 OptionTickData 列表。

        列映射: ts(int64 ms)→timestamp, code→contract_code, last→current,
        ask1/bid1→盘口一档, askv1/bidv1→一档量, oi→position, vol→volume,
        high/low→高低价, 2-5 档填 nan/0, money=0.0

        自动过滤 9:30 前的集合竞价数据。
        """
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Parquet 文件不存在: {filepath}")

        logger.debug("加载期权 Parquet: %s", filepath.name)

        df = pd.read_parquet(filepath, engine="pyarrow")
        if df.empty:
            return []

        # --- 过滤集合竞价数据 (9:30 前) ---
        ts_arr = df["ts"].to_numpy(dtype=np.int64)
        df = self._filter_pre_open(df, ts_arr)
        if df.empty:
            return []

        # 重新取过滤后的数组
        ts_arr = df["ts"].to_numpy(dtype=np.int64)
        n = len(df)

        # 向量化时间戳转换: int64 ms → datetime (本地时区)
        timestamps = pd.to_datetime(ts_arr, unit="ms", utc=True).tz_convert("Asia/Shanghai").tz_localize(None).to_pydatetime().tolist()

        codes = df["code"].tolist()
        current_arr = df["last"].to_numpy(dtype=float)
        ask1_arr = df["ask1"].to_numpy(dtype=float)
        bid1_arr = df["bid1"].to_numpy(dtype=float)
        askv1_arr = df["askv1"].to_numpy(dtype=np.int64)
        bidv1_arr = df["bidv1"].to_numpy(dtype=np.int64)
        oi_arr = df["oi"].to_numpy(dtype=np.int64) if "oi" in df.columns else np.zeros(n, dtype=np.int64)
        vol_arr = df["vol"].to_numpy(dtype=np.int64) if "vol" in df.columns else np.zeros(n, dtype=np.int64)
        high_arr = df["high"].to_numpy(dtype=float) if "high" in df.columns else np.full(n, math.nan)
        low_arr = df["low"].to_numpy(dtype=float) if "low" in df.columns else np.full(n, math.nan)

        nan5 = [math.nan] * 4
        zero4 = [0] * 4

        ticks: List[OptionTickData] = []
        for i in range(n):
            ticks.append(OptionTickData(
                timestamp=timestamps[i],
                contract_code=codes[i],
                current=float(current_arr[i]),
                volume=int(vol_arr[i]),
                high=float(high_arr[i]),
                low=float(low_arr[i]),
                money=0.0,
                position=int(oi_arr[i]),
                ask_prices=[float(ask1_arr[i])] + nan5,
                ask_volumes=[int(askv1_arr[i])] + zero4,
                bid_prices=[float(bid1_arr[i])] + nan5,
                bid_volumes=[int(bidv1_arr[i])] + zero4,
            ))

        ticks.sort(key=lambda t: t.timestamp)
        logger.debug("  期权 Parquet 加载完毕: %d 条 (过滤集合竞价后)", len(ticks))
        return ticks

    def load_etf_parquet(self, filepath: str | Path) -> List[ETFTickData]:
        """
        加载 DataBus 落盘的 ETF Parquet 文件并转换为 ETFTickData 列表。

        列映射: ts→timestamp, code→etf_code, last→price,
        ask1/bid1→ask_price/bid_price, askv1/bidv1→ask_volume/bid_volume

        自动过滤 9:30 前的集合竞价数据。
        """
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Parquet 文件不存在: {filepath}")

        logger.debug("加载 ETF Parquet: %s", filepath.name)

        df = pd.read_parquet(filepath, engine="pyarrow")
        if df.empty:
            return []

        # --- 过滤集合竞价数据 (9:30 前) ---
        ts_arr = df["ts"].to_numpy(dtype=np.int64)
        df = self._filter_pre_open(df, ts_arr)
        if df.empty:
            return []

        ts_arr = df["ts"].to_numpy(dtype=np.int64)
        n = len(df)

        timestamps = pd.to_datetime(ts_arr, unit="ms", utc=True).tz_convert("Asia/Shanghai").tz_localize(None).to_pydatetime().tolist()

        codes = df["code"].tolist()
        price_arr = df["last"].to_numpy(dtype=float)
        ask1_arr = df["ask1"].to_numpy(dtype=float)
        bid1_arr = df["bid1"].to_numpy(dtype=float)
        askv1_arr = df["askv1"].to_numpy(dtype=np.int64) if "askv1" in df.columns else np.zeros(n, dtype=np.int64)
        bidv1_arr = df["bidv1"].to_numpy(dtype=np.int64) if "bidv1" in df.columns else np.zeros(n, dtype=np.int64)

        ticks: List[ETFTickData] = []
        for i in range(n):
            ticks.append(ETFTickData(
                timestamp=timestamps[i],
                etf_code=codes[i],
                price=float(price_arr[i]),
                ask_price=float(ask1_arr[i]),
                bid_price=float(bid1_arr[i]),
                ask_volume=int(askv1_arr[i]),
                bid_volume=int(bidv1_arr[i]),
                is_simulated=False,
            ))

        ticks.sort(key=lambda t: t.timestamp)
        logger.debug("  ETF Parquet 加载完毕: %d 条 (过滤集合竞价后)", len(ticks))
        return ticks

    def load_market_data_dir(
        self,
        root: str | Path,
        underlyings: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Tuple[Dict[str, List[OptionTickData]], List[ETFTickData], List[date]]:
        """
        从 MARKET_DATA 目录加载多品种多日 Parquet 数据。

        目录结构: {root}/{underlying_去掉.SH}/options_YYYYMMDD.parquet
                  {root}/{underlying_去掉.SH}/etf_YYYYMMDD.parquet

        Args:
            root: 数据根目录 (如 D:\\MARKET_DATA)
            underlyings: 品种列表 (如 ["510050.SH", "510300.SH"])
            start_date: 起始日期 YYYYMMDD (含)
            end_date: 结束日期 YYYYMMDD (含)

        Returns:
            (option_ticks_by_code, all_etf_ticks, sorted_trading_dates)
        """
        root = Path(root)
        option_ticks: Dict[str, List[OptionTickData]] = {}
        all_etf_ticks: List[ETFTickData] = []
        date_set: set[date] = set()
        _date_re = re.compile(r"(\d{8})")

        for underlying in underlyings:
            ul_dir = root / underlying.replace(".SH", "")
            if not ul_dir.is_dir():
                logger.warning("品种目录不存在: %s", ul_dir)
                continue

            opt_files = sorted(ul_dir.glob("options_*.parquet"))
            etf_files = sorted(ul_dir.glob("etf_*.parquet"))

            for fpath in opt_files:
                m = _date_re.search(fpath.stem)
                if not m:
                    continue
                fdate = m.group(1)
                if start_date and fdate < start_date:
                    continue
                if end_date and fdate > end_date:
                    continue

                trade_date = date(int(fdate[:4]), int(fdate[4:6]), int(fdate[6:8]))
                date_set.add(trade_date)

                logger.info("加载期权: %s", fpath.name)
                ticks = self.load_option_parquet(fpath)
                for tick in ticks:
                    option_ticks.setdefault(tick.contract_code, []).append(tick)

            for fpath in etf_files:
                m = _date_re.search(fpath.stem)
                if not m:
                    continue
                fdate = m.group(1)
                if start_date and fdate < start_date:
                    continue
                if end_date and fdate > end_date:
                    continue

                logger.info("加载 ETF: %s", fpath.name)
                ticks = self.load_etf_parquet(fpath)
                all_etf_ticks.extend(ticks)

        # 按时间排序
        for code in option_ticks:
            option_ticks[code].sort(key=lambda t: t.timestamp)
        all_etf_ticks.sort(key=lambda t: t.timestamp)

        sorted_dates = sorted(date_set)
        total_opt = sum(len(v) for v in option_ticks.values())
        logger.info(
            "Parquet 加载完毕: %d 个品种, %d 个交易日, %d 个期权合约 (%d 条), %d 条 ETF",
            len(underlyings), len(sorted_dates), len(option_ticks), total_opt, len(all_etf_ticks),
        )
        return option_ticks, all_etf_ticks, sorted_dates

    @staticmethod
    def _filter_pre_open(df: pd.DataFrame, ts_arr: np.ndarray) -> pd.DataFrame:
        """过滤 9:30 前的集合竞价数据（基于 Unix ms 时间戳整数比较）。"""
        if len(ts_arr) == 0:
            return df
        # 从第一个 ts 推算当日日期 (CST = UTC+8)
        sample_dt = datetime.fromtimestamp(int(ts_arr[0]) / 1000)  # 本地时区（CST）
        # 当日 9:30:00 CST 的 Unix 时间戳
        open_dt = datetime(sample_dt.year, sample_dt.month, sample_dt.day, 9, 30, 0)
        open_ms = int(open_dt.timestamp() * 1000)
        mask = ts_arr >= open_ms
        return df[mask].copy()

    # ============================================================
    # 内部工具方法（CSV）
    # ============================================================

    @staticmethod
    def _filter_by_date(
        csv_files: List[Path],
        start_month: Optional[str],
        end_month: Optional[str],
    ) -> List[Path]:
        """
        根据文件名中的 YYYY-MM 过滤 CSV 文件列表

        Args:
            csv_files: 待过滤的文件列表
            start_month: 起始月份字符串（含），如 '2024-01'
            end_month: 结束月份字符串（含），如 '2024-06'

        Returns:
            过滤后的文件列表
        """
        if not start_month and not end_month:
            return csv_files

        filtered: List[Path] = []
        for f in csv_files:
            m = _FILE_DATE_RE.search(f.name)
            if m is None:
                filtered.append(f)
                continue
            file_ym = m.group(1)
            if start_month and file_ym < start_month:
                continue
            if end_month and file_ym > end_month:
                continue
            filtered.append(f)
        return filtered

    @staticmethod
    def _detect_schema(columns: List[str]) -> str:
        """检测 CSV 盘口深度：depth5 或 depth1"""
        if "a2_p" in columns:
            return "depth5"
        elif "a1_p" in columns:
            return "depth1"
        else:
            raise ValueError(f"无法识别的列结构: {columns}")

    @staticmethod
    def _parse_timestamps_batch(time_series: pd.Series) -> List[datetime]:
        """
        向量化批量解析时间戳列

        自动检测格式（精确整型 or 科学计数法），对整型使用全向量化
        numpy 整数运算，对科学计数法使用 Decimal 列表推导（保留精度）。

        Args:
            time_series: CSV 中的 time 列（字符串）

        Returns:
            datetime 对象列表（与输入等长）
        """
        raw = time_series.str.strip()
        sample = next((s for s in raw if s and s.upper() != "NAN"), "0")

        if "E" in sample.upper():
            # 科学计数法 → 用 Decimal 列表推导保留精度
            try:
                numerics = np.array(
                    [int(Decimal(s)) for s in raw], dtype=np.int64
                )
            except (InvalidOperation, ValueError):
                numerics = pd.to_numeric(raw, errors="coerce").fillna(0).astype(np.int64)
        else:
            # 直接整型字符串 → 纯向量化
            numerics = pd.to_numeric(raw, errors="coerce").fillna(0).astype(np.int64)

        return TickLoader._int_array_to_datetimes(numerics)

    @staticmethod
    def _int_array_to_datetimes(numerics: np.ndarray) -> List[datetime]:
        """
        将 YYYYMMDDHHMMSSmmm 格式的整型数组向量化转换为 datetime 列表

        使用 numpy 整数运算并通过 pd.to_datetime 批量构建，
        比逐元素 Python datetime() 构造快约 20 倍。
        """
        # 检测时间戳位数（取非零样本）
        nonzero = numerics[numerics > 0]
        if len(nonzero) == 0:
            return [datetime(1970, 1, 1)] * len(numerics)

        n_digits = len(str(int(nonzero[0])))

        if n_digits >= 17:
            # YYYYMMDDHHMMSSmmm
            years   = (numerics // 10_000_000_000_000).astype(np.int32)
            months  = ((numerics // 100_000_000_000) % 100).clip(1, 12).astype(np.int32)
            days    = ((numerics // 1_000_000_000) % 100).clip(1, 31).astype(np.int32)
            hours   = ((numerics // 10_000_000) % 100).astype(np.int32)
            minutes = ((numerics // 100_000) % 100).astype(np.int32)
            seconds = ((numerics // 1_000) % 100).astype(np.int32)
            micros  = (numerics % 1000 * 1000).astype(np.int32)
        elif n_digits >= 14:
            # YYYYMMDDHHMMSS
            years   = (numerics // 10_000_000_000).astype(np.int32)
            months  = ((numerics // 100_000_000) % 100).clip(1, 12).astype(np.int32)
            days    = ((numerics // 1_000_000) % 100).clip(1, 31).astype(np.int32)
            hours   = ((numerics // 10_000) % 100).astype(np.int32)
            minutes = ((numerics // 100) % 100).astype(np.int32)
            seconds = (numerics % 100).astype(np.int32)
            micros  = np.zeros(len(numerics), dtype=np.int32)
        else:
            # YYYYMMDD 降级（科学计数法精度丢失）
            years   = (numerics // 10_000).astype(np.int32)
            months  = ((numerics // 100) % 100).clip(1, 12).astype(np.int32)
            days    = (numerics % 100).clip(1, 28).astype(np.int32)
            hours = minutes = seconds = np.zeros(len(numerics), dtype=np.int32)
            micros  = np.zeros(len(numerics), dtype=np.int32)

        # pd.to_datetime 批量构建（比逐个 datetime() 快约 20 倍）
        df_dt = pd.DataFrame({
            "year": years, "month": months, "day": days,
            "hour": hours, "minute": minutes, "second": seconds,
            "microsecond": micros,
        })
        return pd.to_datetime(df_dt).tolist()
