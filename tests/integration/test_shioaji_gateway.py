"""Shioaji 模擬環境端到端整合測試。"""

from __future__ import annotations

from threading import Event

import pytest

from microtx.broker.base import OrderRequest, RawTick
from microtx.broker.shioaji_gateway import ShioajiGateway
from microtx.config import PROJECT_ROOT, Settings
from microtx.enums import Direction, OrderIntent, PriceType, TimeInForce

_SETTINGS = Settings()
_HAS_ENV = (PROJECT_ROOT / ".env").is_file()
_HAS_KEY = bool(_SETTINGS.shioaji_api_key.get_secret_value())

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (_HAS_ENV and _HAS_KEY), reason="缺少 .env 或 Shioaji API Key"),
]


def test_simulation_login_tick_order_query_cancel_logout() -> None:
    assert _SETTINGS.simulation is True
    gateway = ShioajiGateway(_SETTINGS)
    received = Event()
    latest: list[RawTick] = []

    def on_tick(tick: RawTick) -> None:
        latest.append(tick)
        received.set()

    gateway.connect()
    try:
        gateway.subscribe_ticks(_SETTINGS.symbol, on_tick)
        assert received.wait(30.0), "30 秒內未收到模擬行情"
        price = latest[-1].price
        request = OrderRequest(
            _SETTINGS.symbol,
            Direction.LONG,
            1,
            price,
            PriceType.LMT,
            TimeInForce.ROD,
            OrderIntent.ENTRY,
            "integration-order",
        )
        acknowledgement = gateway.place_order(request)
        assert acknowledgement.accepted is True
        assert gateway.list_open_orders()
        assert gateway.cancel_all_orders() >= 1
    finally:
        gateway.disconnect()
