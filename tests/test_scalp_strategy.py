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
from microtx.strategies.oco import OcoStrategy
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


@pytest.mark.parametrize("state", list(StrategyState))
def test_abort_never_raises_in_any_state(state: StrategyState) -> None:
    strategy = _strategy()
    strategy._state = state

    strategy.abort("緊急平倉")

    expected = state if state.is_terminal else StrategyState.ABORTED
    assert strategy.state is expected


def test_aborted_strategy_ignores_tick_and_fill() -> None:
    strategy = _strategy()
    strategy.arm()
    strategy.on_tick(_tick(23_150.0))
    strategy.abort("緊急平倉")

    assert strategy.on_tick(_tick(25_000.0)) == []
    assert strategy.on_fill(_fill(Direction.LONG, 23_150.0)) == []


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


def _absolute_strategy(
    *,
    direction: Direction = Direction.LONG,
    trigger: float = 46_500.0,
    take_profit: float = 46_600.0,
    stop_loss: float = 46_400.0,
) -> ScalpStrategy:
    return ScalpStrategy(
        spec=TMF,
        direction=direction,
        trigger_price=trigger,
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
    )


def test_absolute_long_normal_take_profit_flow() -> None:
    strategy = _absolute_strategy()
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    assert strategy.on_fill(_fill(Direction.LONG, 46_500.0)) == []
    signal = strategy.on_tick(_tick(46_600.0))[0]
    assert signal.intent is OrderIntent.TAKE_PROFIT


def test_absolute_short_uses_directional_crossing() -> None:
    strategy = _absolute_strategy(
        direction=Direction.SHORT,
        trigger=46_300.0,
        take_profit=46_200.0,
        stop_loss=46_400.0,
    )
    strategy.arm()
    strategy.on_tick(_tick(46_300.0))
    strategy.on_fill(_fill(Direction.SHORT, 46_300.0))
    assert strategy.on_tick(_tick(46_200.0))[0].intent is OrderIntent.TAKE_PROFIT


def test_absolute_stop_does_not_slide_after_gap_fill() -> None:
    strategy = _absolute_strategy()
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    strategy.on_fill(_fill(Direction.LONG, 46_550.0))
    # 核心價值：跳空成交只壓縮獲利空間，固定風險底線不可滑到 46,520。
    assert strategy.stop_price == 46_400.0


def test_gap_fill_beyond_absolute_take_profit_exits_immediately() -> None:
    strategy = _absolute_strategy()
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    signals = strategy.on_fill(_fill(Direction.LONG, 46_650.0))
    assert signals[0].intent is OrderIntent.TAKE_PROFIT


def test_gap_fill_beyond_absolute_stop_exits_immediately() -> None:
    strategy = _absolute_strategy()
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    signals = strategy.on_fill(_fill(Direction.LONG, 46_350.0))
    assert signals[0].intent is OrderIntent.STOP_LOSS


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "take_profit_points": 50,
            "stop_loss_points": 30,
            "take_profit_price": 46_600.0,
            "stop_loss_price": 46_400.0,
        },
        {
            "take_profit_points": 50,
            "take_profit_price": 46_600.0,
            "stop_loss_price": 46_400.0,
        },
        {
            "take_profit_points": 50,
            "stop_loss_points": 30,
            "take_profit_price": 46_600.0,
        },
        {},
        {"take_profit_price": 46_600.0, "stop_loss_points": 30},
        {"take_profit_price": 46_600.0, "stop_loss_price": 46_550.0},
        {"take_profit_price": 46_600.5, "stop_loss_price": 46_400.0},
        {"take_profit_price": 46_600.0, "stop_loss_price": 46_400.0, "trailing_points": 20},
    ],
)
def test_absolute_mode_rejects_invalid_or_mixed_parameters(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ScalpStrategy(
            spec=TMF,
            direction=Direction.LONG,
            trigger_price=46_500.0,
            **kwargs,
        )


def test_absolute_short_rejects_wrong_price_order() -> None:
    with pytest.raises(ValueError, match="順序"):
        _absolute_strategy(
            direction=Direction.SHORT,
            trigger=46_300.0,
            take_profit=46_400.0,
            stop_loss=46_200.0,
        )


def test_describe_distinguishes_point_and_absolute_modes() -> None:
    assert "TP+" in _strategy().describe()
    absolute = _absolute_strategy().describe()
    assert "TP@46600" in absolute
    assert "SL@46400" in absolute


def test_oco_delegates_independent_absolute_prices_to_both_legs() -> None:
    strategy = OcoStrategy(
        spec=TMF,
        upper_trigger=46_500.0,
        lower_trigger=46_300.0,
        long_take_profit_price=46_600.0,
        long_stop_loss_price=46_000.0,
        short_take_profit_price=46_200.0,
        short_stop_loss_price=46_400.0,
    )
    assert strategy._long.stop_price == 46_000.0
    assert strategy._short.stop_price == 46_400.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"long_take_profit_price": 46_600.0},
        {
            "take_profit_points": 50,
            "stop_loss_points": 30,
            "long_take_profit_price": 46_600.0,
            "long_stop_loss_price": 46_400.0,
            "short_take_profit_price": 46_200.0,
            "short_stop_loss_price": 46_400.0,
        },
    ],
)
def test_oco_rejects_partial_or_mixed_absolute_mode(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OcoStrategy(
            spec=TMF,
            upper_trigger=46_500.0,
            lower_trigger=46_300.0,
            **kwargs,
        )


def test_default_execution_styles_preserve_market_signals() -> None:
    strategy = _absolute_strategy()
    strategy.arm()
    assert strategy.on_tick(_tick(46_500.0))[0].limit_price is None
    strategy.on_fill(_fill(Direction.LONG, 46_500.0))
    assert strategy.on_tick(_tick(46_600.0))[0].limit_price is None


def test_absolute_exit_and_entry_styles_are_independent() -> None:
    from microtx.enums import ExecutionStyle

    strategy = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=46_500.0,
        take_profit_price=46_600.0,
        stop_loss_price=46_400.0,
        entry_style=ExecutionStyle.LIMIT,
        take_profit_style=ExecutionStyle.LIMIT,
    )
    strategy.arm()
    assert strategy.on_tick(_tick(46_500.0))[0].limit_price == 46_500.0
    strategy.on_fill(_fill(Direction.LONG, 46_500.0))
    assert strategy.on_tick(_tick(46_600.0))[0].limit_price == 46_600.0


def test_limit_stop_uses_absolute_stop_price() -> None:
    from microtx.enums import ExecutionStyle

    strategy = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=46_500.0,
        take_profit_price=46_600.0,
        stop_loss_price=46_400.0,
        stop_loss_style=ExecutionStyle.LIMIT,
    )
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    strategy.on_fill(_fill(Direction.LONG, 46_500.0))
    assert strategy.on_tick(_tick(46_400.0))[0].limit_price == 46_400.0


@pytest.mark.parametrize("style_name", ["take_profit_style", "stop_loss_style"])
def test_point_mode_rejects_limit_exit(style_name: str) -> None:
    from microtx.enums import ExecutionStyle

    with pytest.raises(ValueError, match="限價需搭配絕對價格"):
        ScalpStrategy(
            spec=TMF,
            direction=Direction.LONG,
            trigger_price=23_000.0,
            take_profit_points=50,
            stop_loss_points=30,
            **{style_name: ExecutionStyle.LIMIT},
        )


def test_point_mode_allows_limit_entry() -> None:
    from microtx.enums import ExecutionStyle

    strategy = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=23_000.0,
        take_profit_points=50,
        stop_loss_points=30,
        entry_style=ExecutionStyle.LIMIT,
    )
    strategy.arm()
    assert strategy.on_tick(_tick(23_000.0))[0].limit_price == 23_000.0


@pytest.mark.parametrize(
    ("fill_price", "intent"),
    [(46_650.0, OrderIntent.TAKE_PROFIT), (46_350.0, OrderIntent.STOP_LOSS)],
)
def test_gap_beyond_limit_exit_degrades_to_market(
    fill_price: float, intent: OrderIntent, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from microtx.enums import ExecutionStyle

    strategy = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=46_500.0,
        take_profit_price=46_600.0,
        stop_loss_price=46_400.0,
        take_profit_style=ExecutionStyle.LIMIT,
        stop_loss_style=ExecutionStyle.LIMIT,
    )
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    with caplog.at_level(logging.INFO, logger="microtx.strategies.scalp"):
        signal = strategy.on_fill(_fill(Direction.LONG, fill_price))[0]
    assert signal.intent is intent
    assert signal.limit_price is None
    assert "降級為範圍市價" in caplog.text


def test_force_close_is_always_market_even_when_all_styles_are_limit() -> None:
    from microtx.enums import ExecutionStyle

    strategy = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=46_500.0,
        take_profit_price=46_600.0,
        stop_loss_price=46_400.0,
        entry_style=ExecutionStyle.LIMIT,
        take_profit_style=ExecutionStyle.LIMIT,
        stop_loss_style=ExecutionStyle.LIMIT,
    )
    strategy.arm()
    strategy.on_tick(_tick(46_500.0))
    strategy.on_fill(_fill(Direction.LONG, 46_500.0))
    signal = strategy.force_close("收盤強平")[0]
    assert signal.intent is OrderIntent.FORCE_CLOSE
    assert signal.limit_price is None


def test_execution_styles_are_visible_in_scalp_and_oco_descriptions() -> None:
    from microtx.enums import ExecutionStyle

    scalp = ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=46_500.0,
        take_profit_price=46_600.0,
        stop_loss_price=46_400.0,
        stop_loss_style=ExecutionStyle.LIMIT,
    )
    oco = OcoStrategy(
        spec=TMF,
        upper_trigger=46_500.0,
        lower_trigger=46_300.0,
        long_take_profit_price=46_600.0,
        long_stop_loss_price=46_400.0,
        short_take_profit_price=46_200.0,
        short_stop_loss_price=46_400.0,
        entry_style=ExecutionStyle.LIMIT,
    )
    assert "SL:LIMIT" in scalp.describe()
    assert "ENTRY:LIMIT" in oco.describe()
