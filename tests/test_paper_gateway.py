"""PaperGateway 離線撮合與安全不變式測試。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Thread

import pytest

from microtx.broker.base import (
    AckEvent,
    CancelEvent,
    FillEvent,
    OrderRequest,
    RawTick,
    RejectEvent,
    new_client_id,
)
from microtx.broker.paper_gateway import PaperGateway
from microtx.contracts import TMF
from microtx.enums import Direction, EventOrder, OrderIntent, PriceType, TimeInForce
from microtx.exceptions import ConnectionLostError


def _gateway(**kwargs: object) -> PaperGateway:
    gateway = PaperGateway(spec=TMF, **kwargs)
    gateway.connect()
    return gateway


def _request(
    *,
    action: Direction = Direction.LONG,
    quantity: int = 1,
    price: float | None = None,
    price_type: PriceType = PriceType.MKP,
    time_in_force: TimeInForce = TimeInForce.ROD,
    intent: OrderIntent = OrderIntent.ENTRY,
    client_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        symbol=TMF.symbol,
        action=action,
        quantity=quantity,
        price=price,
        price_type=price_type,
        time_in_force=time_in_force,
        intent=intent,
        client_id=client_id or new_client_id(),
    )


def _events(gateway: PaperGateway) -> list[object]:
    events: list[object] = []
    gateway.set_order_event_callback(events.append)
    return events


@pytest.mark.parametrize(
    ("action", "limit", "non_crossing", "crossing"),
    [
        (Direction.LONG, 22_990.0, 23_000.0, 22_989.0),
        (Direction.SHORT, 23_010.0, 23_000.0, 23_011.0),
    ],
)
def test_limit_rod_waits_for_price_crossing(
    action: Direction, limit: float, non_crossing: float, crossing: float
) -> None:
    gateway = _gateway(initial_price=non_crossing)
    events = _events(gateway)

    gateway.place_order(
        _request(
            action=action, price=limit, price_type=PriceType.LMT, time_in_force=TimeInForce.ROD
        )
    )
    assert [type(event) for event in events] == [AckEvent]
    assert len(gateway.list_open_orders()) == 1

    gateway.feed_tick(crossing)

    fill = next(event for event in events if isinstance(event, FillEvent))
    assert fill.price == limit
    assert gateway.list_open_orders() == []


def test_limit_ioc_fills_when_current_price_crosses() -> None:
    gateway = _gateway(initial_price=22_999.0)
    events = _events(gateway)

    gateway.place_order(
        _request(price=23_000.0, price_type=PriceType.LMT, time_in_force=TimeInForce.IOC)
    )

    assert [type(event) for event in events] == [FillEvent, AckEvent]
    assert gateway.list_positions()[0].quantity == 1


def test_limit_ioc_cancels_when_price_does_not_cross() -> None:
    gateway = _gateway(initial_price=23_001.0)
    events = _events(gateway)

    gateway.place_order(
        _request(price=23_000.0, price_type=PriceType.LMT, time_in_force=TimeInForce.IOC)
    )

    assert [type(event) for event in events] == [AckEvent, CancelEvent]
    assert isinstance(events[-1], CancelEvent)
    assert events[-1].reason == "ioc_expired"


@pytest.mark.parametrize("price_type", [PriceType.MKP, PriceType.MKT])
def test_market_types_fill_with_directional_slippage(price_type: PriceType) -> None:
    gateway = _gateway(initial_price=23_000.0, slippage_ticks=2)
    events = _events(gateway)

    gateway.place_order(_request(action=Direction.LONG, price_type=price_type))
    gateway.place_order(_request(action=Direction.SHORT, price_type=price_type))

    fills = [event for event in events if isinstance(event, FillEvent)]
    assert [fill.price for fill in fills] == [23_002.0, 22_998.0]


def test_tick_volume_does_not_limit_default_liquidity() -> None:
    gateway = _gateway(initial_price=23_000.0)
    gateway.feed_tick(23_000.0, volume=1)

    gateway.place_order(_request(quantity=5))

    assert gateway.list_positions()[0].quantity == 5


def test_partial_ioc_fills_available_and_cancels_remainder() -> None:
    gateway = _gateway(max_fill_quantity_per_tick=2)
    events = _events(gateway)

    gateway.place_order(_request(quantity=5, time_in_force=TimeInForce.IOC))

    fill = next(event for event in events if isinstance(event, FillEvent))
    cancel = next(event for event in events if isinstance(event, CancelEvent))
    assert fill.quantity == 2
    assert (cancel.reason, cancel.cancelled_quantity) == ("ioc_expired", 3)
    assert gateway.list_open_orders() == []


def test_partial_fok_cancels_entire_order_without_fill() -> None:
    gateway = _gateway(max_fill_quantity_per_tick=2)
    events = _events(gateway)

    gateway.place_order(_request(quantity=5, time_in_force=TimeInForce.FOK))

    assert not any(isinstance(event, FillEvent) for event in events)
    cancel = next(event for event in events if isinstance(event, CancelEvent))
    assert (cancel.reason, cancel.cancelled_quantity) == ("fok_expired", 5)
    assert gateway.list_positions() == []


def test_partial_rod_keeps_remainder_until_later_ticks() -> None:
    gateway = _gateway(max_fill_quantity_per_tick=2)

    gateway.place_order(
        _request(
            quantity=5, price=23_000.0, price_type=PriceType.LMT, time_in_force=TimeInForce.ROD
        )
    )

    order = gateway.list_open_orders()[0]
    assert (order.quantity, order.filled_quantity) == (5, 2)
    gateway.feed_tick(23_000.0, volume=1)
    assert gateway.list_open_orders()[0].filled_quantity == 4
    gateway.feed_tick(23_000.0, volume=1)
    assert gateway.list_open_orders() == []
    assert gateway.list_positions()[0].quantity == 5


@pytest.mark.parametrize("event_order", list(EventOrder))
def test_immediate_event_order_is_explicit(event_order: EventOrder) -> None:
    gateway = _gateway(event_order=event_order)
    events = _events(gateway)

    gateway.place_order(_request())

    expected = (
        [FillEvent, AckEvent] if event_order is EventOrder.FILL_FIRST else [AckEvent, FillEvent]
    )
    assert [type(event) for event in events] == expected
    ack = next(event for event in events if isinstance(event, AckEvent))
    assert ack.exchange_order_no.startswith("P")


@pytest.mark.parametrize("event_order", list(EventOrder))
def test_fill_delay_does_not_select_event_order(event_order: EventOrder) -> None:
    gateway = _gateway(event_order=event_order, fill_delay_sec=10.0)
    events = _events(gateway)
    gateway.place_order(_request())
    gateway.flush_pending_events()

    expected = (
        [FillEvent, AckEvent] if event_order is EventOrder.FILL_FIRST else [AckEvent, FillEvent]
    )
    assert [type(event) for event in events] == expected


def test_order_callback_can_reenter_gateway_without_deadlock() -> None:
    gateway = _gateway()
    reentered = False

    def callback(event: object) -> None:
        nonlocal reentered
        gateway.list_positions()
        if isinstance(event, FillEvent) and not reentered:
            reentered = True
            gateway.place_order(
                _request(price=22_000.0, price_type=PriceType.LMT, time_in_force=TimeInForce.ROD)
            )

    gateway.set_order_event_callback(callback)
    worker = Thread(target=gateway.place_order, args=(_request(),))
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert reentered is True


def test_tick_callback_runs_after_gateway_lock_is_released() -> None:
    gateway = _gateway()
    lock_was_available = False

    def callback(tick: RawTick) -> None:
        del tick
        worker = Thread(target=gateway.list_positions)
        worker.start()
        worker.join(timeout=1.0)
        nonlocal lock_was_available
        lock_was_available = not worker.is_alive()

    gateway.subscribe_ticks(TMF.symbol, callback)
    gateway.feed_tick(23_001.0)

    assert lock_was_available is True


def test_disconnect_cancels_pending_timers() -> None:
    gateway = _gateway(fill_delay_sec=10.0)
    events = _events(gateway)
    gateway.place_order(_request())

    gateway.disconnect()

    assert gateway.flush_pending_events() == 0
    assert events == []


def test_cancel_all_orders_cancels_delayed_fill_events() -> None:
    gateway = _gateway(fill_delay_sec=10.0)
    events = _events(gateway)
    gateway.place_order(_request())

    gateway.cancel_all_orders()

    assert gateway.flush_pending_events() == 0
    assert not any(isinstance(event, FillEvent) for event in events)


def test_flush_pending_events_is_deterministic() -> None:
    gateway = _gateway(fill_delay_sec=10.0)
    events = _events(gateway)
    gateway.place_order(_request())

    assert gateway.flush_pending_events() == 2
    assert [type(event) for event in events] == [FillEvent, AckEvent]
    assert gateway.flush_pending_events() == 0


@pytest.mark.parametrize(
    "intent",
    [
        OrderIntent.TAKE_PROFIT,
        OrderIntent.STOP_LOSS,
        OrderIntent.FORCE_CLOSE,
        OrderIntent.EMERGENCY,
    ],
)
@pytest.mark.parametrize("opening_direction", list(Direction))
@pytest.mark.parametrize("close_quantity", [1, 2, 5])
def test_close_only_never_reverses_position(
    intent: OrderIntent, opening_direction: Direction, close_quantity: int
) -> None:
    gateway = _gateway()
    events = _events(gateway)
    gateway.place_order(_request(action=opening_direction, quantity=2))
    events.clear()

    ack = gateway.place_order(
        _request(
            action=opening_direction.opposite,
            quantity=close_quantity,
            intent=intent,
            time_in_force=TimeInForce.IOC,
        )
    )

    assert ack.accepted is True
    positions = gateway.list_positions()
    assert not positions or positions[0].direction is opening_direction
    assert not positions or positions[0].quantity == 2 - close_quantity
    if close_quantity > 2:
        cancel = next(event for event in events if isinstance(event, CancelEvent))
        assert (cancel.reason, cancel.cancelled_quantity, cancel.remaining_quantity) == (
            "over_close",
            close_quantity - 2,
            0,
        )


@pytest.mark.parametrize("direction", list(Direction))
def test_close_only_when_flat_is_accepted_then_cancelled(direction: Direction) -> None:
    gateway = _gateway()
    events = _events(gateway)

    ack = gateway.place_order(_request(action=direction, quantity=3, intent=OrderIntent.EMERGENCY))

    assert ack.accepted is True
    cancel = next(event for event in events if isinstance(event, CancelEvent))
    assert (cancel.reason, cancel.cancelled_quantity) == ("no_position", 3)
    assert gateway.list_positions() == []


def test_opposite_entry_can_reverse_after_closing_existing_position() -> None:
    gateway = _gateway()
    gateway.place_order(_request(action=Direction.LONG, quantity=2))

    gateway.place_order(_request(action=Direction.SHORT, quantity=3))

    position = gateway.list_positions()[0]
    assert (position.direction, position.quantity) == (Direction.SHORT, 1)


def test_realized_pnl_uses_contract_specification() -> None:
    gateway = _gateway(initial_price=23_000.0)
    gateway.place_order(_request(action=Direction.LONG))
    gateway.feed_tick(23_050.0)

    gateway.place_order(_request(action=Direction.SHORT, intent=OrderIntent.TAKE_PROFIT))

    assert gateway.realized_pnl == 500.0
    assert gateway.list_positions() == []


def test_opposite_fill_closes_oldest_lot_first() -> None:
    gateway = _gateway(initial_price=23_000.0)
    gateway.place_order(_request(action=Direction.LONG))
    gateway.feed_tick(23_100.0)
    gateway.place_order(_request(action=Direction.LONG))
    gateway.feed_tick(23_200.0)

    gateway.place_order(_request(action=Direction.SHORT))

    position = gateway.list_positions()[0]
    assert gateway.realized_pnl == 2_000.0
    assert (position.quantity, position.average_price) == (1, 23_100.0)


def test_duplicate_client_id_is_idempotent() -> None:
    gateway = _gateway()
    client_id = new_client_id()

    acknowledgements = [gateway.place_order(_request(client_id=client_id)) for _ in range(3)]

    assert [ack.accepted for ack in acknowledgements] == [True, False, False]
    assert gateway.list_positions()[0].quantity == 1


def test_reject_rate_rejects_entire_order(mocker) -> None:
    gateway = _gateway(reject_rate=0.5)
    events = _events(gateway)
    mocker.patch("microtx.broker.paper_gateway._RANDOM.random", return_value=0.1)

    ack = gateway.place_order(_request())

    assert ack.accepted is False
    assert [type(event) for event in events] == [RejectEvent]
    assert gateway.list_positions() == []


def test_locked_price_limit_leaves_close_order_open() -> None:
    gateway = _gateway(initial_price=23_000.0)
    gateway.place_order(_request(action=Direction.LONG))
    gateway.set_price_limits(22_000.0, 22_500.0)
    events = _events(gateway)

    ack = gateway.place_order(
        _request(
            action=Direction.SHORT, intent=OrderIntent.EMERGENCY, time_in_force=TimeInForce.IOC
        )
    )

    assert ack.accepted is True
    assert [type(event) for event in events] == [AckEvent]
    assert gateway.list_positions()[0].direction is Direction.LONG
    assert len(gateway.list_open_orders()) == 1


def test_disconnected_order_raises() -> None:
    gateway = _gateway()
    gateway.force_disconnect()

    with pytest.raises(ConnectionLostError):
        gateway.place_order(_request())


def test_cancel_one_and_cancel_all_emit_events() -> None:
    gateway = _gateway(initial_price=23_000.0)
    events = _events(gateway)
    first = gateway.place_order(
        _request(price=22_900.0, price_type=PriceType.LMT, time_in_force=TimeInForce.ROD)
    )
    gateway.place_order(
        _request(price=22_800.0, price_type=PriceType.LMT, time_in_force=TimeInForce.ROD)
    )

    assert first.broker_order_id is not None
    assert gateway.cancel_order(first.broker_order_id) is True
    assert gateway.cancel_order(first.broker_order_id) is False
    assert gateway.cancel_all_orders() == 1
    assert gateway.list_open_orders() == []
    assert [event.reason for event in events if isinstance(event, CancelEvent)] == ["user", "user"]


def test_simtrade_tick_is_forwarded_and_unsubscribe_stops_callback() -> None:
    gateway = _gateway()
    ticks: list[RawTick] = []
    gateway.subscribe_ticks(TMF.symbol, ticks.append)

    gateway.feed_tick(23_001.0, simtrade=True)
    gateway.unsubscribe_ticks(TMF.symbol)
    gateway.feed_tick(23_002.0)

    assert len(ticks) == 1
    assert ticks[0].simtrade is True


def test_replay_preserves_ticks_and_supports_speed(mocker) -> None:
    gateway = _gateway()
    received: list[RawTick] = []
    gateway.subscribe_ticks(TMF.symbol, received.append)
    sleep = mocker.patch("microtx.broker.paper_gateway.time.sleep")
    start = datetime(2026, 1, 5, 8, 45, tzinfo=timezone.utc)
    ticks = [
        RawTick(TMF.symbol, start, 23_000.0, 1, 1, 1, False),
        RawTick(TMF.symbol, start + timedelta(seconds=2), 23_001.0, 1, 2, 1, False),
    ]

    gateway.replay(ticks, speed=2.0)

    assert received == ticks
    sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slippage_ticks": -1},
        {"fill_delay_sec": -1.0},
        {"reject_rate": -0.1},
        {"reject_rate": 1.1},
        {"max_fill_quantity_per_tick": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PaperGateway(spec=TMF, **kwargs)


def test_invalid_price_limits_and_replay_speed_are_rejected() -> None:
    gateway = _gateway()
    with pytest.raises(ValueError):
        gateway.set_price_limits(23_100.0, 23_000.0)
    with pytest.raises(ValueError):
        gateway.replay([], speed=-1.0)
