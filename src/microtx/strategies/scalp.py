"""單向觸價進場與點數停利停損策略。"""

from __future__ import annotations

from microtx.broker.base import FillEvent, RejectEvent
from microtx.contracts import FuturesSpec
from microtx.enums import Direction, OrderIntent, StrategyState
from microtx.exceptions import StrategyError
from microtx.market.tick import TickEvent
from microtx.strategies.base import Signal, Strategy


class ScalpStrategy(Strategy):
    """觸價進場後依固定或移動停損出場的純邏輯策略。"""

    def __init__(
        self,
        *,
        spec: FuturesSpec,
        direction: Direction,
        trigger_price: float,
        take_profit_points: int,
        stop_loss_points: int,
        quantity: int = 1,
        trailing_points: int | None = None,
    ) -> None:
        """初始化單向策略並驗證所有價格參數。"""
        super().__init__(spec=spec, quantity=quantity)
        if trigger_price <= 0:
            raise ValueError("觸發價必須大於 0")
        if take_profit_points <= 0:
            raise ValueError("停利點數必須大於 0")
        if stop_loss_points <= 0:
            raise ValueError("停損點數必須大於 0")
        if trailing_points is not None and trailing_points <= 0:
            raise ValueError("移動停利點數必須大於 0")
        self._direction = direction
        self._trigger_price = trigger_price
        self._take_profit_points = take_profit_points
        self._stop_loss_points = stop_loss_points
        self._trailing_points = trailing_points
        self._filled_quantity = 0
        self._entry_value = 0.0
        self._entry_price: float | None = None
        self._best_price: float | None = None
        self._latest_price: float | None = None
        self._exit_signal_emitted = False

    @property
    def filled_quantity(self) -> int:
        """回傳目前尚未平倉的實際成交口數。"""
        return self._filled_quantity

    @property
    def entry_price(self) -> float | None:
        """回傳進場成交加權均價。"""
        return self._entry_price

    @property
    def stop_price(self) -> float | None:
        """回傳目前有效停損價。"""
        if self._entry_price is None:
            return None
        if self._trailing_points is not None and self._best_price is not None:
            return self._best_price - self._direction.sign * self._trailing_points
        return self._entry_price - self._direction.sign * self._stop_loss_points

    def arm(self) -> None:
        """將閒置策略武裝為等待觸價。"""
        self._transition(StrategyState.ARMED, "策略已武裝")

    def cancel(self, reason: str = "使用者取消") -> None:
        """取消尚未出場的未持倉策略。"""
        self._transition(StrategyState.CANCELLED, reason)

    def on_tick(self, tick: TickEvent) -> list[Signal]:
        """依狀態處理穿越進場與曝險出場條件。"""
        self._latest_price = tick.price
        if self._state.is_terminal or self._state is StrategyState.EXIT_PENDING:
            return []
        if self._filled_quantity > 0:
            return self._check_exit(tick.price)
        if self._state is not StrategyState.ARMED:
            return []
        triggered = (
            tick.price >= self._trigger_price
            if self._direction is Direction.LONG
            else tick.price <= self._trigger_price
        )
        if not triggered:
            return []
        self._transition(StrategyState.ENTRY_PENDING, "成交價穿越進場觸發價")
        return [
            Signal(
                intent=OrderIntent.ENTRY,
                action=self._direction,
                quantity=self._quantity,
                reason="成交價穿越進場觸發價",
            )
        ]

    def on_fill(self, fill: FillEvent) -> list[Signal]:
        """累計部分成交，並立即保護已存在的市場曝險。"""
        if self._state is StrategyState.ENTRY_PENDING:
            if fill.action is not self._direction:
                raise StrategyError("進場成交方向與策略方向不符")
            if self._filled_quantity + fill.quantity > self._quantity:
                raise StrategyError("進場累計成交量超過策略口數")
            self._entry_value += fill.price * fill.quantity
            self._filled_quantity += fill.quantity
            self._entry_price = self._entry_value / self._filled_quantity
            self._update_best_price(self._entry_price)
            if self._filled_quantity == self._quantity:
                self._transition(StrategyState.IN_POSITION, "進場委託已全數成交")
            if self._latest_price is not None:
                return self._check_exit(self._latest_price)
            return []
        if self._state is StrategyState.EXIT_PENDING:
            if fill.action is not self._direction.opposite:
                raise StrategyError("出場成交方向與策略方向不符")
            if fill.quantity > self._filled_quantity:
                raise StrategyError("出場成交量超過剩餘部位")
            self._filled_quantity -= fill.quantity
            if self._filled_quantity == 0:
                self._transition(StrategyState.CLOSED, "出場委託已全數成交")
            return []
        if self._state.is_terminal:
            return []
        raise StrategyError(f"狀態 {self._state.value} 不接受成交回報")

    def on_reject(self, reject: RejectEvent) -> list[Signal]:
        """將進場拒單轉為取消，其他在途拒單轉為錯誤。"""
        reason = f"委託遭拒：{reject.code} {reject.message}".strip()
        if self._state is StrategyState.ENTRY_PENDING:
            self._transition(StrategyState.CANCELLED, reason)
        elif self._state is StrategyState.EXIT_PENDING:
            self._transition(StrategyState.ERROR, reason)
        elif not self._state.is_terminal:
            raise StrategyError(f"狀態 {self._state.value} 不接受拒單回報")
        return []

    def force_close(self, reason: str) -> list[Signal]:
        """優先取消未持倉策略，或為實際曝險產生唯一強平訊號。"""
        if self._state is StrategyState.EXIT_PENDING or self._state.is_terminal:
            return []
        if self._filled_quantity > 0:
            return self._emit_exit(OrderIntent.FORCE_CLOSE, reason)
        self._transition(StrategyState.CANCELLED, reason)
        return []

    def describe(self) -> str:
        """回傳單向策略的一行摘要。"""
        return (
            f"Scalp {self._spec.symbol} {self._direction.value} qty={self._quantity} "
            f"trigger={self._trigger_price:g} state={self._state.value}"
        )

    def _check_exit(self, price: float) -> list[Signal]:
        if self._exit_signal_emitted or self._filled_quantity == 0:
            return []
        self._update_best_price(price)
        if self._entry_price is None:
            raise StrategyError("已有成交口數但缺少進場均價")
        stop_price = self.stop_price
        if stop_price is None:
            raise StrategyError("已有成交口數但缺少停損價")
        take_profit_price = self._entry_price + self._direction.sign * self._take_profit_points
        stop_triggered = (
            price <= stop_price if self._direction is Direction.LONG else price >= stop_price
        )
        take_profit_triggered = (
            price >= take_profit_price
            if self._direction is Direction.LONG
            else price <= take_profit_price
        )
        # 保守優先序：若未來條件擴充造成競合，停損必須先於停利確保離場。
        if stop_triggered:
            return self._emit_exit(OrderIntent.STOP_LOSS, "成交價穿越停損價")
        if take_profit_triggered:
            return self._emit_exit(OrderIntent.TAKE_PROFIT, "成交價穿越停利價")
        return []

    def _update_best_price(self, price: float) -> None:
        if self._best_price is None:
            self._best_price = price
            return
        if self._direction is Direction.LONG:
            self._best_price = max(self._best_price, price)
        else:
            self._best_price = min(self._best_price, price)

    def _emit_exit(self, intent: OrderIntent, reason: str) -> list[Signal]:
        if self._exit_signal_emitted or self._filled_quantity == 0:
            return []
        self._exit_signal_emitted = True
        self._transition(StrategyState.EXIT_PENDING, reason)
        return [
            Signal(
                intent=intent,
                action=self._direction.opposite,
                quantity=self._filled_quantity,
                reason=reason,
            )
        ]
