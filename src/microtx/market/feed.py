"""非阻塞行情正規化與有界佇列。"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from microtx.broker.base import BrokerGateway, RawTick
from microtx.market.tick import TickEvent


@dataclass(frozen=True, slots=True)
class FeedStats:
    """行情健康度統計；除 queue_depth 外皆為單調累計值。"""

    received: int
    filtered_simtrade: int
    delivered: int
    consumed: int
    evicted_overflow: int
    queue_depth: int
    last_tick_at: datetime | None
    max_latency_ms: float


class MarketFeed:
    """將券商 tick 過濾、正規化後送入非阻塞有界佇列。"""

    def __init__(
        self,
        gateway: BrokerGateway,
        *,
        symbol: str,
        queue_maxsize: int = 1000,
        drop_simtrade: bool = True,
    ) -> None:
        """初始化行情來源。

        Args:
            gateway: 券商抽象閘道。
            symbol: 設定與訂閱使用的商品代碼。
            queue_maxsize: 行情佇列最大容量。
            drop_simtrade: 是否過濾試撮 tick。

        Raises:
            ValueError: 佇列容量小於 1。
        """
        if queue_maxsize < 1:
            raise ValueError("行情佇列容量必須大於 0")
        self._gateway = gateway
        self._symbol = symbol
        self._drop_simtrade = drop_simtrade
        self._queue: queue.Queue[TickEvent] = queue.Queue(maxsize=queue_maxsize)
        self._stats_lock = Lock()
        self._lifecycle_lock = Lock()
        self._running = False
        self._received = 0
        self._filtered_simtrade = 0
        self._delivered = 0
        self._consumed = 0
        self._evicted_overflow = 0
        self._queue_depth = 0
        self._last_tick_at: datetime | None = None
        self._max_latency_ms = 0.0

    def start(self) -> None:
        """冪等地訂閱行情並註冊 callback。"""
        with self._lifecycle_lock:
            if self._running:
                return
            self._gateway.subscribe_ticks(self._symbol, self._on_raw_tick)
            self._running = True

    def stop(self) -> None:
        """冪等地取消行情訂閱。"""
        with self._lifecycle_lock:
            if not self._running:
                return
            self._gateway.unsubscribe_ticks(self._symbol)
            self._running = False

    def resubscribe(self) -> None:
        """連線恢復後重新訂閱，並保留既有統計。"""
        with self._lifecycle_lock:
            if not self._running:
                return
            self._gateway.subscribe_ticks(self._symbol, self._on_raw_tick)

    def get(self, timeout: float | None = None) -> TickEvent | None:
        """取出下一筆 tick；逾時時回傳 None。"""
        try:
            event = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._stats_lock:
            self._consumed += 1
            self._queue_depth -= 1
        return event

    @property
    def stats(self) -> FeedStats:
        """回傳不可變的行情健康度統計快照。"""
        with self._stats_lock:
            return FeedStats(
                received=self._received,
                filtered_simtrade=self._filtered_simtrade,
                delivered=self._delivered,
                consumed=self._consumed,
                evicted_overflow=self._evicted_overflow,
                queue_depth=self._queue_depth,
                last_tick_at=self._last_tick_at,
                max_latency_ms=self._max_latency_ms,
            )

    def _on_raw_tick(self, raw: RawTick) -> None:
        # 試撮必須先過濾，避免無效行情進入正規化與佇列路徑。
        if self._drop_simtrade and raw.simtrade:
            with self._stats_lock:
                self._received += 1
                self._filtered_simtrade += 1
            return
        event = TickEvent.from_raw(raw, symbol=self._symbol)
        self._enqueue(event)

    def _enqueue(self, event: TickEvent) -> None:
        with self._stats_lock:
            self._received += 1
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    self._evicted_overflow += 1
                    self._queue_depth -= 1
                self._queue.put_nowait(event)
            self._delivered += 1
            self._queue_depth += 1
            self._last_tick_at = event.received_at
            self._max_latency_ms = max(self._max_latency_ms, event.latency_ms)
