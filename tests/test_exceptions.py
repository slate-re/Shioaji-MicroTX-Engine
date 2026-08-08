"""例外體系與券商基礎契約測試。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import NoReturn

import pytest

from microtx.broker.base import (
    AckEvent,
    CancelEvent,
    FillEvent,
    OrderEvent,
    OrderRequest,
    Position,
    RejectEvent,
    new_client_id,
)
from microtx.enums import Direction, OrderIntent, PriceType, TimeInForce
from microtx.exceptions import (
    BrokerError,
    ConfigError,
    ConnectionLostError,
    EmergencyCloseError,
    MicroTXError,
    OrderRejectedError,
    RiskViolationError,
    StrategyError,
)


def _request(**overrides: object) -> OrderRequest:
    values = {
        "symbol": "TMFR1",
        "action": Direction.LONG,
        "quantity": 1,
        "price": 23_000.0,
        "price_type": PriceType.LMT,
        "time_in_force": TimeInForce.ROD,
        "intent": OrderIntent.ENTRY,
        "client_id": new_client_id(),
    }
    values.update(overrides)
    return OrderRequest(**values)


def _unreachable(event: NoReturn) -> NoReturn:
    raise AssertionError(f"未處理的事件：{event!r}")


def _event_name(event: OrderEvent) -> str:
    """以型別窮盡分派四種委託事件。"""
    match event:
        case FillEvent():
            return "fill"
        case RejectEvent():
            return "reject"
        case AckEvent():
            return "ack"
        case CancelEvent():
            return "cancel"
    return _unreachable(event)


class TestExceptionHierarchy:
    """專案例外必須能由共同根類別一致捕捉。"""

    @pytest.mark.parametrize(
        "error",
        [
            ConfigError("設定錯誤"),
            BrokerError("券商錯誤"),
            ConnectionLostError("連線中斷"),
            RiskViolationError("風控拒絕"),
            StrategyError("策略錯誤"),
            EmergencyCloseError("緊急平倉失敗"),
        ],
    )
    def test_project_errors_share_root(self, error: MicroTXError) -> None:
        assert isinstance(error, MicroTXError)

    def test_connection_lost_is_broker_error(self) -> None:
        assert isinstance(ConnectionLostError("連線中斷"), BrokerError)

    def test_order_rejected_diagnostics(self) -> None:
        error = OrderRejectedError("委託遭拒", code="E101", client_id="cid-001")

        assert error.code == "E101"
        assert error.client_id == "cid-001"
        assert "E101" in str(error)
        assert "cid-001" in str(error)

    def test_exception_text_does_not_add_secrets(self) -> None:
        error = OrderRejectedError("委託遭拒", code="E101", client_id="cid-001")
        text = str(error).lower()

        assert "api_key" not in text
        assert "password" not in text
        assert "account" not in text

    def test_emergency_close_diagnostics(self) -> None:
        error = EmergencyCloseError(
            "仍有部位",
            mode="PANIC",
            source="SIGUSR1",
            residual_quantity=2,
        )

        assert error.mode == "PANIC"
        assert error.source == "SIGUSR1"
        assert error.residual_quantity == 2
        assert "mode=PANIC" in str(error)
        assert "source=SIGUSR1" in str(error)
        assert "residual_quantity=2" in str(error)


class TestBrokerContracts:
    """券商資料契約的基本驗證。"""

    def test_client_id_is_uuid_prefix(self) -> None:
        first = new_client_id()
        second = new_client_id()

        assert len(first) == 16
        assert int(first, 16) >= 0
        assert first != second

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_position_quantity_must_be_positive(self, quantity: int) -> None:
        """部位口數恆為正數，空單方向不得以負口數重複表示。"""
        with pytest.raises(ValueError, match="部位數量必須大於 0"):
            Position("TMFF6", Direction.SHORT, quantity, 23_000.0, 0.0)

    def test_order_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="數量必須大於 0"):
            _request(quantity=0)

    def test_limit_order_requires_price(self) -> None:
        with pytest.raises(ValueError, match="必須提供價格"):
            _request(price=None)

    def test_market_range_order_accepts_no_price(self) -> None:
        request = _request(price=None, price_type=PriceType.MKP)
        assert request.price is None

    def test_order_request_is_immutable(self) -> None:
        request = _request()
        with pytest.raises(FrozenInstanceError):
            request.__setattr__("quantity", 2)

    def test_order_event_match_is_exhaustive(self) -> None:
        now = datetime.now(timezone.utc)
        events: list[tuple[OrderEvent, str]] = [
            (
                FillEvent(None, "broker-1", "TMFF6", Direction.LONG, 23_000.0, 1, now),
                "fill",
            ),
            (RejectEvent(None, None, "E101", "委託遭拒", now), "reject"),
            (
                AckEvent(
                    "cid-1",
                    "broker-1",
                    "ordno-1",
                    "TMFF6",
                    Direction.LONG,
                    23_000.0,
                    1,
                    now,
                ),
                "ack",
            ),
            (CancelEvent("cid-1", "broker-1", "TMFF6", 1, 0, now, "user"), "cancel"),
        ]

        assert [_event_name(event) for event, _ in events] == [name for _, name in events]

    def test_order_event_is_immutable(self) -> None:
        event = CancelEvent(
            "cid-1",
            "broker-1",
            "TMFF6",
            1,
            0,
            datetime.now(timezone.utc),
        )
        with pytest.raises(FrozenInstanceError):
            event.__setattr__("reason", "session_end")
