"""不需券商連線的確定性模擬撮合閘道。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from secrets import SystemRandom
from threading import RLock, Timer
from zoneinfo import ZoneInfo

from microtx.broker.base import (
    AckEvent,
    BrokerGateway,
    CancelEvent,
    FillEvent,
    OpenOrder,
    OrderAck,
    OrderEvent,
    OrderRequest,
    Position,
    RawTick,
    RejectEvent,
)
from microtx.contracts import FuturesSpec
from microtx.enums import Direction, EventOrder, OrderIntent, PriceType, TimeInForce
from microtx.exceptions import ConnectionLostError
from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")
_RANDOM = SystemRandom()
_CLOSE_ONLY_INTENTS = frozenset(
    {
        OrderIntent.TAKE_PROFIT,
        OrderIntent.STOP_LOSS,
        OrderIntent.FORCE_CLOSE,
        OrderIntent.EMERGENCY,
    }
)


@dataclass(frozen=True, slots=True)
class _PendingOrder:
    """模擬市場中的未完成委託。"""

    request: OrderRequest
    broker_order_id: str
    exchange_order_no: str
    remaining_quantity: int
    filled_quantity: int = 0


@dataclass(frozen=True, slots=True)
class _PositionLot:
    """供 FIFO 平倉使用的單筆持倉批次。"""

    direction: Direction
    quantity: int
    price: float


class PaperGateway(BrokerGateway):
    """提供離線測試與 Demo 使用的執行緒安全模擬券商。"""

    def __init__(
        self,
        *,
        spec: FuturesSpec,
        initial_price: float = 23_000.0,
        slippage_ticks: int = 0,
        fill_delay_sec: float = 0.0,
        reject_rate: float = 0.0,
        max_fill_quantity_per_tick: int | None = None,
        event_order: EventOrder = EventOrder.FILL_FIRST,
    ) -> None:
        """初始化模擬閘道。

        Args:
            spec: 撮合商品規格。
            initial_price: 尚未注入 tick 前使用的最新成交價。
            slippage_ticks: 市價類委託的固定滑價 tick 數。
            fill_delay_sec: FillEvent 額外延遲秒數。
            reject_rate: 每筆新委託被整筆拒絕的機率。
            max_fill_quantity_per_tick: 每次撮合最多成交口數；None 表示無限。
            event_order: 立即成交時 AckEvent 與 FillEvent 的順序。

        Raises:
            ValueError: 控制參數超出合法範圍。
        """
        if slippage_ticks < 0:
            raise ValueError("滑價 tick 數不可為負數")
        if fill_delay_sec < 0:
            raise ValueError("成交回報延遲不可為負數")
        if not 0.0 <= reject_rate <= 1.0:
            raise ValueError("拒單機率必須介於 0 與 1 之間")
        if max_fill_quantity_per_tick is not None and max_fill_quantity_per_tick <= 0:
            raise ValueError("每 tick 最大成交量必須大於 0")

        self._spec = spec
        self._latest_price = initial_price
        self._slippage_ticks = slippage_ticks
        self._fill_delay_sec = fill_delay_sec
        self._reject_rate = reject_rate
        self._max_fill_quantity = max_fill_quantity_per_tick
        self._event_order = event_order
        self._connected = False
        self._lock = RLock()
        self._subscriptions: dict[str, Callable[[RawTick], None]] = {}
        self._order_callback: Callable[[OrderEvent], None] | None = None
        self._open_orders: dict[str, _PendingOrder] = {}
        self._positions: dict[str, Position] = {}
        self._position_lots: dict[str, list[_PositionLot]] = {}
        self._seen_client_ids: set[str] = set()
        self._sequence = 0
        self._total_volume = 0
        self._price_limits = (float("-inf"), float("inf"))
        self._realized_pnl = 0.0
        self._pending_timers: dict[Timer, tuple[OrderEvent, ...]] = {}

    @property
    def is_connected(self) -> bool:
        """回傳模擬連線狀態。"""
        with self._lock:
            return self._connected

    @property
    def realized_pnl(self) -> float:
        """回傳累計已實現損益，單位為新台幣。"""
        with self._lock:
            return self._realized_pnl

    def connect(self) -> None:
        """建立模擬連線。"""
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        """關閉模擬連線。"""
        with self._lock:
            self._cancel_pending_timers_locked()
            self._connected = False

    def force_disconnect(self) -> None:
        """強制切斷模擬連線。"""
        self.disconnect()

    def subscribe_ticks(self, symbol: str, callback: Callable[[RawTick], None]) -> None:
        """註冊指定商品的同步行情 callback。"""
        with self._lock:
            self._subscriptions[symbol] = callback

    def unsubscribe_ticks(self, symbol: str) -> None:
        """取消指定商品的行情 callback。"""
        with self._lock:
            self._subscriptions.pop(symbol, None)

    def set_order_event_callback(self, callback: Callable[[OrderEvent], None]) -> None:
        """註冊委託事件 callback。"""
        with self._lock:
            self._order_callback = callback

    def feed_tick(self, price: float, *, volume: int = 1, simtrade: bool = False) -> None:
        """手動注入 tick，並同步觸發行情 callback 與掛單撮合。"""
        with self._lock:
            self._total_volume += volume
            tick = RawTick(
                code=self._spec.symbol,
                timestamp=datetime.now(_TAIPEI),
                price=price,
                volume=volume,
                total_volume=self._total_volume,
                tick_type=0,
                simtrade=simtrade,
            )
            callback, events = self._process_tick_locked(tick)
        if callback is not None:
            callback(tick)
        self._emit_many(events)

    def replay(self, ticks: Iterable[RawTick], *, speed: float = 0.0) -> None:
        """依時間戳重播 tick；speed=0 時不等待。"""
        if speed < 0:
            raise ValueError("重播速度不可為負數")
        previous: datetime | None = None
        for tick in ticks:
            if speed > 0 and previous is not None:
                delay = max((tick.timestamp - previous).total_seconds() / speed, 0.0)
                time.sleep(delay)
            with self._lock:
                self._total_volume = tick.total_volume
                callback, events = self._process_tick_locked(tick)
            if callback is not None:
                callback(tick)
            self._emit_many(events)
            previous = tick.timestamp

    def place_order(self, request: OrderRequest) -> OrderAck:
        """送出模擬委託，依最新價格同步撮合。"""
        with self._lock:
            self._ensure_connected()
            if request.client_id in self._seen_client_ids:
                return OrderAck(request.client_id, None, False, "重複的 client_id")
            self._seen_client_ids.add(request.client_id)
            pending = self._new_pending_order(request)

            if _RANDOM.random() < self._reject_rate:
                events: tuple[OrderEvent, ...] = (
                    RejectEvent(
                        request.client_id,
                        pending.broker_order_id,
                        "PAPER_REJECT",
                        "PaperGateway 隨機拒單",
                        datetime.now(_TAIPEI),
                    ),
                )
                ack = OrderAck(request.client_id, pending.broker_order_id, False, "模擬拒單")
            else:
                self._open_orders[pending.broker_order_id] = pending
                result_events = self._match_order(pending, self._latest_price, immediate=True)
                events = self._prepare_order_events_locked(pending, result_events)
                ack = OrderAck(request.client_id, pending.broker_order_id, True)
        self._emit_many(events)
        return ack

    def cancel_order(self, broker_order_id: str) -> bool:
        """取消指定模擬掛單。"""
        with self._lock:
            pending = self._open_orders.pop(broker_order_id, None)
            if pending is None:
                return False
            self._cancel_pending_timers_locked(broker_order_id)
            event = self._cancel_event(pending, pending.remaining_quantity, "user")
        self._emit(event)
        return True

    def cancel_all_orders(self) -> int:
        """刪除全部模擬掛單並回傳筆數。"""
        with self._lock:
            self._cancel_pending_timers_locked()
            pending_orders = tuple(self._open_orders.values())
            self._open_orders.clear()
            events = tuple(
                self._cancel_event(pending, pending.remaining_quantity, "user")
                for pending in pending_orders
            )
        self._emit_many(events)
        return len(pending_orders)

    def flush_pending_events(self) -> int:
        """立即送出全部待送事件，並回傳事件筆數。"""
        with self._lock:
            pending = tuple(self._pending_timers.items())
            self._pending_timers.clear()
            for timer, _ in pending:
                timer.cancel()
            events = tuple(event for _, timer_events in pending for event in timer_events)
        self._emit_many(events)
        return len(events)

    def list_open_orders(self) -> list[OpenOrder]:
        """回傳目前所有未完成委託。"""
        with self._lock:
            return [
                OpenOrder(
                    broker_order_id=pending.broker_order_id,
                    client_id=pending.request.client_id,
                    code=pending.request.symbol,
                    action=pending.request.action,
                    price=self._order_price(pending.request, self._latest_price),
                    quantity=pending.request.quantity,
                    filled_quantity=pending.filled_quantity,
                )
                for pending in self._open_orders.values()
            ]

    def list_positions(self) -> list[Position]:
        """以內部持倉帳本為唯一來源回傳正口數部位。"""
        with self._lock:
            return [position for position in self._positions.values() if position.quantity > 0]

    def set_price_limits(self, down: float, up: float) -> None:
        """設定模擬跌停價與漲停價。"""
        if down > up:
            raise ValueError("跌停價不可高於漲停價")
        with self._lock:
            self._price_limits = (down, up)

    def get_price_limits(self, symbol: str) -> tuple[float, float]:
        """回傳模擬跌停價與漲停價。"""
        del symbol
        with self._lock:
            return self._price_limits

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectionLostError("PaperGateway 尚未連線")

    def _new_pending_order(self, request: OrderRequest) -> _PendingOrder:
        self._sequence += 1
        order_no = f"P{self._sequence:08d}"
        return _PendingOrder(request, order_no, order_no, request.quantity)

    def _process_tick_locked(
        self, tick: RawTick
    ) -> tuple[Callable[[RawTick], None] | None, tuple[OrderEvent, ...]]:
        self._latest_price = tick.price
        self._refresh_unrealized_pnl()
        callback = self._subscriptions.get(tick.code)
        emitted: list[OrderEvent] = []
        for pending in tuple(self._open_orders.values()):
            events = self._match_order(pending, tick.price, immediate=False)
            emitted.extend(self._prepare_followup_events_locked(events))
        return callback, tuple(emitted)

    def _match_order(
        self, pending: _PendingOrder, market_price: float, *, immediate: bool
    ) -> tuple[OrderEvent, ...]:
        request = pending.request
        if request.price_type is PriceType.LMT and not self._limit_crossed(request, market_price):
            if immediate and request.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                self._open_orders.pop(pending.broker_order_id, None)
                reason = (
                    "ioc_expired" if request.time_in_force is TimeInForce.IOC else "fok_expired"
                )
                return (self._cancel_event(pending, pending.remaining_quantity, reason),)
            return ()

        fill_price = self._order_price(request, market_price)
        if not self._within_price_limits(fill_price):
            return ()

        fillable, close_cancel_reason = self._fillable_quantity(pending)
        if fillable == 0:
            self._open_orders.pop(pending.broker_order_id, None)
            reason = close_cancel_reason or self._expiry_reason(request.time_in_force)
            return (self._cancel_event(pending, pending.remaining_quantity, reason),)

        if request.time_in_force is TimeInForce.FOK and fillable < pending.remaining_quantity:
            self._open_orders.pop(pending.broker_order_id, None)
            return (self._cancel_event(pending, pending.remaining_quantity, "fok_expired"),)

        fill_event = self._apply_fill(pending, fillable, fill_price)
        current = self._open_orders.get(pending.broker_order_id)
        remaining = current.remaining_quantity if current is not None else 0
        events: list[OrderEvent] = [fill_event]

        if close_cancel_reason is not None:
            if current is not None:
                self._open_orders.pop(pending.broker_order_id, None)
            events.append(self._cancel_event(pending, remaining, close_cancel_reason))
        elif request.time_in_force is TimeInForce.IOC and remaining > 0:
            self._open_orders.pop(pending.broker_order_id, None)
            events.append(self._cancel_event(pending, remaining, "ioc_expired"))
        return tuple(events)

    def _fillable_quantity(self, pending: _PendingOrder) -> tuple[int, str | None]:
        request = pending.request
        remaining = pending.remaining_quantity
        if request.intent in _CLOSE_ONLY_INTENTS:
            position = self._positions.get(request.symbol)
            closable = (
                position.quantity
                if position is not None and request.action is position.direction.opposite
                else 0
            )
            # 安全不變式：平倉意圖只能減少既有反向部位，絕不可使部位變號。
            close_fillable = min(remaining, closable)
            if close_fillable == 0:
                return 0, "no_position"
            if close_fillable < remaining:
                return self._apply_liquidity_limit(close_fillable), "over_close"
            return self._apply_liquidity_limit(close_fillable), None
        return self._apply_liquidity_limit(remaining), None

    def _apply_liquidity_limit(self, quantity: int) -> int:
        if self._max_fill_quantity is None:
            return quantity
        return min(quantity, self._max_fill_quantity)

    def _apply_fill(self, pending: _PendingOrder, quantity: int, price: float) -> FillEvent:
        request = pending.request
        self._update_position(request.symbol, request.action, quantity, price)
        remaining = pending.remaining_quantity - quantity
        if remaining == 0:
            self._open_orders.pop(pending.broker_order_id, None)
        else:
            self._open_orders[pending.broker_order_id] = replace(
                pending,
                remaining_quantity=remaining,
                filled_quantity=pending.filled_quantity + quantity,
            )
        return FillEvent(
            request.client_id,
            pending.broker_order_id,
            request.symbol,
            request.action,
            price,
            quantity,
            datetime.now(_TAIPEI),
        )

    def _update_position(self, symbol: str, action: Direction, quantity: int, price: float) -> None:
        lots = self._position_lots.setdefault(symbol, [])
        remaining = quantity
        while lots and lots[0].direction is not action and remaining > 0:
            lot = lots[0]
            closed = min(lot.quantity, remaining)
            points = (price - lot.price) * lot.direction.sign
            self._realized_pnl += self._spec.points_to_ntd(points * closed)
            remaining -= closed
            if closed == lot.quantity:
                lots.pop(0)
            else:
                lots[0] = replace(lot, quantity=lot.quantity - closed)
        if remaining > 0:
            lots.append(_PositionLot(action, remaining, price))
        self._rebuild_position(symbol)
        self._refresh_unrealized_pnl()

    def _rebuild_position(self, symbol: str) -> None:
        lots = self._position_lots[symbol]
        if not lots:
            self._position_lots.pop(symbol)
            self._positions.pop(symbol, None)
            return
        quantity = sum(lot.quantity for lot in lots)
        average = sum(lot.price * lot.quantity for lot in lots) / quantity
        self._positions[symbol] = Position(symbol, lots[0].direction, quantity, average, 0.0)

    def _refresh_unrealized_pnl(self) -> None:
        for symbol, position in tuple(self._positions.items()):
            points = (self._latest_price - position.average_price) * position.direction.sign
            self._positions[symbol] = replace(
                position,
                unrealized_pnl=self._spec.points_to_ntd(points * position.quantity),
            )

    def _prepare_order_events_locked(
        self, pending: _PendingOrder, result_events: tuple[OrderEvent, ...]
    ) -> tuple[OrderEvent, ...]:
        ack = self._ack_event(pending)
        fills = tuple(event for event in result_events if isinstance(event, FillEvent))
        others = tuple(event for event in result_events if not isinstance(event, FillEvent))
        if not fills:
            return (ack, *others)
        if self._event_order is EventOrder.ACK_FIRST:
            delayed = self._prepare_fill_chain_locked(fills + others)
            return (ack, *delayed)
        return self._prepare_fill_chain_locked((*fills, ack, *others))

    def _prepare_followup_events_locked(
        self, events: tuple[OrderEvent, ...]
    ) -> tuple[OrderEvent, ...]:
        fills = tuple(event for event in events if isinstance(event, FillEvent))
        others = tuple(event for event in events if not isinstance(event, FillEvent))
        return self._prepare_fill_chain_locked(fills + others)

    def _prepare_fill_chain_locked(self, events: tuple[OrderEvent, ...]) -> tuple[OrderEvent, ...]:
        if not events:
            return ()
        if self._fill_delay_sec == 0:
            return events
        timer: Timer

        def fire() -> None:
            self._fire_timer(timer)

        timer = Timer(self._fill_delay_sec, fire)
        timer.daemon = True
        self._pending_timers[timer] = events
        timer.start()
        return ()

    def _fire_timer(self, timer: Timer) -> None:
        with self._lock:
            events = self._pending_timers.pop(timer, ())
        self._emit_many(events)

    def _cancel_pending_timers_locked(self, broker_order_id: str | None = None) -> int:
        selected = [
            (timer, events)
            for timer, events in self._pending_timers.items()
            if broker_order_id is None
            or any(event.broker_order_id == broker_order_id for event in events)
        ]
        for timer, _ in selected:
            timer.cancel()
            self._pending_timers.pop(timer, None)
        return len(selected)

    def _emit_many(self, events: tuple[OrderEvent, ...]) -> None:
        for event in events:
            self._emit(event)

    def _emit(self, event: OrderEvent) -> None:
        with self._lock:
            callback = self._order_callback
        if callback is not None:
            callback(event)

    @staticmethod
    def _cancel_event(pending: _PendingOrder, quantity: int, reason: str) -> CancelEvent:
        return CancelEvent(
            pending.request.client_id,
            pending.broker_order_id,
            pending.request.symbol,
            quantity,
            0,
            datetime.now(_TAIPEI),
            reason,
        )

    def _ack_event(self, pending: _PendingOrder) -> AckEvent:
        request = pending.request
        return AckEvent(
            request.client_id,
            pending.broker_order_id,
            pending.exchange_order_no,
            request.symbol,
            request.action,
            self._order_price(request, self._latest_price),
            request.quantity,
            datetime.now(_TAIPEI),
        )

    def _order_price(self, request: OrderRequest, market_price: float) -> float:
        if request.price_type is PriceType.LMT:
            if request.price is None:
                raise AssertionError("限價委託缺少價格")
            return request.price
        return market_price + request.action.sign * self._slippage_ticks * self._spec.tick_size

    @staticmethod
    def _limit_crossed(request: OrderRequest, market_price: float) -> bool:
        if request.price is None:
            return True
        if request.action is Direction.LONG:
            return market_price <= request.price
        return market_price >= request.price

    def _within_price_limits(self, price: float) -> bool:
        down, up = self._price_limits
        return down <= price <= up

    @staticmethod
    def _expiry_reason(time_in_force: TimeInForce) -> str:
        if time_in_force is TimeInForce.FOK:
            return "fok_expired"
        if time_in_force is TimeInForce.IOC:
            return "ioc_expired"
        return "no_position"
