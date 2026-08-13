"""PositionTracker FIFO 部位與損益測試。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from microtx.broker.base import FillEvent, Position
from microtx.contracts import TMF
from microtx.engine.position import PositionTracker
from microtx.enums import Direction
from microtx.exceptions import MicroTXError
from microtx.market.tick import TickEvent

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _fill(client: str, action: Direction, price: float, quantity: int) -> FillEvent:
    return FillEvent(client, f"broker-{client}", "TMFF6", action, price, quantity, _NOW)


def _tick(price: float) -> TickEvent:
    return TickEvent(TMF.symbol, "TMFF6", _NOW, price, 1, 1, 0, _NOW)


def test_long_build_and_weighted_average() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("a", Direction.LONG, 23_000.0, 1))
    tracker.on_fill(_fill("b", Direction.LONG, 23_020.0, 2))
    snapshot = tracker.snapshot()
    assert (snapshot.direction, snapshot.quantity) == (Direction.LONG, 3)
    assert snapshot.average_price == 23_013.333333333332
    assert tracker.trade_count == 2


def test_partial_fills_same_order_count_as_one_trade() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("same", Direction.SHORT, 23_000.0, 1))
    tracker.on_fill(_fill("same", Direction.SHORT, 22_990.0, 1))
    assert tracker.snapshot().quantity == 2
    assert tracker.trade_count == 1


def test_fifo_close_and_reverse_realizes_pnl() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("a", Direction.LONG, 23_000.0, 1))
    tracker.on_fill(_fill("b", Direction.LONG, 23_100.0, 1))

    tracker.on_fill(_fill("c", Direction.SHORT, 23_200.0, 3))

    snapshot = tracker.snapshot()
    assert (snapshot.direction, snapshot.quantity, snapshot.average_price) == (
        Direction.SHORT,
        1,
        23_200.0,
    )
    assert tracker.realized_pnl_ntd == 3_000.0
    assert tracker.trade_count == 3


def test_unrealized_and_total_pnl_use_contract_spec() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("a", Direction.SHORT, 23_000.0, 2))
    tracker.on_tick(_tick(22_950.0))
    snapshot = tracker.snapshot()
    assert snapshot.unrealized_points == 100.0
    assert snapshot.unrealized_ntd == 1_000.0
    assert tracker.total_pnl_ntd == 1_000.0


def test_reset_daily_preserves_position() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("a", Direction.LONG, 23_000.0, 1))
    tracker.on_fill(_fill("b", Direction.SHORT, 23_050.0, 1))
    tracker.reset_daily()
    assert tracker.realized_pnl_ntd == 0.0
    assert tracker.trade_count == 0
    assert tracker.snapshot().quantity == 0


def test_restore_daily_loads_only_accumulators_and_rejects_second_call() -> None:
    tracker = PositionTracker(TMF)
    tracker.restore_daily(realized_pnl_ntd=-2_900.0, trade_count=4)
    assert tracker.realized_pnl_ntd == -2_900.0
    assert tracker.trade_count == 4
    assert tracker.snapshot().quantity == 0
    with pytest.raises(MicroTXError, match="不可重複還原"):
        tracker.restore_daily(realized_pnl_ntd=0.0, trade_count=0)


def test_reconcile_detects_direction_quantity_and_average() -> None:
    tracker = PositionTracker(TMF)
    tracker.on_fill(_fill("a", Direction.LONG, 23_000.0, 2))
    assert tracker.reconcile([Position("TMFF6", Direction.LONG, 2, 23_000.0, 0.0)]) == []

    differences = tracker.reconcile([Position("TMFF6", Direction.SHORT, 1, 23_010.0, 0.0)])
    assert len(differences) == 3
    assert tracker.reconcile([]) == ["內部持倉 2 口，但券商為空手"]


def test_reconcile_reports_multiple_broker_positions() -> None:
    tracker = PositionTracker(TMF)
    differences = tracker.reconcile(
        [
            Position("TMFF6", Direction.LONG, 1, 23_000.0, 0.0),
            Position("TMFG6", Direction.LONG, 1, 23_010.0, 0.0),
        ]
    )
    assert differences == ["券商回報多筆部位：2"]
