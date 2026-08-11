"""EmergencyCloser 的安全不變式與二十項邊界情境測試。"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Event, RLock, Thread

import pytest

from microtx.broker.base import OrderRequest
from microtx.broker.paper_gateway import PaperGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.engine.emergency import EmergencyCloser
from microtx.engine.order_router import OrderRouter
from microtx.engine.position import PositionSnapshot, PositionTracker
from microtx.engine.risk import RiskContext, RiskManager
from microtx.enums import (
    CloseMode,
    Direction,
    EngineState,
    NotifyLevel,
    OrderIntent,
    PriceType,
    SessionType,
    TimeInForce,
)


class _RecorderNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[NotifyLevel, str, str]] = []
        self._fail = fail

    def notify(self, level: NotifyLevel, title: str, body: str) -> None:
        self.messages.append((level, title, body))
        if self._fail:
            raise RuntimeError("通知故障")


class _BrokenReconnectGateway(PaperGateway):
    def __init__(self) -> None:
        super().__init__(spec=TMF)
        self.connect_attempts = 0

    def connect(self) -> None:
        self.connect_attempts += 1
        raise RuntimeError("連線失敗")


class _BrokenCancelGateway(PaperGateway):
    def cancel_all_orders(self) -> int:
        raise RuntimeError("撤單故障")


class _BrokenLimitsGateway(PaperGateway):
    def get_price_limits(self, symbol: str) -> tuple[float, float]:
        del symbol
        raise RuntimeError("無漲跌停資料")


def _settings(**updates: object) -> Settings:
    return Settings(
        emergency_max_retries=updates.pop("emergency_max_retries", 2),
        emergency_retry_interval_sec=0.1,
        **updates,
    )


def _seed_position(
    gateway: PaperGateway, direction: Direction = Direction.LONG, quantity: int = 1
) -> None:
    for index in range(quantity):
        request = OrderRequest(
            TMF.symbol,
            direction,
            1,
            None,
            PriceType.MKP,
            TimeInForce.IOC,
            OrderIntent.ENTRY,
            f"seed-{direction.value}-{index}",
        )
        assert gateway.place_order(request).accepted


def _closer(
    gateway: PaperGateway,
    *,
    settings: Settings | None = None,
    tracker: PositionTracker | None = None,
    state_changes: list[EngineState] | None = None,
    cancelled_reasons: list[str] | None = None,
    is_tradable: Callable[[], bool] = lambda: True,
    notifier: _RecorderNotifier | None = None,
    lock: RLock | None = None,
) -> EmergencyCloser:
    shared_lock = lock or RLock()
    config = settings or _settings()
    return EmergencyCloser(
        gateway,
        OrderRouter(gateway, risk=RiskManager(config), lock=shared_lock),
        tracker or PositionTracker(TMF),
        config,
        lock=shared_lock,
        on_state_change=(state_changes if state_changes is not None else []).append,
        on_cancel_strategies=(cancelled_reasons if cancelled_reasons is not None else []).append,
        is_tradable=is_tradable,
        notifier=notifier,
    )


def test_panic_closes_long_with_unchecked_ioc_mkp(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    submitted: list[OrderRequest] = []
    original = gateway.place_order

    def record(request: OrderRequest):
        submitted.append(request)
        return original(request)

    mocker.patch.object(gateway, "place_order", side_effect=record)
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    close = submitted[-1]
    assert (close.action, close.price_type, close.time_in_force) == (
        Direction.SHORT,
        PriceType.MKP,
        TimeInForce.IOC,
    )
    assert gateway.list_positions() == []
    assert report.succeeded is True


def test_cancel_all_strictly_precedes_first_close_order(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    calls: list[str] = []
    original_cancel = gateway.cancel_all_orders
    original_place = gateway.place_order
    mocker.patch.object(
        gateway,
        "cancel_all_orders",
        side_effect=lambda: (calls.append("cancel"), original_cancel())[1],
    )
    mocker.patch.object(
        gateway,
        "place_order",
        side_effect=lambda request: (calls.append("place"), original_place(request))[1],
    )
    mocker.patch("microtx.engine.emergency.time.sleep")

    _closer(gateway).execute(CloseMode.PANIC, "test")

    # 必須先撤單再平倉，否則殘餘進場單可能在平倉後成交並形成反向部位。
    assert calls.index("cancel") < calls.index("place")


def test_emergency_bypasses_all_risk_rejections(mocker) -> None:
    settings = _settings(max_daily_loss=1.0, max_daily_trades=1, order_cooldown_sec=300.0)
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    risk_spy = mocker.spy(RiskManager, "check")
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, settings=settings).execute(CloseMode.PANIC, "test")

    # 緊急單必須完全繞過風控；即使虧損、次數與 cooldown 同時超限也不可被擋下。
    assert report.succeeded is True
    risk_spy.assert_not_called()


def test_gateway_positions_override_empty_tracker(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway, quantity=2)
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, tracker=PositionTracker(TMF)).execute(CloseMode.PANIC, "test")

    # kill switch 不能相信可能已損壞的內部帳本，券商回報 2 口就必須真的平 2 口。
    assert report.positions_before[0].quantity == 2
    assert any("口數不一致" in note for note in report.notes)
    assert report.succeeded is True


def test_price_lock_retries_only_to_configured_limit(mocker, caplog) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    gateway.set_price_limits(24_000.0, 25_000.0)
    place = mocker.spy(gateway, "place_order")
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, settings=_settings(emergency_max_retries=3)).execute(
        CloseMode.PANIC, "test"
    )

    assert place.call_count == 3
    assert report.succeeded is False
    assert report.residual_positions
    assert "CRITICAL" in caplog.text or "未完成" in caplog.text


def test_partial_fill_resubmits_only_residual_quantity(mocker) -> None:
    gateway = PaperGateway(spec=TMF, max_fill_quantity_per_tick=1)
    gateway.connect()
    _seed_position(gateway, quantity=2)
    submitted: list[OrderRequest] = []
    original = gateway.place_order
    mocker.patch.object(
        gateway,
        "place_order",
        side_effect=lambda request: (submitted.append(request), original(request))[1],
    )
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    assert [request.quantity for request in submitted] == [2, 1]
    assert report.succeeded is True


def test_reentrant_call_returns_without_second_order(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    entered = Event()
    release = Event()

    def blocked_sleep(seconds: float) -> None:
        del seconds
        entered.set()
        release.wait(1.0)

    mocker.patch("microtx.engine.emergency.time.sleep", side_effect=blocked_sleep)
    closer = _closer(gateway)
    reports: list[object] = []
    thread = Thread(target=lambda: reports.append(closer.execute(CloseMode.PANIC, "first")))
    thread.start()
    assert entered.wait(1.0)
    assert closer.is_closing is True

    duplicate = closer.execute(CloseMode.PANIC, "second")
    release.set()
    thread.join(1.0)

    assert duplicate.succeeded is False
    assert "執行中" in duplicate.notes[0]


def test_disconnected_gateway_reconnects_then_closes(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    gateway.force_disconnect()
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    assert report.succeeded is True
    assert gateway.is_connected is True


def test_single_order_failure_is_recorded_and_retried(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    closer = _closer(gateway, settings=_settings(emergency_max_retries=1))
    mocker.patch.object(closer._router, "submit_unchecked", side_effect=RuntimeError("拒單"))
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = closer.execute(CloseMode.PANIC, "test")

    assert report.succeeded is False
    assert "送單失敗" in report.notes[-1]


def test_flat_account_succeeds_without_order(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    place = mocker.spy(gateway, "place_order")

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    assert report.positions_before == ()
    assert report.succeeded is True
    place.assert_not_called()


def test_disconnected_gateway_retries_three_times_without_raising(caplog) -> None:
    gateway = _BrokenReconnectGateway()

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    assert gateway.connect_attempts == 3
    assert report.succeeded is False
    assert "手動平倉" in report.notes[0]
    assert "手動平倉" in caplog.text


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(CloseMode.PANIC, EngineState.HALTED), (CloseMode.FLATTEN, EngineState.RUNNING)],
)
def test_both_modes_abort_strategies_but_finish_in_different_states(
    mode: CloseMode, expected: EngineState
) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    states: list[EngineState] = []
    reasons: list[str] = []

    _closer(gateway, state_changes=states, cancelled_reasons=reasons).execute(mode, "manual")

    assert reasons == ["manual"]
    assert states[-1] is expected


def test_internal_exception_never_escapes(caplog) -> None:
    gateway = _BrokenCancelGateway(spec=TMF)
    gateway.connect()

    report = _closer(gateway).execute(CloseMode.PANIC, "test")

    assert report.succeeded is False
    assert "內部失敗" in report.notes[0]
    assert "內部失敗" in caplog.text


def test_notifier_failure_does_not_change_success(mocker, caplog) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, notifier=_RecorderNotifier(fail=True)).execute(
        CloseMode.PANIC, "test"
    )

    assert report.succeeded is True
    assert "通知失敗" in caplog.text


def test_tradable_check_failure_fails_open_and_closes(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    mocker.patch("microtx.engine.emergency.time.sleep")

    def broken_check() -> bool:
        raise RuntimeError("排程器故障")

    report = _closer(gateway, is_tradable=broken_check).execute(CloseMode.PANIC, "test")
    assert report.succeeded is True


@pytest.mark.parametrize(
    ("position_direction", "action", "expected_price"),
    [
        (Direction.LONG, Direction.SHORT, 22_000.0),
        (Direction.SHORT, Direction.LONG, 24_000.0),
    ],
)
def test_limit_mode_uses_extreme_price_limit(
    mocker,
    position_direction: Direction,
    action: Direction,
    expected_price: float,
) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway, position_direction)
    gateway.set_price_limits(22_000.0, 24_000.0)
    submitted: list[OrderRequest] = []
    original = gateway.place_order
    mocker.patch.object(
        gateway,
        "place_order",
        side_effect=lambda request: (submitted.append(request), original(request))[1],
    )
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, settings=_settings(emergency_use_market_order=False)).execute(
        CloseMode.PANIC, "test"
    )

    assert report.succeeded is True
    assert (submitted[0].action, submitted[0].price_type, submitted[0].price) == (
        action,
        PriceType.LMT,
        expected_price,
    )


def test_missing_limits_falls_back_to_mkp(mocker, caplog) -> None:
    gateway = _BrokenLimitsGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    submitted: list[OrderRequest] = []
    original = gateway.place_order
    mocker.patch.object(
        gateway,
        "place_order",
        side_effect=lambda request: (submitted.append(request), original(request))[1],
    )
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway, settings=_settings(emergency_use_market_order=False)).execute(
        CloseMode.PANIC, "test"
    )

    assert report.succeeded is True
    assert submitted[0].price_type is PriceType.MKP
    assert "降級使用 MKP" in caplog.text


def test_closed_session_halts_aborts_sets_pending_and_warns() -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    states: list[EngineState] = []
    reasons: list[str] = []
    notifier = _RecorderNotifier()
    closer = _closer(
        gateway,
        state_changes=states,
        cancelled_reasons=reasons,
        is_tradable=lambda: False,
        notifier=notifier,
    )

    report = closer.execute(CloseMode.PANIC, "night")

    assert states == [EngineState.HALTED]
    assert reasons == ["night"]
    assert closer.pending is CloseMode.PANIC
    assert report.succeeded is False
    assert notifier.messages[0][0] is NotifyLevel.WARNING


def test_held_shared_lock_times_out_and_closes_without_lock(mocker, caplog) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    shared_lock = RLock()
    lock_held = Event()
    release = Event()

    def hold_lock_during_network_stall() -> None:
        with shared_lock:
            lock_held.set()
            release.wait(2.0)

    holder = Thread(target=hold_lock_during_network_stall)
    holder.start()
    assert lock_held.wait(1.0)
    mocker.patch("microtx.engine.emergency.time.sleep")
    closer = _closer(
        gateway,
        settings=_settings(emergency_lock_timeout_sec=0.1),
        lock=shared_lock,
    )

    started = time.monotonic()
    report = closer.execute(CloseMode.PANIC, "lock_stall")
    elapsed = time.monotonic() - started
    release.set()
    holder.join(1.0)

    assert elapsed < 1.1
    assert report.succeeded is True
    assert gateway.list_positions() == []
    assert "未取得共用鎖，以無鎖模式執行" in report.notes
    assert "無鎖模式強制繼續平倉" in caplog.text


def test_stalled_normal_place_order_cannot_block_emergency_close(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    shared_lock = RLock()
    settings = _settings(emergency_lock_timeout_sec=0.1)
    router = OrderRouter(gateway, risk=RiskManager(settings), lock=shared_lock)
    closer = EmergencyCloser(
        gateway,
        router,
        PositionTracker(TMF),
        settings,
        lock=shared_lock,
        on_state_change=lambda state: None,
        on_cancel_strategies=lambda reason: None,
        is_tradable=lambda: True,
    )
    network_entered = Event()
    release_network = Event()
    original_place = gateway.place_order

    def stalled_place(request: OrderRequest):
        if request.intent is OrderIntent.ENTRY:
            network_entered.set()
            release_network.wait(2.0)
        return original_place(request)

    mocker.patch.object(gateway, "place_order", side_effect=stalled_place)
    mocker.patch("microtx.engine.emergency.time.sleep")
    normal_request = OrderRequest(
        TMF.symbol,
        Direction.LONG,
        1,
        None,
        PriceType.MKP,
        TimeInForce.IOC,
        OrderIntent.ENTRY,
        "stalled-entry",
    )
    context = RiskContext(
        datetime.now(),
        SessionType.DAY,
        EngineState.RUNNING,
        PositionSnapshot(None, 0, 0.0, 0.0, 0.0),
        0.0,
        0.0,
        0,
        None,
        None,
    )
    normal = Thread(target=lambda: router.submit(normal_request, context))
    normal.start()
    assert network_entered.wait(1.0)

    started = time.monotonic()
    report = closer.execute(CloseMode.PANIC, "network_stall")
    elapsed = time.monotonic() - started
    release_network.set()
    normal.join(1.0)

    assert elapsed < 1.1
    assert report.succeeded is True
    assert gateway.list_positions() == []


def test_available_shared_lock_does_not_add_unlocked_note(mocker) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    mocker.patch("microtx.engine.emergency.time.sleep")

    report = _closer(gateway).execute(CloseMode.PANIC, "normal")

    assert "未取得共用鎖，以無鎖模式執行" not in report.notes
