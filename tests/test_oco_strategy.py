"""OcoStrategy 雙向選擇與 ScalpStrategy 委派測試。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from microtx.broker.base import FillEvent, RejectEvent
from microtx.contracts import TMF
from microtx.enums import Direction, OrderIntent, StrategyState
from microtx.market.tick import TickEvent
from microtx.strategies.oco import OcoStrategy

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _tick(price: float) -> TickEvent:
    return TickEvent(TMF.symbol, "TMFF6", _NOW, price, 1, 1, 0, _NOW)


def _fill(action: Direction, price: float, quantity: int = 1) -> FillEvent:
    return FillEvent("client", "broker", "TMFF6", action, price, quantity, _NOW)


def _strategy(**kwargs: object) -> OcoStrategy:
    defaults: dict[str, object] = {
        "spec": TMF,
        "upper_trigger": 23_100.0,
        "lower_trigger": 22_900.0,
        "take_profit_points": 50,
        "stop_loss_points": 30,
    }
    defaults.update(kwargs)
    return OcoStrategy(**defaults)


def test_upper_trigger_selects_long_and_disables_lower_leg() -> None:
    strategy = _strategy()
    strategy.arm()

    signal = strategy.on_tick(_tick(23_120.0))[0]
    later = strategy.on_tick(_tick(22_800.0))

    assert signal.action is Direction.LONG
    assert later == []
    assert strategy.state is StrategyState.ENTRY_PENDING


def test_idle_tick_does_not_arm_strategy_implicitly() -> None:
    strategy = _strategy()
    assert strategy.on_tick(_tick(23_200.0)) == []


def test_lower_trigger_selects_short() -> None:
    strategy = _strategy()
    strategy.arm()
    signal = strategy.on_tick(_tick(22_880.0))[0]
    assert signal.action is Direction.SHORT


def test_active_leg_delegates_fill_take_profit_and_close() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.on_tick(_tick(23_100.0))
    strategy.on_fill(_fill(Direction.LONG, 23_100.0))
    assert strategy.state is StrategyState.IN_POSITION

    signal = strategy.on_tick(_tick(23_150.0))[0]
    assert signal.intent is OrderIntent.TAKE_PROFIT
    assert strategy.state is StrategyState.EXIT_PENDING
    strategy.on_fill(_fill(Direction.SHORT, 23_150.0))
    assert strategy.state is StrategyState.CLOSED


def test_active_leg_reject_is_delegated() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.on_tick(_tick(23_100.0))
    reject = RejectEvent("client", "broker", "E101", "拒單", _NOW)

    assert strategy.on_reject(reject) == []
    assert strategy.state is StrategyState.CANCELLED


def test_force_close_before_trigger_cancels_both_legs() -> None:
    strategy = _strategy()
    strategy.arm()
    assert strategy.force_close("人工停止") == []
    assert strategy.state is StrategyState.CANCELLED
    assert strategy.on_tick(_tick(23_200.0)) == []


def test_force_close_after_partial_fill_uses_actual_quantity() -> None:
    strategy = _strategy(quantity=2)
    strategy.arm()
    strategy.on_tick(_tick(23_100.0))
    strategy.on_fill(_fill(Direction.LONG, 23_100.0))

    signal = strategy.force_close("收盤")[0]

    assert (signal.intent, signal.quantity) == (OrderIntent.FORCE_CLOSE, 1)
    assert strategy.state is StrategyState.EXIT_PENDING
    assert strategy.force_close("再次強平") == []


def test_inactive_fill_and_reject_are_ignored() -> None:
    strategy = _strategy()
    assert strategy.on_fill(_fill(Direction.LONG, 23_000.0)) == []
    reject = RejectEvent("client", "broker", "E101", "拒單", _NOW)
    assert strategy.on_reject(reject) == []


def test_cancel_and_describe() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.cancel("不交易")
    assert strategy.state is StrategyState.CANCELLED
    assert "OCO" in strategy.describe()
    assert "upper=23100" in strategy.describe()


def test_cancel_after_trigger_delegates_to_active_leg() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.on_tick(_tick(23_100.0))
    strategy.cancel("不再進場")
    assert strategy.state is StrategyState.CANCELLED


@pytest.mark.parametrize(
    ("upper", "lower"),
    [(23_000.0, 23_000.0), (22_999.0, 23_000.0)],
)
def test_trigger_order_is_validated(upper: float, lower: float) -> None:
    with pytest.raises(ValueError, match="上方觸發價"):
        _strategy(upper_trigger=upper, lower_trigger=lower)


def test_child_parameter_validation_is_reused() -> None:
    with pytest.raises(ValueError, match="停利點數"):
        _strategy(take_profit_points=0)
