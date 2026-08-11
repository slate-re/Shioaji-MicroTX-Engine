"""不含機密的引擎健康快照與週期原子寫入器。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any
from zoneinfo import ZoneInfo

from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """可公開落盤、且不含任何券商憑證的狀態快照。"""

    schema_version: int
    written_at: str
    pid: int
    engine_state: str
    mode: str
    symbol: str
    session: str | None
    broker_connected: bool | None
    degraded: bool
    degraded_reason: str
    position: dict[str, object] | None
    pnl: dict[str, float] | None
    trade_count: int | None
    strategies: list[dict[str, object]] | None
    feed: dict[str, object] | None
    emergency: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        """轉為可直接 JSON 序列化的字典。"""
        return asdict(self)


class StatusWriter:
    """以有界取鎖週期寫入狀態；鎖忙時仍輸出降級診斷。"""

    def __init__(
        self,
        path: Path,
        *,
        interval_sec: float,
        lock: RLock,
        full_snapshot: Callable[[], StatusSnapshot],
        degraded_snapshot: Callable[[], StatusSnapshot],
    ) -> None:
        self._path = path
        self._interval_sec = interval_sec
        self._lock = lock
        self._full_snapshot = full_snapshot
        self._degraded_snapshot = degraded_snapshot
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """啟動 daemon writer；重複呼叫不會建立第二條執行緒。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="StatusWriter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止背景執行緒；最後快照由引擎切至 STOPPED 後明確寫入。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_sec + 1.0)

    def write_once(self) -> None:
        """有界取得共用鎖並原子寫入一次；所有失敗只記錄警告。"""
        try:
            acquired = self._lock.acquire(timeout=0.5)
            try:
                snapshot = self._full_snapshot() if acquired else self._degraded_snapshot()
            finally:
                if acquired:
                    self._lock.release()
            self._write_payload(snapshot.to_dict())
        except Exception:
            logger.warning("狀態快照寫入失敗：%s", self._path, exc_info=True)

    def _run(self) -> None:
        self.write_once()
        while not self._stop_event.wait(self._interval_sec):
            self.write_once()

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._path)


def snapshot_time() -> str:
    """回傳台北時區 ISO 8601 時戳。"""
    return datetime.now(_TAIPEI).isoformat()
