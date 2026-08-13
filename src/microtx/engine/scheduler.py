"""固定台北時區的交易時段與每日排程器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from threading import Event, Lock, Thread
from zoneinfo import ZoneInfo

from microtx.config import Settings
from microtx.engine.trading_day import trading_date
from microtx.enums import SessionType

_TAIPEI = ZoneInfo("Asia/Taipei")
_NIGHT_START = time(15, 0)
_NIGHT_END = time(5, 0)


class Scheduler:
    """每秒檢查交易時段、日盤強平與跨日重置。"""

    def __init__(
        self,
        settings: Settings,
        *,
        on_force_close: Callable[[str], None],
        on_reset_daily: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._on_force_close = on_force_close
        self._on_reset_daily = on_reset_daily
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._thread: Thread | None = None
        self._last_force_close_date: date | None = None
        self._last_trading_date: date | None = None

    def current_session(self, now: datetime | None = None) -> SessionType:
        """依台北時間回傳日盤、夜盤或休市。"""
        current = self._as_taipei(now)
        current_time = current.time().replace(tzinfo=None)
        if self._is_weekday(current.date()) and (
            self._settings.session_start <= current_time < self._settings.session_end
        ):
            return SessionType.DAY
        if self._settings.enable_night_session:
            session_trading_date = (
                current.date()
                if current_time >= _NIGHT_START
                else current.date() - timedelta(days=1)
            )
            in_night = current_time >= _NIGHT_START or current_time < _NIGHT_END
            if in_night and self._is_weekday(session_trading_date):
                return SessionType.NIGHT
        return SessionType.CLOSED

    def is_tradable(self, now: datetime | None = None) -> bool:
        """回傳指定時間是否屬啟用的交易時段。"""
        return self.current_session(now) is not SessionType.CLOSED

    def start(self) -> None:
        """冪等啟動背景排程執行緒。"""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = Thread(target=self._run, name="microtx-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """停止背景排程執行緒。"""
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._check(datetime.now(_TAIPEI))
            self._stop_event.wait(1.0)

    def _check(self, now: datetime) -> None:
        current = self._as_taipei(now)
        current_trading_date = trading_date(current, boundary=self._settings.trading_day_boundary)
        if self._last_trading_date is not None and current_trading_date != self._last_trading_date:
            self._on_reset_daily()
        self._last_trading_date = current_trading_date
        current_time = current.time().replace(tzinfo=None)
        if (
            self.current_session(current) is SessionType.DAY
            and current_time >= self._settings.force_close_time
            and self._last_force_close_date != current_trading_date
        ):
            self._last_force_close_date = current_trading_date
            self._on_force_close("scheduler")

    @staticmethod
    def _as_taipei(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(_TAIPEI)
        if now.tzinfo is None:
            return now.replace(tzinfo=_TAIPEI)
        return now.astimezone(_TAIPEI)

    @staticmethod
    def _is_weekday(day: date) -> bool:
        # TODO：未來接入臺灣期交所休市行事曆；本版先排除週六、週日。
        return day.weekday() < 5
