"""Shioaji 列舉、取消原因與 Tick 欄位搬運的免連線測試。"""

from __future__ import annotations

import importlib
from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from microtx.broker import _mapping as mapping
from microtx.broker._mapping import (
    from_shioaji_action,
    from_shioaji_octype,
    from_shioaji_price_type,
    from_shioaji_time_in_force,
    infer_cancel_reason,
    to_shioaji_action,
    to_shioaji_octype,
    to_shioaji_price_type,
    to_shioaji_time_in_force,
)
from microtx.broker.base import AckEvent, CancelEvent, FillEvent, OrderRequest, RejectEvent
from microtx.broker.shioaji_gateway import ShioajiGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.enums import Direction, OrderIntent, PriceType, TimeInForce
from microtx.exceptions import BrokerError, ConnectionLostError


def _sdk():
    return pytest.importorskip("shioaji", reason="未安裝 live extra")


def test_action_mapping_is_bidirectional() -> None:
    sj = _sdk()
    for local, sdk in ((Direction.LONG, sj.Action.Buy), (Direction.SHORT, sj.Action.Sell)):
        assert to_shioaji_action(local) == sdk
        assert from_shioaji_action(sdk) is local


def test_price_type_mapping_is_bidirectional() -> None:
    sj = _sdk()
    pairs = (
        (PriceType.LMT, sj.FuturesPriceType.LMT),
        (PriceType.MKP, sj.FuturesPriceType.MKP),
        (PriceType.MKT, sj.FuturesPriceType.MKT),
    )
    for local, sdk in pairs:
        assert to_shioaji_price_type(local) == sdk
        assert from_shioaji_price_type(sdk) is local


def test_time_in_force_mapping_is_bidirectional() -> None:
    sj = _sdk()
    pairs = (
        (TimeInForce.ROD, sj.OrderType.ROD),
        (TimeInForce.IOC, sj.OrderType.IOC),
        (TimeInForce.FOK, sj.OrderType.FOK),
    )
    for local, sdk in pairs:
        assert to_shioaji_time_in_force(local) == sdk
        assert from_shioaji_time_in_force(sdk) is local


def test_octype_mapping_is_bidirectional() -> None:
    sj = _sdk()
    for configured, sdk in (
        ("Auto", sj.FuturesOCType.Auto),
        ("DayTrade", sj.FuturesOCType.DayTrade),
    ):
        assert to_shioaji_octype(configured) == sdk
        assert from_shioaji_octype(sdk) == configured


@pytest.mark.parametrize(
    "converter",
    [
        to_shioaji_action,
        from_shioaji_action,
        to_shioaji_price_type,
        from_shioaji_price_type,
        to_shioaji_time_in_force,
        from_shioaji_time_in_force,
        to_shioaji_octype,
        from_shioaji_octype,
    ],
)
def test_unknown_mapping_value_raises(converter) -> None:
    _sdk()
    with pytest.raises(ValueError):
        converter("UNKNOWN")


def _request(time_in_force: TimeInForce) -> OrderRequest:
    return OrderRequest(
        TMF.symbol,
        Direction.LONG,
        1,
        None,
        PriceType.MKP,
        time_in_force,
        OrderIntent.ENTRY,
        "client-1",
    )


@pytest.mark.parametrize(
    ("pending", "time_in_force", "expected"),
    [
        ({"client-1"}, TimeInForce.ROD, "user"),
        (set(), TimeInForce.IOC, "ioc_expired"),
        (set(), TimeInForce.FOK, "fok_expired"),
        (set(), TimeInForce.ROD, "session_end"),
    ],
)
def test_cancel_reason_is_project_inference(
    pending: set[str], time_in_force: TimeInForce, expected: str
) -> None:
    assert infer_cancel_reason("client-1", pending, _request(time_in_force)) == expected
    assert infer_cancel_reason(None, pending, None) == ""


def test_missing_sdk_keeps_pure_inference_collectable(monkeypatch) -> None:
    def missing(name: str):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(mapping, "import_module", missing)

    assert infer_cancel_reason("client-1", set(), _request(TimeInForce.IOC)) == "ioc_expired"
    with pytest.raises(BrokerError, match="pip install -e") as captured:
        to_shioaji_action(Direction.LONG)
    assert '".[live]"' in str(captured.value)


def test_missing_sdk_gateway_error_is_actionable(monkeypatch) -> None:
    def missing(name: str):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(mapping, "import_module", missing)

    with pytest.raises(BrokerError, match="pip install -e") as captured:
        ShioajiGateway(Settings())
    assert '".[live]"' in str(captured.value)
    assert "PaperGateway" in str(captured.value)


def test_missing_sdk_does_not_break_other_layer_imports(monkeypatch) -> None:
    def missing(name: str):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(mapping, "import_module", missing)
    modules = (
        "microtx",
        "microtx.config",
        "microtx.enums",
        "microtx.contracts",
        "microtx.broker.base",
        "microtx.broker.paper_gateway",
        "microtx.market.feed",
        "microtx.strategies.scalp",
        "microtx.engine.emergency",
        "microtx.engine.engine",
        "microtx.broker._mapping",
        "microtx.broker.shioaji_gateway",
    )

    for module_name in modules:
        assert importlib.import_module(module_name) is not None


def test_raw_tick_moves_fields_and_decimal_is_exact_for_integer_point() -> None:
    FakeTick = namedtuple("FakeTick", "code datetime close volume total_volume tick_type simtrade")
    timestamp = datetime(2026, 8, 10, 9, 0)
    raw = ShioajiGateway.raw_tick_from_sdk(
        FakeTick("TMFF6", timestamp, Decimal("23150"), 2, 100, 1, True)
    )

    assert raw.code == "TMFF6"
    assert raw.timestamp == timestamp.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    assert raw.price == 23_150.0
    assert raw.volume == 2
    assert raw.total_volume == 100
    assert raw.tick_type == 1
    assert raw.simtrade is True


def test_futures_octype_setting_accepts_only_formal_values() -> None:
    assert Settings(futures_octype="Auto").futures_octype == "Auto"
    assert Settings(futures_octype="DayTrade").futures_octype == "DayTrade"
    with pytest.raises(ValueError):
        Settings(futures_octype="Cover")


def _fake_api(mocker):
    api = mocker.Mock()
    api.futopt_account = SimpleNamespace(signed=True)
    api.contracts.get.return_value = SimpleNamespace(code=TMF.symbol)
    api.contracts.info.return_value = SimpleNamespace(limit_down=20_000, limit_up=25_000)
    return api


def _gateway(mocker, **settings_updates: object):
    sj = _sdk()
    api = _fake_api(mocker)
    mocker.patch.object(sj, "Shioaji", return_value=api)
    gateway = ShioajiGateway(Settings(**settings_updates))
    return gateway, api


def _trade(
    *,
    status: str = "Submitted",
    broker_order_id: str = "broker-1",
    deal_quantity: int = 0,
):
    return SimpleNamespace(
        status=SimpleNamespace(
            id=broker_order_id,
            status=status,
            msg="",
            order_quantity=2,
            deal_quantity=deal_quantity,
            cancel_quantity=0,
        ),
        order=SimpleNamespace(action=_sdk().Action.Buy, price=23_000, quantity=2),
        contract=SimpleNamespace(code="TMFF6"),
    )


def test_gateway_connect_registers_callbacks_and_logout(mocker) -> None:
    gateway, api = _gateway(mocker)
    mocker.patch.object(gateway, "_start_dispatcher")
    mocker.patch.object(gateway, "_stop_dispatcher")

    gateway.connect()

    assert gateway.is_connected is True
    api.login.assert_called_once()
    api.quote.set_on_tick_fop_v1_callback.assert_called_once()
    api.set_order_callback.assert_called_once()
    api.quote.set_event_callback.assert_called_once()
    gateway.disconnect()
    api.logout.assert_called_once()
    assert gateway.is_connected is False


def test_connect_rejects_unsigned_account_and_hides_sdk_error(mocker) -> None:
    gateway, api = _gateway(mocker)
    api.futopt_account.signed = False
    with pytest.raises(BrokerError, match="尚未完成"):
        gateway.connect()

    api.futopt_account.signed = True
    api.login.side_effect = RuntimeError("contains secret")
    with pytest.raises(BrokerError, match="登入失敗") as captured:
        gateway.connect()
    assert "contains secret" not in str(captured.value)


def test_live_connect_activates_ca_without_storing_plain_secrets(mocker, tmp_path) -> None:
    certificate = tmp_path / "certificate.pfx"
    certificate.write_bytes(b"fake")
    gateway, api = _gateway(
        mocker,
        simulation=False,
        allow_live_trading=True,
        shioaji_api_key="key",
        shioaji_secret_key="secret",
        shioaji_ca_path=certificate,
        shioaji_ca_password="password",
        shioaji_person_id="person",
    )
    mocker.patch.object(gateway, "_start_dispatcher")
    gateway.connect()
    api.activate_ca.assert_called_once()
    assert not hasattr(gateway, "_api_key")


def test_place_order_maps_fields_and_caches_trade(mocker) -> None:
    gateway, api = _gateway(mocker)
    gateway._connected = True
    trade = _trade()
    api.place_order.return_value = trade
    futures_order = mocker.patch.object(gateway._sj, "FuturesOrder")
    request = OrderRequest(
        TMF.symbol,
        Direction.LONG,
        2,
        23_000,
        PriceType.LMT,
        TimeInForce.ROD,
        OrderIntent.ENTRY,
        "client-1",
    )

    ack = gateway.place_order(request)

    assert ack.accepted is True
    assert ack.broker_order_id == "broker-1"
    assert gateway._client_id_map == {"client-1": "broker-1"}
    futures_order.assert_called_once()
    api.contracts.get.assert_called_once_with(TMF.symbol)
    assert gateway.get_price_limits(TMF.symbol) == (20_000.0, 25_000.0)
    assert api.contracts.get.call_count == 1


def test_place_order_rejects_disconnected_and_out_of_limits(mocker) -> None:
    gateway, _ = _gateway(mocker)
    request = OrderRequest(
        TMF.symbol,
        Direction.LONG,
        1,
        30_000,
        PriceType.LMT,
        TimeInForce.ROD,
        OrderIntent.ENTRY,
        "client-1",
    )
    with pytest.raises(ConnectionLostError):
        gateway.place_order(request)
    gateway._connected = True
    with pytest.raises(BrokerError, match="超出漲跌停"):
        gateway.place_order(request)


def test_positions_and_open_orders_are_mapped_one_to_one(mocker) -> None:
    gateway, api = _gateway(mocker)
    api.list_positions.return_value = [
        SimpleNamespace(
            code="TMFF6",
            direction=_sdk().Action.Sell,
            quantity=2,
            price=23_100,
            pnl=-200,
        )
    ]
    trade = _trade(deal_quantity=1)
    api.list_trades.return_value = [trade, _trade(status="Filled", broker_order_id="done")]
    gateway._client_id_map["client-1"] = "broker-1"

    position = gateway.list_positions()[0]
    order = gateway.list_open_orders()[0]

    assert (position.direction, position.quantity, position.average_price) == (
        Direction.SHORT,
        2,
        23_100.0,
    )
    assert (order.client_id, order.filled_quantity) == ("client-1", 1)
    api.update_status.assert_called_once()


def test_single_and_bulk_cancel_update_status_and_continue_on_failure(mocker) -> None:
    gateway, api = _gateway(mocker)
    first = _trade(broker_order_id="one")
    second = _trade(broker_order_id="two")
    gateway._trade_cache["one"] = first
    gateway._client_id_map.update({"c1": "one", "c2": "two"})
    assert gateway.cancel_order("one") is True
    assert "c1" in gateway._pending_cancels
    api.list_trades.return_value = []
    assert gateway.cancel_order("missing") is False

    api.list_trades.return_value = [first, second, _trade(status="Filled")]
    api.cancel_order.side_effect = [RuntimeError("one failed"), None]
    assert gateway.cancel_all_orders() == 1
    assert "c1" not in gateway._pending_cancels
    assert "c2" in gateway._pending_cancels


def test_tick_subscription_and_sdk_tick_callback(mocker) -> None:
    gateway, api = _gateway(mocker)
    gateway._connected = True
    received = []
    gateway.subscribe_ticks(TMF.symbol, received.append)
    FakeTick = namedtuple("FakeTick", "code datetime close volume total_volume tick_type simtrade")
    gateway._on_tick(
        object(), FakeTick("TMFF6", datetime(2026, 8, 10, 9), Decimal("23000"), 1, 9, 0, False)
    )
    assert received[0].code == "TMFF6"
    gateway.unsubscribe_ticks(TMF.symbol)
    api.quote.unsubscribe.assert_called_once()


def _order_message(op_type: str = "New", op_code: str = "00") -> dict[str, object]:
    return {
        "operation": {"op_type": op_type, "op_code": op_code, "op_msg": "rejected"},
        "order": {
            "id": "broker-1",
            "ordno": "P001",
            "action": _sdk().Action.Buy,
            "price": 23_000,
            "quantity": 2,
        },
        "status": {
            "exchange_ts": datetime(2026, 8, 10, 9),
            "cancel_quantity": 1,
            "order_quantity": 2,
            "deal_quantity": 1,
        },
        "contract": {"code": "TMFF6"},
    }


def test_order_callbacks_convert_fill_ack_reject_and_cancel(mocker) -> None:
    gateway, _ = _gateway(mocker)
    gateway._client_id_map["client-1"] = "broker-1"
    gateway._request_map["client-1"] = _request(TimeInForce.IOC)
    deal = {
        "trade_id": "broker-1",
        "code": "TMFF6",
        "action": _sdk().Action.Buy,
        "price": 23_000,
        "quantity": 1,
        "ts": datetime(2026, 8, 10, 9),
    }

    fill = gateway._convert_order_event(_sdk().OrderState.FuturesDeal, deal)
    ack = gateway._convert_order_event(_sdk().OrderState.FuturesOrder, _order_message())
    reject = gateway._convert_order_event(
        _sdk().OrderState.FuturesOrder, _order_message(op_code="E01")
    )
    cancel = gateway._convert_order_event(
        _sdk().OrderState.FuturesOrder, _order_message(op_type="Cancel")
    )

    assert isinstance(fill, FillEvent)
    assert isinstance(ack, AckEvent) and ack.exchange_order_no == "P001"
    assert isinstance(reject, RejectEvent) and reject.message == "rejected"
    assert isinstance(cancel, CancelEvent)
    assert (cancel.cancelled_quantity, cancel.remaining_quantity, cancel.reason) == (
        1,
        0,
        "ioc_expired",
    )


def test_event_codes_update_connection_and_queue_resubscribe(mocker) -> None:
    gateway, _ = _gateway(mocker)
    gateway._event_callback(0, 0, "", "")
    assert gateway.is_connected is True
    for code in (1, 2, 12):
        gateway._event_callback(0, code, "", "")
        assert gateway.is_connected is False
    gateway._event_callback(0, 13, "", "")
    assert gateway.is_connected is True
    assert gateway._dispatch_queue.get_nowait() == 13
    gateway._event_callback(0, 17, "", "")
    assert gateway._dispatch_queue.get_nowait() == 17
    gateway._event_callback(0, 16, "", "")


def test_dispatcher_delivers_order_event_and_resubscribes(mocker) -> None:
    gateway, _ = _gateway(mocker)
    delivered = []
    gateway.set_order_event_callback(delivered.append)
    resubscribe = mocker.patch.object(gateway, "_resubscribe_all")
    event = RejectEvent(None, None, "TMFF6", "reject", datetime.now())
    gateway._dispatch_queue.put_nowait(event)
    gateway._dispatch_queue.put_nowait(13)
    gateway._dispatch_queue.put_nowait(None)

    gateway._dispatch_loop()

    assert delivered == [event]
    resubscribe.assert_called_once()


def test_gateway_error_paths_are_wrapped_without_secrets(mocker) -> None:
    gateway, api = _gateway(mocker)
    gateway._connected = True
    api.contracts.get.side_effect = RuntimeError("sdk")
    with pytest.raises(BrokerError, match="商品解析失敗"):
        gateway._resolve_contract("UNKNOWN")
    api.contracts.get.side_effect = None
    api.contracts.get.return_value = None
    with pytest.raises(BrokerError, match="找不到"):
        gateway._resolve_contract("UNKNOWN")

    api.list_positions.side_effect = RuntimeError("secret")
    with pytest.raises(BrokerError, match="查詢期貨部位失敗"):
        gateway.list_positions()
    api.list_trades.side_effect = RuntimeError("secret")
    with pytest.raises(BrokerError, match="查詢未成交委託失敗"):
        gateway.list_open_orders()
    with pytest.raises(BrokerError, match="查詢未成交委託失敗"):
        gateway.cancel_all_orders()


def test_price_limit_ttl_and_subscription_errors(mocker) -> None:
    gateway, api = _gateway(mocker)
    gateway._connected = True
    assert gateway.get_price_limits(TMF.symbol) == (20_000.0, 25_000.0)
    assert gateway.get_price_limits(TMF.symbol) == (20_000.0, 25_000.0)
    assert api.contracts.info.call_count == 1
    gateway._info_cache.clear()
    api.contracts.info.side_effect = RuntimeError("failed")
    with pytest.raises(BrokerError, match="查詢漲跌停失敗"):
        gateway.get_price_limits(TMF.symbol)

    api.quote.subscribe.side_effect = RuntimeError("failed")
    with pytest.raises(BrokerError, match="行情訂閱失敗"):
        gateway.subscribe_ticks(TMF.symbol, lambda tick: None)
    api.quote.subscribe.side_effect = None
    api.quote.unsubscribe.side_effect = RuntimeError("failed")
    with pytest.raises(BrokerError, match="取消行情訂閱失敗"):
        gateway.unsubscribe_ticks(TMF.symbol)


def test_place_cancel_and_logout_sdk_failures_are_wrapped(mocker) -> None:
    gateway, api = _gateway(mocker)
    gateway._connected = True
    request = _request(TimeInForce.IOC)
    mocker.patch.object(gateway._sj, "FuturesOrder", return_value=object())
    api.place_order.side_effect = RuntimeError("secret")
    with pytest.raises(BrokerError, match="下單失敗"):
        gateway.place_order(request)

    trade = _trade()
    gateway._trade_cache["broker-1"] = trade
    gateway._client_id_map["client-1"] = "broker-1"
    api.cancel_order.side_effect = RuntimeError("secret")
    with pytest.raises(BrokerError, match="刪單失敗"):
        gateway.cancel_order("broker-1")
    assert "client-1" not in gateway._pending_cancels

    api.logout.side_effect = RuntimeError("secret")
    mocker.patch.object(gateway, "_stop_dispatcher")
    with pytest.raises(BrokerError, match="登出失敗"):
        gateway.disconnect()
    gateway._stop_dispatcher.assert_called_once()


def test_callback_tolerates_early_fill_bad_payload_and_unknown_operations(mocker, caplog) -> None:
    gateway, _ = _gateway(mocker)
    early_deal = {
        "trade_id": "unknown",
        "code": "TMFF6",
        "action": _sdk().Action.Sell,
        "price": 23_000,
        "quantity": 1,
        "ts": 1_786_329_600_000_000_000,
    }
    fill = gateway._convert_order_event(_sdk().OrderState.FuturesDeal, early_deal)
    assert isinstance(fill, FillEvent) and fill.client_id is None
    assert "早於委託對映" in caplog.text
    assert gateway._convert_order_event(object(), {}) is None
    assert (
        gateway._convert_order_event(
            _sdk().OrderState.FuturesOrder, _order_message(op_type="UpdatePrice")
        )
        is None
    )

    gateway._order_callback(_sdk().OrderState.FuturesOrder, {})
    assert "無法轉換" in caplog.text


def test_resubscribe_continues_when_one_symbol_fails(mocker, caplog) -> None:
    gateway, _ = _gateway(mocker)
    gateway._tick_callbacks = {"TMFR1": lambda tick: None, "MXFR1": lambda tick: None}
    subscribe = mocker.patch.object(
        gateway, "_subscribe_symbol", side_effect=[BrokerError("failed"), None]
    )
    gateway._resubscribe_all()
    assert subscribe.call_count == 2
    assert "恢復行情訂閱失敗" in caplog.text
