"""OrderRouter 冪等、重試、撤單補償與端到端安全測試。"""

from __future__ import annotations

from datetime import datetime
from threading import Event, RLock, Thread
from zoneinfo import ZoneInfo

import pytest

from microtx.broker.base import (
    AckEvent,
    BrokerGateway,
    CancelEvent,
    FillEvent,
    OpenOrder,
    OrderAck,
    OrderRequest,
    RejectEvent,
    new_client_id,
)
from microtx.broker.paper_gateway import PaperGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.engine.order_router import OrderRouter
from microtx.engine.position import PositionSnapshot
from microtx.engine.risk import RiskContext, RiskDecision, RiskManager
from microtx.enums import (
    Direction,
    EngineState,
    OrderIntent,
    PriceType,
    SessionType,
    TimeInForce,
)
from microtx.exceptions import BrokerError

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _ctx() -> RiskContext:
    return RiskContext(
        _NOW,
        SessionType.DAY,
        EngineState.RUNNING,
        PositionSnapshot(None, 0, 0.0, 0.0, 0.0),
        0.0,
        0.0,
        0,
        None,
        None,
    )


def _request(
    *,
    intent: OrderIntent = OrderIntent.ENTRY,
    action: Direction = Direction.LONG,
    quantity: int = 1,
    client_id: str | None = None,
    strategy_id: str = "strategy-1",
    price: float | None = None,
    time_in_force: TimeInForce = TimeInForce.IOC,
) -> OrderRequest:
    return OrderRequest(
        TMF.symbol,
        action,
        quantity,
        price,
        PriceType.LMT if price is not None else PriceType.MKP,
        time_in_force,
        intent,
        client_id or new_client_id(),
        strategy_id,
    )


def _mock_router(mocker) -> tuple[OrderRouter, object, object]:
    gateway = mocker.Mock(spec=BrokerGateway)
    gateway.get_price_limits.return_value = (20_000.0, 25_000.0)
    gateway.list_open_orders.return_value = []
    gateway.cancel_order.return_value = True
    gateway.place_order.side_effect = lambda request: OrderAck(
        request.client_id, f"broker-{request.client_id}", True
    )
    risk = mocker.Mock(spec=RiskManager)
    risk.check.return_value = RiskDecision(True, "通過")
    return OrderRouter(gateway, risk=risk, lock=RLock()), gateway, risk


def test_duplicate_client_id_is_submitted_only_once(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    request = _request(client_id="same")
    acknowledgements = [router.submit(request, _ctx()) for _ in range(3)]
    assert [ack.accepted for ack in acknowledgements] == [True, False, False]
    gateway.place_order.assert_called_once()


def test_risk_rejection_does_not_reach_gateway(mocker) -> None:
    router, gateway, risk = _mock_router(mocker)
    risk.check.return_value = RiskDecision(False, "風控拒絕")
    ack = router.submit(_request(), _ctx())
    assert ack.accepted is False
    assert "風控拒絕" in ack.message
    gateway.place_order.assert_not_called()


def test_gateway_order_is_retried_and_last_submit_time_is_recorded(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    mocker.patch("microtx.utils.retry.time.sleep")
    request = _request()
    gateway.place_order.side_effect = [
        BrokerError("暫時失敗"),
        BrokerError("再試"),
        OrderAck(request.client_id, "broker-1", True),
    ]
    assert router.submit(request, _ctx()).accepted is True
    assert gateway.place_order.call_count == 3
    assert router.last_order_at == _NOW


def test_submit_unchecked_never_calls_risk(mocker) -> None:
    router, gateway, risk = _mock_router(mocker)
    request = _request(intent=OrderIntent.EMERGENCY, strategy_id="")
    assert router.submit_unchecked(request).accepted is True
    risk.check.assert_not_called()
    gateway.place_order.assert_called_once()
    assert router.submit_unchecked(request).accepted is False


def test_gateway_place_order_runs_without_router_lock(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    acquired = Event()

    def inspect_lock(request: OrderRequest) -> OrderAck:
        def contender() -> None:
            with router._lock:
                acquired.set()

        thread = Thread(target=contender)
        thread.start()
        assert acquired.wait(0.5)
        thread.join(0.5)
        return OrderAck(request.client_id, "broker-1", True)

    gateway.place_order.side_effect = inspect_lock

    assert router.submit(_request(), _ctx()).accepted is True


def test_broker_rejection_and_exception_clean_in_flight(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    gateway.place_order.side_effect = None
    request = _request(client_id="rejected")
    gateway.place_order.return_value = OrderAck("rejected", None, False, "拒單")
    assert router.submit(request, _ctx()).accepted is False
    assert router.in_flight == {}
    assert router.submit(request, _ctx()).accepted is False

    failing = _request(client_id="failing")
    gateway.place_order.side_effect = BrokerError("斷線")
    mocker.patch("microtx.utils.retry.time.sleep")
    with pytest.raises(BrokerError):
        router.submit(failing, _ctx())
    assert "failing" not in router.in_flight


def test_price_limit_lookup_failure_is_passed_as_none(mocker) -> None:
    router, gateway, risk = _mock_router(mocker)
    gateway.get_price_limits.side_effect = BrokerError("尚未載入")
    router.submit(_request(), _ctx())
    checked_context = risk.check.call_args.args[1]
    assert checked_context.price_limits is None


def test_fill_before_ack_resolves_entry_from_open_orders(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    entry = _request(client_id="entry")
    gateway.place_order.side_effect = None
    gateway.place_order.return_value = OrderAck("entry", None, True)
    router.submit(entry, _ctx())
    gateway.list_open_orders.return_value = [
        OpenOrder("broker-entry", "entry", TMF.symbol, Direction.LONG, 23_000.0, 2, 1)
    ]

    gateway.place_order.return_value = OrderAck("exit", "broker-exit", True)
    exit_request = _request(client_id="exit", intent=OrderIntent.STOP_LOSS, action=Direction.SHORT)
    assert router.submit(exit_request, _ctx()).accepted is True
    gateway.cancel_order.assert_called_with("broker-entry")


@pytest.mark.parametrize("cancel_behavior", [False, BrokerError("撤單失敗")])
def test_cancel_failure_marks_abandoned_but_still_submits_exit(
    mocker, cancel_behavior: object
) -> None:
    router, gateway, _ = _mock_router(mocker)
    mocker.patch("microtx.utils.retry.time.sleep")
    entry = _request(client_id="entry")
    gateway.place_order.return_value = OrderAck("entry", "broker-entry", True)
    router.submit(entry, _ctx())
    if isinstance(cancel_behavior, BaseException):
        gateway.cancel_order.side_effect = cancel_behavior
    else:
        gateway.cancel_order.return_value = cancel_behavior
    gateway.place_order.return_value = OrderAck("exit", "broker-exit", True)

    ack = router.submit(
        _request(client_id="exit", intent=OrderIntent.STOP_LOSS, action=Direction.SHORT),
        _ctx(),
    )

    assert ack.accepted is True
    assert "entry" in router._abandoned_entries


def test_missing_order_number_marks_abandoned_and_still_submits(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    entry = _request(client_id="entry")
    gateway.place_order.side_effect = None
    gateway.place_order.return_value = OrderAck("entry", None, True)
    router.submit(entry, _ctx())
    gateway.list_open_orders.return_value = []
    gateway.place_order.return_value = OrderAck("exit", "broker-exit", True)
    ack = router.submit(
        _request(client_id="exit", intent=OrderIntent.FORCE_CLOSE, action=Direction.SHORT),
        _ctx(),
    )
    assert ack.accepted is True
    assert "entry" in router._abandoned_entries


def test_abandoned_fill_is_compensated_with_unchecked_emergency(mocker, caplog) -> None:
    router, gateway, risk = _mock_router(mocker)
    entry = _request(client_id="entry", quantity=2)
    router._abandoned_entries["entry"] = entry
    router._abandoned_filled["entry"] = 1

    router.on_event(
        FillEvent("entry", "broker-entry", TMF.symbol, Direction.LONG, 23_000.0, 1, _NOW)
    )

    compensation = gateway.place_order.call_args.args[0]
    assert compensation.intent is OrderIntent.EMERGENCY
    assert compensation.action is Direction.SHORT
    assert compensation.quantity == 1
    risk.check.assert_not_called()
    assert "立即反向平倉" in caplog.text


def test_late_ack_retries_abandoned_cancel_and_removes_it(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    entry = _request(client_id="entry")
    router._abandoned_entries["entry"] = entry
    gateway.cancel_order.return_value = True

    router.on_event(
        AckEvent("entry", "broker-entry", "P001", TMF.symbol, Direction.LONG, 23_000.0, 1, _NOW)
    )

    gateway.cancel_order.assert_called_with("broker-entry")
    assert "entry" not in router._abandoned_entries


def test_reject_and_final_cancel_events_complete_orders(mocker) -> None:
    router, _, _ = _mock_router(mocker)
    first = _request(client_id="first")
    second = _request(client_id="second")
    router.submit(first, _ctx())
    router.submit(second, _ctx())
    router.on_event(RejectEvent("first", "b1", "E", "拒單", _NOW))
    router.on_event(CancelEvent("second", "b2", TMF.symbol, 1, 0, _NOW, "user"))
    assert router.in_flight == {}


def test_partial_fill_stop_then_residual_fill_ends_flat(mocker) -> None:
    gateway = PaperGateway(spec=TMF, initial_price=23_000.0, max_fill_quantity_per_tick=1)
    gateway.connect()
    risk = RiskManager(Settings(_env_file=None, order_cooldown_sec=0))
    router = OrderRouter(gateway, risk=risk, lock=RLock())
    gateway.set_order_event_callback(router.on_event)

    entry = _request(
        client_id="entry",
        quantity=2,
        price=23_000.0,
        time_in_force=TimeInForce.ROD,
    )
    assert router.submit(entry, _ctx()).accepted is True
    assert gateway.list_positions()[0].quantity == 1

    cancel_mock = mocker.patch.object(gateway, "cancel_order", return_value=False)
    stop = _request(
        client_id="stop",
        intent=OrderIntent.STOP_LOSS,
        action=Direction.SHORT,
        quantity=1,
    )
    assert router.submit(stop, _ctx()).accepted is True
    mocker.stop(cancel_mock)
    assert gateway.list_positions() == []

    gateway.feed_tick(23_000.0)

    # 撤不掉的殘餘進場成交後必須立即抵銷；最終不得留下反向部位。
    assert gateway.list_positions() == []


def test_cancel_and_cancel_all_delegate_under_router(mocker) -> None:
    router, gateway, _ = _mock_router(mocker)
    gateway.cancel_order.return_value = True
    gateway.cancel_all_orders.return_value = 2
    assert router.cancel("broker-1") is True
    assert router.cancel_all() == 2
