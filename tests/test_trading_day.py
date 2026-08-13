"""台指期交易日邊界的單一定義測試。"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from microtx.engine.trading_day import trading_date

_TAIPEI = ZoneInfo("Asia/Taipei")
_BOUNDARY = time(6, 0)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_TAIPEI)


def test_evening_belongs_to_same_trading_date() -> None:
    assert trading_date(_dt("2026-01-09T22:30"), boundary=_BOUNDARY) == date(2026, 1, 9)


def test_after_midnight_night_session_belongs_to_previous_date() -> None:
    assert trading_date(_dt("2026-01-10T01:30"), boundary=_BOUNDARY) == date(2026, 1, 9)


def test_after_boundary_belongs_to_calendar_date() -> None:
    assert trading_date(_dt("2026-01-10T07:00"), boundary=_BOUNDARY) == date(2026, 1, 10)


def test_minutes_around_boundary_are_different_trading_dates() -> None:
    assert trading_date(_dt("2026-01-10T05:59"), boundary=_BOUNDARY) == date(2026, 1, 9)
    assert trading_date(_dt("2026-01-10T06:01"), boundary=_BOUNDARY) == date(2026, 1, 10)


def test_naive_and_utc_datetime_are_normalized_to_taipei() -> None:
    assert trading_date(datetime(2026, 1, 10, 1, 30), boundary=_BOUNDARY) == date(2026, 1, 9)
    utc = ZoneInfo("UTC")
    assert trading_date(datetime(2026, 1, 9, 17, 30, tzinfo=utc), boundary=_BOUNDARY) == date(
        2026, 1, 9
    )
