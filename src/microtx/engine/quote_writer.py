"""不接觸交易鎖的最新報價快照寫入器。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo

from microtx.market.tick import TickEvent
from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """只包含行情、不包含部位與損益的顯示快照。"""

    schema_version: int
    symbol: str
    last_price: float | None
    tick_at: str | None
    written_at: str
    latency_ms: float | None


class QuoteWriter:
    """週期讀取單一屬性並原子寫檔，不取得任何交易共用鎖。"""

    def __init__(
        self,
        path: Path,
        *,
        symbol: str,
        interval_sec: float,
        latest_tick: Callable[[], TickEvent | None],
    ) -> None:
        self._path = path
        self._symbol = symbol
        self._interval_sec = interval_sec
        self._latest_tick = latest_tick
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """啟動 daemon writer。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="QuoteWriter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止 writer 並等待有限時間。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_sec + 1.0)

    def write_once(self) -> None:
        """寫入一次；失敗只記警告且不重試。"""
        try:
            tick = self._latest_tick()
            now = datetime.now(_TAIPEI)
            snapshot = QuoteSnapshot(
                1,
                self._symbol,
                tick.price if tick else None,
                tick.timestamp.isoformat() if tick else None,
                now.isoformat(),
                tick.latency_ms if tick else None,
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(asdict(snapshot), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        except Exception:
            logger.warning("報價快照寫入失敗：%s", self._path, exc_info=True)

    def _run(self) -> None:
        self.write_once()
        while not self._stop_event.wait(self._interval_sec):
            self.write_once()
