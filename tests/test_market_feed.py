"""MarketFeed 非阻塞佇列與統計守恆測試。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from threading import Thread
from time import perf_counter
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from microtx.broker.base import BrokerGateway, RawTick
from microtx.broker.paper_gateway import PaperGateway
from microtx.contracts import TMF
from microtx.market.feed import FeedStats, MarketFeed
from microtx.market.tick import TickEvent

_TAIPEI = ZoneInfo("Asia/Taipei")


def _raw(
    price: float = 23_000.0,
    *,
    simtrade: bool = False,
    timestamp: datetime | None = None,
) -> RawTick:
    return RawTick(
        code=TMF.symbol,
        timestamp=timestamp or datetime.now(_TAIPEI),
        price=price,
        volume=1,
        total_volume=1,
        tick_type=0,
        simtrade=simtrade,
    )


def _assert_conservation(stats: FeedStats) -> None:
    assert stats.received == stats.filtered_simtrade + stats.delivered
    assert stats.delivered == stats.consumed + stats.queue_depth + stats.evicted_overflow


def test_tick_event_from_raw_and_latency() -> None:
    exchange_time = datetime(2026, 1, 5, 8, 45, tzinfo=_TAIPEI)
    raw = _raw(23_123.0, timestamp=exchange_time)

    with freeze_time(exchange_time + timedelta(milliseconds=125)):
        event = TickEvent.from_raw(raw, symbol=TMF.symbol)

    assert event.symbol == TMF.symbol
    assert event.code == raw.code
    assert event.price == 23_123.0
    assert event.latency_ms == pytest.approx(125.0)
    with pytest.raises(FrozenInstanceError):
        event.__setattr__("price", 1.0)


def test_simtrade_is_filtered_before_queueing() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol)
    feed.start()

    gateway.feed_tick(23_000.0, simtrade=True)

    assert feed.get(timeout=0.0) is None
    stats = feed.stats
    assert (stats.received, stats.filtered_simtrade, stats.delivered) == (1, 1, 0)
    _assert_conservation(stats)


def test_simtrade_can_be_retained() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol, drop_simtrade=False)
    feed.start()

    gateway.feed_tick(23_000.0, simtrade=True)

    assert feed.get(timeout=0.0) is not None
    assert feed.stats.filtered_simtrade == 0
    _assert_conservation(feed.stats)


def test_overflow_evicts_oldest_and_keeps_latest() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol, queue_maxsize=5)
    feed.start()

    for price in range(23_000, 23_105):
        gateway.feed_tick(float(price))

    retained = [feed.get(timeout=0.0) for _ in range(5)]
    assert [event.price for event in retained if event is not None] == [
        float(price) for price in range(23_100, 23_105)
    ]
    stats = feed.stats
    assert (stats.delivered, stats.consumed, stats.evicted_overflow, stats.queue_depth) == (
        105,
        5,
        100,
        0,
    )
    _assert_conservation(stats)


def test_overflow_callback_path_is_nonblocking() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol, queue_maxsize=1)
    feed.start()
    gateway.feed_tick(23_000.0)

    started = perf_counter()
    gateway.feed_tick(23_001.0)
    elapsed = perf_counter() - started

    assert elapsed < 0.001


def test_entry_and_exit_conservation_without_overflow() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol)
    feed.start()

    gateway.feed_tick(23_000.0)
    gateway.feed_tick(23_001.0, simtrade=True)
    gateway.feed_tick(23_002.0)
    assert feed.get(timeout=0.0) is not None

    _assert_conservation(feed.stats)
    assert feed.stats.last_tick_at is not None


def test_conservation_after_overflow_and_partial_consumption() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol, queue_maxsize=10)
    feed.start()
    for price in range(200):
        gateway.feed_tick(float(price))
    for _ in range(4):
        assert feed.get(timeout=0.0) is not None

    stats = feed.stats
    _assert_conservation(stats)
    assert (stats.consumed, stats.queue_depth, stats.evicted_overflow) == (4, 6, 190)


def test_multithreaded_feed_preserves_statistics() -> None:
    gateway = PaperGateway(spec=TMF)
    feed = MarketFeed(gateway, symbol=TMF.symbol, queue_maxsize=50)
    feed.start()

    def produce(offset: int) -> None:
        for index in range(100):
            gateway.feed_tick(float(offset + index))

    workers = [Thread(target=produce, args=(index * 1000,)) for index in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert not any(worker.is_alive() for worker in workers)
    stats = feed.stats
    assert stats.received == 800
    _assert_conservation(stats)


def test_start_stop_and_resubscribe_are_idempotent(mocker) -> None:
    gateway = mocker.Mock(spec=BrokerGateway)
    feed = MarketFeed(gateway, symbol=TMF.symbol)

    feed.start()
    feed.start()
    feed.resubscribe()
    feed.stop()
    feed.stop()
    feed.resubscribe()

    assert gateway.subscribe_ticks.call_count == 2
    gateway.unsubscribe_ticks.assert_called_once_with(TMF.symbol)


def test_invalid_queue_size_is_rejected() -> None:
    gateway = mocker_gateway = PaperGateway(spec=TMF)
    with pytest.raises(ValueError, match="容量必須大於 0"):
        MarketFeed(mocker_gateway, symbol=TMF.symbol, queue_maxsize=0)
    assert gateway is mocker_gateway
