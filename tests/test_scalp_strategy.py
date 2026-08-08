"""ScalpStrategy 穿越觸發、狀態機與出場安全測試。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from microtx.broker.base import FillEvent, RejectEvent
from microtx.contracts import TMF
from microtx.enums import Direction, OrderIntent, StrategyState
from microtx.exceptions import StrategyError
from microtx.market.tick import TickEvent
from microtx.strategies.scalp import ScalpStrategy

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _tick(price: float) -> TickEvent:
    return TickEvent(TMF.symbol, "TMFF6", _NOW, price, 1, 1, 0, _NOW)


def _fill(action: Direction, price: float, quantity: int = 1) -> FillEvent:
    return FillEvent("client", "broker", "TMFF6", action, price, quantity, _NOW)


def _reject(message: str = "價格超界") -> RejectEvent:
    return RejectEvent("client", "broker", "E101", message, _NOW)


def _strategy(
    *,
    direction: Direction = Direction.LONG,
    trigger_price: float = 23_150.0,
    take_profit_points: int = 50,
    stop_loss_points: int = 30,
    quantity: int = 1,
    trailing_points: int | None = None,
) -> ScalpStrategy:
    return ScalpStrategy(
        spec=TMF,
        direction=direction,
        trigger_price=trigger_price,
        take_profit_points=take_profit_points,
        stop_loss_points=stop_loss_points,
        quantity=quantity,
        trailing_points=trailing_points,
    )


def _enter(strategy: ScalpStrategy, *, price: float = 23_000.0, quantity: int = 1) -> None:
    strategy.arm()
    strategy.on_tick(_tick(strategy_price_for_trigger(strategy)))
    strategy.on_fill(_fill(_direction(strategy), price, quantity))


def _direction(strategy: ScalpStrategy) -> Direction:
    description = strategy.describe()
    return Direction.LONG if " LONG " in description else Direction.SHORT


def strategy_price_for_trigger(strategy: ScalpStrategy) -> float:
    description = strategy.describe()
    marker = "trigger="
    return float(description.split(marker, maxsplit=1)[1].split(maxsplit=1)[0])


def test_jump_crossing_triggers_entry() -> None:
    strategy = _strategy(trigger_price=23_150.0)
    strategy.arm()
    assert strategy.on_tick(_tick(23_140.0)) == []

    signals = strategy.on_tick(_tick(23_180.0))

    # 必須用穿越比較；若照官方範例寫成相等，跳空行情會永遠漏掉進場。
    assert len(signals) == 1
    assert signals[0].intent is OrderIntent.ENTRY
    assert strategy.state is StrategyState.ENTRY_PENDING


def test_exact_trigger_price_triggers_entry() -> None:
    strategy = _strategy()
    strategy.arm()
    assert strategy.on_tick(_tick(23_150.0))[0].action is Direction.LONG


def test_entry_signal_is_idempotent_after_trigger() -> None:
    strategy = _strategy()
    strategy.arm()
    assert len(strategy.on_tick(_tick(23_180.0))) == 1
    assert strategy.on_tick(_tick(23_200.0)) == []


def test_short_entry_uses_downward_crossing() -> None:
    strategy = _strategy(direction=Direction.SHORT, trigger_price=23_000.0)
    strategy.arm()
    assert strategy.on_tick(_tick(23_010.0)) == []
    signal = strategy.on_tick(_tick(22_980.0))[0]
    assert signal.action is Direction.SHORT


@pytest.mark.parametrize(
    ("price", "expected_intent"),
    [(23_050.0, OrderIntent.TAKE_PROFIT), (22_970.0, OrderIntent.STOP_LOSS)],
)
def test_long_take_profit_and_stop_loss(price: float, expected_intent: OrderIntent) -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)

    signal = strategy.on_tick(_tick(price))[0]

    assert signal.intent is expected_intent
    assert signal.action is Direction.SHORT
    assert strategy.state is StrategyState.EXIT_PENDING


@pytest.mark.parametrize(
    ("price", "expected_intent"),
    [(22_950.0, OrderIntent.TAKE_PROFIT), (23_030.0, OrderIntent.STOP_LOSS)],
)
def test_short_take_profit_and_stop_loss(price: float, expected_intent: OrderIntent) -> None:
    strategy = _strategy(direction=Direction.SHORT, trigger_price=23_000.0)
    _enter(strategy)
    signal = strategy.on_tick(_tick(price))[0]
    assert signal.intent is expected_intent
    assert signal.action is Direction.LONG


def test_only_one_exit_signal_can_be_emitted() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)

    first = strategy.on_tick(_tick(23_050.0))
    second = strategy.on_tick(_tick(22_900.0))
    forced = strategy.force_close("收盤強平")

    # 出場意圖若重複，兩張平倉單都成交會把空手變成反向持倉。
    assert len(first) == 1
    assert second == []
    assert forced == []
    assert strategy.state is StrategyState.EXIT_PENDING


def test_force_close_wins_before_price_exit_check() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)

    forced = strategy.force_close("收盤強平")
    price_signals = strategy.on_tick(_tick(22_900.0))

    assert [signal.intent for signal in forced] == [OrderIntent.FORCE_CLOSE]
    assert price_signals == []


def test_fill_immediately_checks_latest_stop_price() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    strategy.arm()
    strategy.on_tick(_tick(23_000.0))
    strategy.on_tick(_tick(22_950.0))

    signals = strategy.on_fill(_fill(Direction.LONG, 23_000.0))

    assert [signal.intent for signal in signals] == [OrderIntent.STOP_LOSS]
    assert strategy.state is StrategyState.EXIT_PENDING


def test_trailing_stop_only_moves_in_favorable_direction() -> None:
    strategy = _strategy(
        trigger_price=23_000.0,
        take_profit_points=100,
        trailing_points=10,
    )
    _enter(strategy)
    assert strategy.on_tick(_tick(23_040.0)) == []
    assert strategy.stop_price == 23_030.0
    assert strategy.on_tick(_tick(23_035.0)) == []
    assert strategy.stop_price == 23_030.0

    signal = strategy.on_tick(_tick(23_030.0))[0]
    assert signal.intent is OrderIntent.STOP_LOSS


def test_terminal_state_ignores_ticks() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)
    strategy.on_tick(_tick(23_050.0))
    strategy.on_fill(_fill(Direction.SHORT, 23_050.0))

    assert strategy.state is StrategyState.CLOSED
    assert strategy.on_tick(_tick(20_000.0)) == []
    assert strategy.on_fill(_fill(Direction.SHORT, 23_050.0)) == []


def test_stop_price_is_unknown_before_entry_fill() -> None:
    assert _strategy().stop_price is None


def test_illegal_transition_raises_strategy_error() -> None:
    strategy = _strategy()
    strategy.arm()
    with pytest.raises(StrategyError, match="非法策略狀態轉換"):
        strategy.arm()


@pytest.mark.parametrize("initial", [StrategyState.IDLE, StrategyState.ARMED])
def test_force_close_without_position_cancels(initial: StrategyState) -> None:
    strategy = _strategy()
    if initial is StrategyState.ARMED:
        strategy.arm()
    assert strategy.force_close("人工停止") == []
    assert strategy.state is StrategyState.CANCELLED


def test_entry_reject_transitions_to_cancelled_and_records_reason() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.on_tick(_tick(23_150.0))

    assert strategy.on_reject(_reject()) == []
    assert strategy.state is StrategyState.CANCELLED
    assert "E101" in strategy.last_transition_reason


def test_partial_fills_accumulate_weighted_average() -> None:
    strategy = _strategy(trigger_price=23_000.0, quantity=2)
    strategy.arm()
    strategy.on_tick(_tick(23_000.0))

    assert strategy.on_fill(_fill(Direction.LONG, 23_000.0)) == []
    assert strategy.state is StrategyState.ENTRY_PENDING
    assert strategy.entry_price == 23_000.0
    assert strategy.on_fill(_fill(Direction.LONG, 23_020.0)) == []
    assert strategy.state is StrategyState.IN_POSITION
    assert strategy.entry_price == 23_010.0
    assert strategy.filled_quantity == 2


def test_partial_entry_is_still_protected_by_stop_loss() -> None:
    strategy = _strategy(trigger_price=23_000.0, quantity=2)
    strategy.arm()
    strategy.on_tick(_tick(23_000.0))
    strategy.on_fill(_fill(Direction.LONG, 23_000.0))

    signals = strategy.on_tick(_tick(22_970.0))

    # 狀態雖仍等待剩餘進場成交，已成交的一口已有完整市場風險，不能裸露無停損。
    assert [(signal.intent, signal.quantity) for signal in signals] == [(OrderIntent.STOP_LOSS, 1)]
    assert strategy.state is StrategyState.EXIT_PENDING


def test_exit_pending_ticks_and_force_close_never_emit_again() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)
    assert strategy.on_tick(_tick(23_050.0))
    assert strategy.on_tick(_tick(22_900.0)) == []
    assert strategy.force_close("再次強平") == []


def test_partial_exit_closes_only_after_all_fills() -> None:
    strategy = _strategy(trigger_price=23_000.0, quantity=2)
    _enter(strategy, quantity=2)
    strategy.force_close("收盤")

    strategy.on_fill(_fill(Direction.SHORT, 23_000.0, 1))
    assert strategy.state is StrategyState.EXIT_PENDING
    assert strategy.filled_quantity == 1
    strategy.on_fill(_fill(Direction.SHORT, 23_000.0, 1))
    assert strategy.state is StrategyState.CLOSED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger_price": 0.0},
        {"take_profit_points": 0},
        {"stop_loss_points": 0},
        {"quantity": 0},
        {"trailing_points": 0},
    ],
)
def test_invalid_parameters_raise(kwargs: dict[str, object]) -> None:
    defaults: dict[str, object] = {
        "spec": TMF,
        "direction": Direction.LONG,
        "trigger_price": 23_000.0,
        "take_profit_points": 50,
        "stop_loss_points": 30,
    }
    defaults.update(kwargs)
    with pytest.raises(ValueError):
        ScalpStrategy(**defaults)


def test_invalid_fill_and_reject_states_raise() -> None:
    strategy = _strategy()
    with pytest.raises(StrategyError, match="不接受成交"):
        strategy.on_fill(_fill(Direction.LONG, 23_000.0))
    with pytest.raises(StrategyError, match="不接受拒單"):
        strategy.on_reject(_reject())


def test_wrong_fill_directions_and_overfills_raise() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    strategy.arm()
    strategy.on_tick(_tick(23_000.0))
    with pytest.raises(StrategyError, match="進場成交方向"):
        strategy.on_fill(_fill(Direction.SHORT, 23_000.0))
    with pytest.raises(StrategyError, match="超過策略口數"):
        strategy.on_fill(_fill(Direction.LONG, 23_000.0, 2))

    strategy.on_fill(_fill(Direction.LONG, 23_000.0))
    strategy.force_close("收盤")
    with pytest.raises(StrategyError, match="出場成交方向"):
        strategy.on_fill(_fill(Direction.LONG, 23_000.0))
    with pytest.raises(StrategyError, match="超過剩餘部位"):
        strategy.on_fill(_fill(Direction.SHORT, 23_000.0, 2))


def test_exit_reject_transitions_to_error() -> None:
    strategy = _strategy(trigger_price=23_000.0)
    _enter(strategy)
    strategy.force_close("收盤")
    strategy.on_reject(_reject("平倉遭拒"))
    assert strategy.state is StrategyState.ERROR


def test_describe_is_human_readable() -> None:
    description = _strategy().describe()
    assert "Scalp" in description
    assert TMF.symbol in description
    assert "state=IDLE" in description
