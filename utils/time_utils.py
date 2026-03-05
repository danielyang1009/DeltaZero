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

