"""
基于 CBOE VIX 白皮书 Interest Rate Calculation 的无风险利率构建模块。
"""

from __future__ import annotations

import csv
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scipy.interpolate import CubicSpline


# 将期限标签映射到标准天数 ti（可包含扩展长端点）
_TENOR_TO_DAYS: Dict[str, int] = {
    "0.08y": 30,
    "0.17y": 60,
    "0.25y": 91,
    "0.5y": 182,
    "1.0y": 365,
    "1y": 365,
    "2.0y": 730,
    "2y": 730,
    "3.0y": 1095,
    "3y": 1095,
    "5.0y": 1825,
    "5y": 1825,
    "7.0y": 2555,
    "7y": 2555,
    "10.0y": 3650,
    "10y": 3650,
    "15.0y": 5475,
    "15y": 5475,
    "20.0y": 7300,
    "20y": 7300,
    "30.0y": 10950,
    "30y": 10950,
    "40.0y": 14600,
    "40y": 14600,
    "50.0y": 18250,
    "50y": 18250,
}


class BoundedCubicSplineRate:
    """
    Bounded Natural Cubic Spline 无风险利率构建器。

    输入为 BEY(%)，输出为连续复利 r（年化小数）。
    """

    def __init__(
        self,
        tenor_days: Sequence[int],
        bey_rates: Sequence[float],
        *,
        data_date: Optional[date] = None,
    ) -> None:
        points: Dict[int, float] = {}
        for t, y in zip(tenor_days, bey_rates):
            if t is None or y is None:
                continue
            try:
                t_int = int(t)
                y_f = float(y)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(y_f):
                continue
            points[t_int] = y_f

        if len(points) < 2:
            raise ValueError("有效收益率点不足，至少需要 2 个点进行样条插值")

        pairs = sorted(points.items(), key=lambda x: x[0])
        self._t: List[float] = [float(p[0]) for p in pairs]
        self._cmt: List[float] = [float(p[1]) for p in pairs]  # BEY(%)
        self.data_date: Optional[date] = data_date
        self._spline = CubicSpline(self._t, self._cmt, bc_type="natural", extrapolate=True)

    @classmethod
    def from_cgb_csv(
        cls,
        csv_path: str | Path,
        *,
        expected_date: Optional[date] = None,
        require_date_match: bool = True,
    ) -> "BoundedCubicSplineRate":
        path = Path(csv_path)
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            row = next(reader, None)
        if row is None:
            raise ValueError(f"中债曲线文件为空: {path}")
        parsed_date = _parse_row_date(row)
        if expected_date is not None and require_date_match and parsed_date != expected_date:
            raise ValueError(
                f"中债曲线日期不匹配: 期望 {expected_date.isoformat()}，实际 {parsed_date}"
            )
        return cls.from_cgb_row(row, data_date=parsed_date)

    @classmethod
    def from_cgb_daily(
        cls,
        *,
        base_dir: str | Path = "D:/MARKET_DATA",
        target_date: Optional[date] = None,
        require_exists: bool = True,
    ) -> "BoundedCubicSplineRate":
        """
        从标准日文件加载曲线。
        - target_date=None: 默认使用今天
        - 当文件不存在且 require_exists=True 时抛错
        """
        d = target_date or date.today()
        csv_path = (
            Path(base_dir)
            / "macro"
            / "cgb_yield"
            / f"cgb_yieldcurve_{d.strftime('%Y%m%d')}.csv"
        )
        if require_exists and not csv_path.exists():
            raise FileNotFoundError(
                f"未找到 {d.isoformat()} 的中债曲线文件: {csv_path}"
            )
        return cls.from_cgb_csv(csv_path, expected_date=d, require_date_match=True)

    @classmethod
    def from_cgb_row(
        cls,
        row: Dict[str, Any],
        *,
        data_date: Optional[date] = None,
    ) -> "BoundedCubicSplineRate":
        tenor_days: List[int] = []
        bey_rates: List[float] = []
        for key, val in row.items():
            if key is None:
                continue
            k = str(key).strip()
            if k == "date":
                continue
            if k not in _TENOR_TO_DAYS:
                continue
            if val in (None, "", "None"):
                continue
            try:
                y = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(y):
                continue
            tenor_days.append(_TENOR_TO_DAYS[k])
            bey_rates.append(y)
        return cls(tenor_days, bey_rates, data_date=data_date)

    def get_rate(self, t_days: float) -> float:
        """
        给定到期天数，返回连续复利无风险利率 r_t（年化小数）。
        """
        if t_days <= 0:
            raise ValueError("t_days 必须为正数")

        t = float(t_days)
        bey_raw = float(self._spline(t))  # BEY(%)
        bey_bounded = self._apply_bounds(t, bey_raw)

        # BEY(%) -> APY -> 连续复利
        apy = (1.0 + bey_bounded / 200.0) ** 2 - 1.0
        return math.log(1.0 + apy)

    def _apply_bounds(self, t: float, bey_raw: float) -> float:
        t_list = self._t
        y_list = self._cmt

        # 左外推：t < t1
        if t < t_list[0]:
            lower, upper = self._left_extrapolation_bounds(t)
            return max(min(bey_raw, max(lower, upper)), min(lower, upper))

        # 右外推：t > tn（白皮书重点在左端；右端采用最后相邻区间边界）
        if t > t_list[-1]:
            lower = min(y_list[-2], y_list[-1])
            upper = max(y_list[-2], y_list[-1])
            return max(min(bey_raw, upper), lower)

        # 插值区间：ti < t < t_{i+1}
        for i in range(len(t_list) - 1):
            t_i, t_j = t_list[i], t_list[i + 1]
            if t_i <= t <= t_j:
                lower = min(y_list[i], y_list[i + 1])
                upper = max(y_list[i], y_list[i + 1])
                return max(min(bey_raw, upper), lower)

        return bey_raw

    def _left_extrapolation_bounds(self, t: float) -> Tuple[float, float]:
        t1 = self._t[0]
        c1 = self._cmt[0]

        # m_lower: 找到下一个 >= CMT1 的最短期限点；不存在则 0
        m_lower = 0.0
        for tx, cx in zip(self._t[1:], self._cmt[1:]):
            if cx >= c1:
                m_lower = (cx - c1) / (tx - t1)
                break

        # m_upper: 找到下一个 <= CMT1 的最短期限点；不存在则 0
        m_upper = 0.0
        for tz, cz in zip(self._t[1:], self._cmt[1:]):
            if cz <= c1:
                m_upper = (cz - c1) / (tz - t1)
                break

        lower_val = c1 + m_lower * (t - t1)
        upper_val = c1 + m_upper * (t - t1)
        return lower_val, upper_val


def _parse_row_date(row: Dict[str, Any]) -> Optional[date]:
    raw = row.get("date")
    if raw in (None, "", "None"):
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

