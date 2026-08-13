"""台指期交易日的唯一邊界定義。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_TAIPEI = ZoneInfo("Asia/Taipei")


def trading_date(now: datetime, *, boundary: time) -> date:
    """回傳指定時間所屬的台指期交易日。

    凌晨夜盤仍歸屬前一交易日；預設邊界應設在夜盤收盤與日盤開盤間。

    Args:
        now: 任意時區的時間；無時區時視為台北時間。
        boundary: 交易日切換時間。

    Returns:
        台北市場語意下的交易日期。
    """
    taipei_now = now.replace(tzinfo=_TAIPEI) if now.tzinfo is None else now.astimezone(_TAIPEI)
    if taipei_now.time().replace(tzinfo=None) < boundary:
        return taipei_now.date() - timedelta(days=1)
    return taipei_now.date()
