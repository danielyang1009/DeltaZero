"""
合约信息管理器

从当日 optionchain CSV（fetch_optionchain 产出）加载合约信息，含乘数。
处理代码后缀标准化（.SH / .XSHG 互转）并提供 Call/Put 配对查询。
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import (
    ContractInfo,
    OptionType,
    UNDERLYING_MAP,
    normalize_code,
)

logger = logging.getLogger(__name__)

_ADJUSTED_TAIL_RE = re.compile(r"[A-Z]$")


def get_optionchain_path(target_date: date | None = None, metadata_dir: Path | None = None) -> Path:
    """
    获取 optionchain 文件路径（CSV 或 xlsx）。
    优先级：当日 CSV > 最新 CSV > metadata 下任意 xlsx > 当日 CSV 占位路径（不存在）。
    """
    base = metadata_dir or (Path(__file__).resolve().parent.parent / "metadata")
    if target_date is None:
        target_date = date.today()
    p = base / f"{target_date:%Y-%m-%d}_optionchain.csv"
    if p.exists():
        return p
    csv_candidates = sorted(base.glob("*_optionchain.csv"), reverse=True)
    if csv_candidates:
        return csv_candidates[0]
    # 优先找名字含 optionchain 的 xlsx，再退而求其次找任意 xlsx
    xlsx_candidates = sorted(base.glob("*optionchain*.xlsx"), reverse=True)
    if not xlsx_candidates:
        xlsx_candidates = sorted(base.glob("*.xlsx"), reverse=True)
    if xlsx_candidates:
        return xlsx_candidates[0]
    return p


class ContractInfoManager:
    """
    期权合约基本信息管理器

    加载并维护合约代码到 ContractInfo 的映射表，
    提供高效的合约查询和 Call/Put 配对功能。

    Attributes:
        contracts: 合约代码 -> ContractInfo 的字典（代码统一 .SH 后缀）
    """

    def __init__(self) -> None:
        self.contracts: Dict[str, ContractInfo] = {}
        self._pairs_cache: Dict[str, List[Tuple[ContractInfo, ContractInfo]]] = {}

    # Wind xlsx 按列位置到标准字段名的映射（顺序固定）
    _XLSX_COL_NAMES = [
        "option_code",      # 0 证券代码
        "option_name",      # 1 证券简称
        "us_code",          # 2 标的Wind代码
        "call_put",         # 3 期权类型（认购/认沽）
        "strike_price",     # 4 行权价格
        "first_tradedate",  # 5 起始交易日期
        "last_tradedate",   # 6 最后交易日期
        "month",            # 7 交割月份
        "multiplier",       # 8 合约乘数
    ]

    def load_from_optionchain(
        self,
        csv_path: str | Path,
        target_date: Optional[date] = None,
        max_age_days: int = 7,
    ) -> int:
        """
        从 optionchain 文件加载合约信息，含乘数。支持 CSV 和 Wind 导出的 xlsx。

        CSV 需含：option_code, option_name, us_code, strike_price, month, call_put,
        first_tradedate, last_tradedate, multiplier。

        xlsx 按列位置解析（Wind 导出格式，列名中文，无需列名匹配）。

        Args:
            csv_path: 文件路径（.csv 或 .xlsx）
            target_date: 目标日期，用于校验文件是否过期（不传则用今日）
            max_age_days: 文件日期与目标日期允许的最大偏差（天），超限则告警

        Returns:
            成功加载的合约数量

        Raises:
            FileNotFoundError: 文件不存在
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"optionchain 文件不存在: {csv_path}")

        # 版本校验：从文件名提取日期，防止使用过期乘数（xlsx 无日期前缀则跳过）
        try:
            stem = csv_path.stem  # e.g. 2026-03-04_optionchain
            file_date_str = stem.split("_")[0]
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d").date()
            check_date = target_date or date.today()
            age_days = abs((file_date - check_date).days)
            if age_days > max_age_days:
                logger.warning(
                    "optionchain 文件可能过期: %s (文件日期 %s, 目标 %s, 相差 %d 天)，"
                    "乘数可能不准确，请开盘前执行 fetch_optionchain",
                    csv_path.name, file_date, check_date, age_days,
                )
        except (ValueError, IndexError):
            pass

        logger.info("加载合约信息: %s", csv_path)

        if csv_path.suffix.lower() == ".xlsx":
            rows = self._read_xlsx_as_dicts(csv_path)
        else:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))

        count = 0
        for row in rows:
            try:
                info = self._parse_optionchain_row(row)
                if info is not None:
                    self.contracts[info.contract_code] = info
                    count += 1
            except Exception as e:
                logger.warning("解析 optionchain 行失败（跳过）: %s | 行: %s", e, row)

        self._pairs_cache.clear()
        logger.info("成功加载 %d 条合约信息", count)
        return count

    def _read_xlsx_as_dicts(self, path: Path) -> list:
        """将 Wind 导出 xlsx 按列位置读取，转换为标准字段名的 dict 列表。"""
        try:
            import pandas as pd
        except ImportError as e:
            raise RuntimeError("读取 xlsx 需要 pandas：pip install pandas openpyxl") from e

        df = pd.read_excel(path, header=0, dtype=str)
        # 只取前 9 列（忽略多余列）
        n_cols = min(len(df.columns), len(self._XLSX_COL_NAMES))
        df = df.iloc[:, :n_cols].copy()
        df.columns = self._XLSX_COL_NAMES[:n_cols]
        # 去掉全空行及非合约行（合约代码首字符必须为数字，过滤 Wind 水印行等）
        df = df.dropna(how="all")
        df = df[df["option_code"].str.match(r"^\d", na=False)]
        return df.to_dict(orient="records")

    def get_info(self, contract_code: str) -> Optional[ContractInfo]:
        """
        查询合约信息

        Args:
            contract_code: 合约代码（支持 .SH 和 .XSHG 后缀，自动标准化）

        Returns:
            ContractInfo 或 None（未找到）
        """
        normalized = normalize_code(contract_code, ".SH")
        return self.contracts.get(normalized)

    def find_call_put_pairs(
        self,
        underlying: str,
        expiry: Optional[date] = None,
        delivery_month: Optional[str] = None,
    ) -> List[Tuple[ContractInfo, ContractInfo]]:
        """
        查找同行权价的 Call/Put 配对

        按标的代码和到期日/交割月份筛选，返回所有匹配的 (Call, Put) 元组。

        Args:
            underlying: 标的 ETF 代码（如 '510050.SH'）或简称（如 '50ETF'）
            expiry: 按到期日精确筛选（可选）
            delivery_month: 按交割月份筛选，如 '201503'（可选）

        Returns:
            (Call ContractInfo, Put ContractInfo) 元组列表，按行权价排序
        """
        underlying_code = self._resolve_underlying(underlying)

        cache_key = f"{underlying_code}|{expiry}|{delivery_month}"
        if cache_key in self._pairs_cache:
            return self._pairs_cache[cache_key]

        calls: Dict[float, ContractInfo] = {}
        puts: Dict[float, ContractInfo] = {}

        for info in self.contracts.values():
            if info.underlying_code != underlying_code:
                continue
            if expiry is not None and info.expiry_date != expiry:
                continue
            if delivery_month is not None and info.delivery_month != delivery_month:
                continue

            if info.option_type == OptionType.CALL:
                calls[info.strike_price] = info
            else:
                puts[info.strike_price] = info

        pairs: List[Tuple[ContractInfo, ContractInfo]] = []
        common_strikes = sorted(set(calls.keys()) & set(puts.keys()))
        for strike in common_strikes:
            pairs.append((calls[strike], puts[strike]))

        self._pairs_cache[cache_key] = pairs
        return pairs

    def get_available_expiries(self, underlying: str) -> List[date]:
        """
        获取指定标的的所有可用到期日

        Args:
            underlying: 标的代码或简称

        Returns:
            按日期排序的到期日列表
        """
        underlying_code = self._resolve_underlying(underlying)
        expiries = set()
        for info in self.contracts.values():
            if info.underlying_code == underlying_code:
                expiries.add(info.expiry_date)
        return sorted(expiries)

    def get_contracts_by_underlying(self, underlying: str) -> List[ContractInfo]:
        """
        获取指定标的的所有合约

        Args:
            underlying: 标的代码或简称

        Returns:
            ContractInfo 列表
        """
        underlying_code = self._resolve_underlying(underlying)
        return [
            info for info in self.contracts.values()
            if info.underlying_code == underlying_code
        ]

    def _resolve_underlying(self, underlying: str) -> str:
        """将简称或代码统一解析为标准代码"""
        if underlying in UNDERLYING_MAP:
            return UNDERLYING_MAP[underlying]
        return normalize_code(underlying, ".SH")

    def _parse_optionchain_row(self, row: dict) -> Optional[ContractInfo]:
        """
        解析 optionchain CSV 的一行为 ContractInfo。

        自动识别调整型合约（option_name 末尾带 A/B/C 标记）。
        """
        code = row.get("option_code") or row.get("wind_code") or ""
        if not code:
            return None

        short_name = (row.get("option_name") or "").strip()
        us_code = normalize_code((row.get("us_code") or "").strip(), ".SH")
        if not us_code:
            return None

        call_put = (row.get("call_put") or "").strip()
        if call_put == "认购":
            option_type = OptionType.CALL
        elif call_put == "认沽":
            option_type = OptionType.PUT
        else:
            return None

        try:
            strike_price = float(row.get("strike_price", 0))
        except (TypeError, ValueError):
            return None

        list_date = self._parse_date(row.get("first_tradedate"))
        expiry_date = self._parse_date(row.get("last_tradedate"))
        if list_date is None or expiry_date is None:
            return None

        delivery_month = str(row.get("month") or "").strip()

        mult_raw = row.get("multiplier")
        try:
            contract_unit = int(float(mult_raw)) if mult_raw else 10000
            if contract_unit <= 0:
                contract_unit = 10000
        except (TypeError, ValueError):
            contract_unit = 10000

        is_adjusted = bool(_ADJUSTED_TAIL_RE.search(short_name))

        return ContractInfo(
            contract_code=normalize_code(code, ".SH"),
            short_name=short_name,
            underlying_code=us_code,
            option_type=option_type,
            strike_price=strike_price,
            list_date=list_date,
            expiry_date=expiry_date,
            delivery_month=delivery_month,
            is_adjusted=is_adjusted,
            contract_unit=contract_unit,
        )

    @staticmethod
    def _parse_date(s: str) -> Optional[date]:
        """解析 YYYY-MM-DD 为 date"""
        if not s:
            return None
        try:
            return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
