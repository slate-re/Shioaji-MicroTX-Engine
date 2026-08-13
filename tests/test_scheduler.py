"""Scheduler 台北時區、跨日夜盤與每日回呼測試。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from freezegun import freeze_time

from microtx.config import Settings
from microtx.engine.scheduler import Scheduler
from microtx.enums import SessionType

_TAIPEI = ZoneInfo("Asia/Taipei")


def _settings(*, night: bool = False) -> Settings:
    return Settings(_env_file=None, enable_night_session=night)


def _scheduler(*, night: bool = False) -> tuple[Scheduler, list[str], list[str]]:
    closes: list[str] = []
    resets: list[str] = []
    scheduler = Scheduler(
        _settings(night=night),
        on_force_close=closes.append,
        on_reset_daily=lambda: resets.append("reset"),
    )
    return scheduler, closes, resets


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=_TAIPEI)


def test_day_session_boundaries() -> None:
    scheduler, _, _ = _scheduler()
    assert scheduler.current_session(_dt("2026-01-05T08:44")) is SessionType.CLOSED
    assert scheduler.current_session(_dt("2026-01-05T08:45")) is SessionType.DAY
    assert scheduler.current_session(_dt("2026-01-05T13:44")) is SessionType.DAY
    assert scheduler.current_session(_dt("2026-01-05T13:45")) is SessionType.CLOSED


def test_night_session_crosses_midnight() -> None:
    scheduler, _, _ = _scheduler(night=True)
    assert scheduler.current_session(_dt("2026-01-05T15:00")) is SessionType.NIGHT
    assert scheduler.current_session(_dt("2026-01-06T04:59")) is SessionType.NIGHT
    assert scheduler.current_session(_dt("2026-01-06T05:00")) is SessionType.CLOSED


def test_weekend_uses_night_trading_date() -> None:
    scheduler, _, _ = _scheduler(night=True)
    assert scheduler.current_session(_dt("2026-01-10T02:00")) is SessionType.NIGHT
    assert scheduler.current_session(_dt("2026-01-11T02:00")) is SessionType.CLOSED
    assert scheduler.current_session(_dt("2026-01-10T16:00")) is SessionType.CLOSED


def test_force_close_only_once_per_day() -> None:
    scheduler, closes, _ = _scheduler()
    scheduler._check(_dt("2026-01-05T13:39"))
    scheduler._check(_dt("2026-01-05T13:40"))
    scheduler._check(_dt("2026-01-05T13:44"))
    assert closes == ["scheduler"]


def test_midnight_does_not_reset_night_session_daily_state() -> None:
    scheduler, _, resets = _scheduler(night=True)
    # 回歸 bug B：夜盤跨午夜仍屬同一交易日，絕不可在盤中清空風控累計。
    with freeze_time(_dt("2026-01-05T23:59")):
        scheduler._check(datetime.now(_TAIPEI))
    with freeze_time(_dt("2026-01-06T00:01")):
        scheduler._check(datetime.now(_TAIPEI))
    assert resets == []


def test_crossing_trading_day_boundary_resets_exactly_once() -> None:
    scheduler, _, resets = _scheduler(night=True)
    with freeze_time(_dt("2026-01-10T05:59")):
        scheduler._check(datetime.now(_TAIPEI))
    with freeze_time(_dt("2026-01-10T06:00")):
        scheduler._check(datetime.now(_TAIPEI))
    with freeze_time(_dt("2026-01-10T07:00")):
        scheduler._check(datetime.now(_TAIPEI))
    assert resets == ["reset"]


def test_naive_and_utc_times_are_normalized_to_taipei() -> None:
    scheduler, _, _ = _scheduler()
    assert scheduler.current_session(datetime(2026, 1, 5, 9, 0)) is SessionType.DAY
    utc = ZoneInfo("UTC")
    assert scheduler.current_session(datetime(2026, 1, 5, 1, 0, tzinfo=utc)) is SessionType.DAY


def test_start_stop_are_idempotent() -> None:
    scheduler, _, _ = _scheduler()
    with freeze_time("2026-01-05 09:00:00", tz_offset=8):
        scheduler.start()
        scheduler.start()
        scheduler.stop()
        scheduler.stop()
    assert scheduler.is_tradable(_dt("2026-01-05T09:00")) is True
