"""RiskManager 八條規則與緊急平倉優先序測試。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from microtx.broker.base import OrderRequest, new_client_id
from microtx.config import Settings
from microtx.engine.position import PositionSnapshot
from microtx.engine.risk import RiskContext, RiskManager
from microtx.enums import (
    Direction,
    EngineState,
    OrderIntent,
    PriceType,
    SessionType,
    TimeInForce,
)

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        max_daily_loss=3_000.0,
        max_daily_trades=10,
        max_position_size=2,
        order_cooldown_sec=3.0,
    )


def _request(
    *,
    intent: OrderIntent = OrderIntent.ENTRY,
    action: Direction = Direction.LONG,
    quantity: int = 1,
    price: float | None = 23_000.0,
) -> OrderRequest:
    return OrderRequest(
        "TMFR1",
        action,
        quantity,
        price,
        PriceType.LMT if price is not None else PriceType.MKP,
        TimeInForce.ROD,
        intent,
        new_client_id(),
    )


def _ctx(**changes: object) -> RiskContext:
    base = RiskContext(
        now=_NOW,
        session=SessionType.DAY,
        engine_state=EngineState.RUNNING,
        position=PositionSnapshot(None, 0, 0.0, 0.0, 0.0),
        realized_pnl_ntd=0.0,
        total_pnl_ntd=0.0,
        trade_count=0,
        last_order_at=None,
        price_limits=(20_000.0, 25_000.0),
    )
    return replace(base, **changes)


def test_emergency_bypasses_loss_trade_limit_and_cooldown() -> None:
    manager = RiskManager(_settings())
    context = _ctx(
        total_pnl_ntd=-9_999.0,
        realized_pnl_ntd=-9_999.0,
        trade_count=999,
        last_order_at=_NOW,
        engine_state=EngineState.HALTED,
        session=SessionType.CLOSED,
    )

    decision = manager.check(_request(intent=OrderIntent.EMERGENCY, price=None), context)

    # 緊急平倉是第一條規則；虧損、次數、節流與停機同時觸發也不得擋下救命單。
    assert decision.approved is True


@pytest.mark.parametrize(
    ("context", "message"),
    [
        (_ctx(session=SessionType.CLOSED), "非交易時段"),
        (_ctx(engine_state=EngineState.HALTED), "引擎已停機"),
        (_ctx(total_pnl_ntd=-3_000.0), "單日停損"),
        (_ctx(trade_count=10), "交易上限"),
        (_ctx(last_order_at=_NOW - timedelta(seconds=2)), "下單節流"),
    ],
)
def test_entry_rejection_rules(context: RiskContext, message: str) -> None:
    decision = RiskManager(_settings()).check(_request(), context)
    assert decision.approved is False
    assert message in decision.reason


def test_projected_position_limit_handles_same_and_opposite_direction() -> None:
    manager = RiskManager(_settings())
    long_two = PositionSnapshot(Direction.LONG, 2, 23_000.0, 0.0, 0.0)
    assert manager.check(_request(), _ctx(position=long_two)).approved is False
    assert manager.check(_request(action=Direction.SHORT), _ctx(position=long_two)).approved is True


@pytest.mark.parametrize(("price", "message"), [(19_999.0, "跌停"), (25_001.0, "漲停")])
def test_price_limit_rejection(price: float, message: str) -> None:
    decision = RiskManager(_settings()).check(_request(price=price), _ctx())
    assert decision.approved is False
    assert message in decision.reason


def test_missing_price_limits_and_market_price_skip_rule() -> None:
    manager = RiskManager(_settings())
    assert manager.check(_request(), _ctx(price_limits=None)).approved is True
    assert manager.check(_request(price=None), _ctx()).approved is True


def test_close_orders_bypass_halt_loss_trade_and_cooldown() -> None:
    context = _ctx(
        engine_state=EngineState.HALTED,
        total_pnl_ntd=-3_000.0,
        trade_count=10,
        last_order_at=_NOW,
    )
    decision = RiskManager(_settings()).check(
        _request(intent=OrderIntent.STOP_LOSS, action=Direction.SHORT), context
    )
    assert decision.approved is True


def test_should_halt_returns_reason() -> None:
    manager = RiskManager(_settings())
    halted, reason = manager.should_halt(_ctx(total_pnl_ntd=-3_001.0))
    assert halted is True
    assert "3,000" in reason
    assert manager.should_halt(_ctx(total_pnl_ntd=-2_999.0))[0] is False
