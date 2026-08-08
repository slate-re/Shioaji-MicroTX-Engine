"""引擎內部部位、FIFO lot 與損益帳本。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock

from microtx.broker.base import FillEvent, Position
from microtx.contracts import FuturesSpec
from microtx.enums import Direction
from microtx.market.tick import TickEvent


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """單一商品的不可變部位快照。"""

    direction: Direction | None
    quantity: int
    average_price: float
    unrealized_points: float
    unrealized_ntd: float


@dataclass(frozen=True, slots=True)
class _Lot:
    """供 FIFO 平倉使用的成交批次。"""

    direction: Direction
    quantity: int
    price: float


class PositionTracker:
    """執行緒安全的單一商品部位與損益唯一帳本。"""

    def __init__(self, spec: FuturesSpec) -> None:
        self._spec = spec
        self._lock = RLock()
        self._lots: list[_Lot] = []
        self._latest_price: float | None = None
        self._realized_pnl_ntd = 0.0
        self._trade_count = 0
        self._counted_entries: set[str] = set()

    def on_fill(self, fill: FillEvent) -> None:
        """依 FIFO 先平後開更新部位與已實現損益。"""
        with self._lock:
            remaining = fill.quantity
            opened = False
            while self._lots and self._lots[0].direction is not fill.action and remaining > 0:
                lot = self._lots[0]
                closed = min(lot.quantity, remaining)
                points = (fill.price - lot.price) * lot.direction.sign
                self._realized_pnl_ntd += self._spec.points_to_ntd(points * closed)
                remaining -= closed
                if closed == lot.quantity:
                    self._lots.pop(0)
                else:
                    self._lots[0] = replace(lot, quantity=lot.quantity - closed)
            if remaining > 0:
                self._lots.append(_Lot(fill.action, remaining, fill.price))
                opened = True
            entry_key = fill.client_id or fill.broker_order_id
            if opened and entry_key not in self._counted_entries:
                self._counted_entries.add(entry_key)
                self._trade_count += 1

    def on_tick(self, tick: TickEvent) -> None:
        """更新計算未實現損益所需的最新成交價。"""
        with self._lock:
            self._latest_price = tick.price

    def snapshot(self) -> PositionSnapshot:
        """回傳目前淨部位與未實現損益快照。"""
        with self._lock:
            if not self._lots:
                return PositionSnapshot(None, 0, 0.0, 0.0, 0.0)
            quantity = sum(lot.quantity for lot in self._lots)
            average = sum(lot.price * lot.quantity for lot in self._lots) / quantity
            direction = self._lots[0].direction
            unrealized_points = 0.0
            if self._latest_price is not None:
                unrealized_points = (self._latest_price - average) * direction.sign * quantity
            return PositionSnapshot(
                direction,
                quantity,
                average,
                unrealized_points,
                self._spec.points_to_ntd(unrealized_points),
            )

    @property
    def realized_pnl_ntd(self) -> float:
        """回傳當日已實現損益。"""
        with self._lock:
            return self._realized_pnl_ntd

    @property
    def total_pnl_ntd(self) -> float:
        """回傳已實現與未實現損益合計。"""
        with self._lock:
            return self._realized_pnl_ntd + self.snapshot().unrealized_ntd

    @property
    def trade_count(self) -> int:
        """回傳當日產生新曝險的進場委託數。"""
        with self._lock:
            return self._trade_count

    def reset_daily(self) -> None:
        """跨日重置已實現損益與進場次數，不改變隔夜部位。"""
        with self._lock:
            self._realized_pnl_ntd = 0.0
            self._trade_count = 0
            self._counted_entries.clear()

    def reconcile(self, broker_positions: list[Position]) -> list[str]:
        """比對內部快照與券商實際部位，回傳所有差異描述。"""
        internal = self.snapshot()
        differences: list[str] = []
        if len(broker_positions) > 1:
            differences.append(f"券商回報多筆部位：{len(broker_positions)}")
        broker = broker_positions[0] if len(broker_positions) == 1 else None
        if broker is None:
            if internal.quantity != 0:
                differences.append(f"內部持倉 {internal.quantity} 口，但券商為空手")
            return differences
        if internal.direction is not broker.direction:
            differences.append(
                f"方向不一致：內部={internal.direction} 券商={broker.direction.value}"
            )
        if internal.quantity != broker.quantity:
            differences.append(f"口數不一致：內部={internal.quantity} 券商={broker.quantity}")
        if abs(internal.average_price - broker.average_price) > 1e-9:
            differences.append(
                f"均價不一致：內部={internal.average_price:g} 券商={broker.average_price:g}"
            )
        return differences
