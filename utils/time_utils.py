from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# 统一使用北京时间（UTC+8），避免受机器本地时区影响。
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def bj_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def bj_now_naive() -> datetime:
    """返回北京时间的 naive datetime（保留现有代码兼容性）。"""
    return bj_now().replace(tzinfo=None)


def bj_today() -> date:
    return bj_now().date()


def bj_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, BEIJING_TZ)


_TRADE_DATE_SET: set | None = None


def get_trade_date_set() -> set:
    """懒加载 A 股交易日历（akshare），失败时返回空集触发回退。"""
    global _TRADE_DATE_SET
    if _TRADE_DATE_SET is None:
        try:
            import akshare as ak
            cal = ak.tool_trade_date_hist_sina()
            _TRADE_DATE_SET = set(cal["trade_date"].tolist())
        except Exception:
            _TRADE_DATE_SET = set()
    return _TRADE_DATE_SET


def trading_days_until(expiry: date, today: date) -> int:
    """从 today 到 expiry（含两端）的 A 股交易日数；akshare 不可用时回退到工作日数。"""
    trade_set = get_trade_date_set()
    count = 0
    d = today
    while d <= expiry:
        if trade_set:
            if d in trade_set:
                count += 1
        else:
            if d.weekday() < 5:
                count += 1
        d += timedelta(days=1)
    return count

