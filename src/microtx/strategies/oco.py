"""由兩個 ScalpStrategy 組合而成的雙向 OCO 策略。"""

from __future__ import annotations

from microtx.broker.base import FillEvent, RejectEvent
from microtx.contracts import FuturesSpec
from microtx.enums import Direction, ExecutionStyle, StrategyState
from microtx.market.tick import TickEvent
from microtx.strategies.base import Signal, Strategy
from microtx.strategies.scalp import ScalpStrategy


class OcoStrategy(Strategy):
    """任一方向觸發後立即停用另一方向的雙向策略。"""

    def __init__(
        self,
        *,
        spec: FuturesSpec,
        upper_trigger: float,
        lower_trigger: float,
        take_profit_points: int | None = None,
        stop_loss_points: int | None = None,
        long_take_profit_price: float | None = None,
        long_stop_loss_price: float | None = None,
        short_take_profit_price: float | None = None,
        short_stop_loss_price: float | None = None,
        quantity: int = 1,
        entry_style: ExecutionStyle = ExecutionStyle.MARKET,
        take_profit_style: ExecutionStyle = ExecutionStyle.MARKET,
        stop_loss_style: ExecutionStyle = ExecutionStyle.MARKET,
    ) -> None:
        """初始化上破做多與下破做空的兩個委派策略。"""
        super().__init__(spec=spec, quantity=quantity)
        if upper_trigger <= lower_trigger:
            raise ValueError("上方觸發價必須高於下方觸發價")
        absolute_prices = (
            long_take_profit_price,
            long_stop_loss_price,
            short_take_profit_price,
            short_stop_loss_price,
        )
        absolute_count = sum(price is not None for price in absolute_prices)
        points_given = take_profit_points is not None or stop_loss_points is not None
        if absolute_count not in {0, 4}:
            raise ValueError("OCO 四個絕對出場價位必須一起提供")
        if absolute_count and points_given:
            raise ValueError("OCO 點數模式與絕對價格模式不可混用")
        self._upper_trigger = upper_trigger
        self._lower_trigger = lower_trigger
        self._long = ScalpStrategy(
            spec=spec,
            direction=Direction.LONG,
            trigger_price=upper_trigger,
            take_profit_points=take_profit_points,
            stop_loss_points=stop_loss_points,
            take_profit_price=long_take_profit_price,
            stop_loss_price=long_stop_loss_price,
            quantity=quantity,
            entry_style=entry_style,
            take_profit_style=take_profit_style,
            stop_loss_style=stop_loss_style,
        )
        self._short = ScalpStrategy(
            spec=spec,
            direction=Direction.SHORT,
            trigger_price=lower_trigger,
            take_profit_points=take_profit_points,
            stop_loss_points=stop_loss_points,
            take_profit_price=short_take_profit_price,
            stop_loss_price=short_stop_loss_price,
            quantity=quantity,
            entry_style=entry_style,
            take_profit_style=take_profit_style,
            stop_loss_style=stop_loss_style,
        )
        self._active: ScalpStrategy | None = None

    def arm(self) -> None:
        """同時武裝上下兩個觸價條件。"""
        self._long.arm()
        self._short.arm()
        self._transition(StrategyState.ARMED, "OCO 雙向條件已武裝")

    def cancel(self, reason: str = "使用者取消") -> None:
        """取消尚未觸發的雙向條件。"""
        if self._active is None:
            self._long.cancel(reason)
            self._short.cancel(reason)
        else:
            self._active.cancel(reason)
        self._transition(StrategyState.CANCELLED, reason)

    def abort(self, reason: str) -> None:
        """同步中止 OCO 本體與兩腿，且不產生任何委託訊號。"""
        self._long.abort(reason)
        self._short.abort(reason)
        super().abort(reason)

    def on_tick(self, tick: TickEvent) -> list[Signal]:
        """選定先觸發方向，後續完全委派給該 ScalpStrategy。"""
        if self._state.is_terminal or self._state is StrategyState.EXIT_PENDING:
            return []
        if self._active is not None:
            signals = self._active.on_tick(tick)
            self._sync_active_state("委派策略處理行情")
            return signals
        if self._state is not StrategyState.ARMED:
            return []
        signals = self._long.on_tick(tick)
        if signals:
            self._active = self._long
            self._short.cancel("OCO 另一方向已觸發")
        else:
            signals = self._short.on_tick(tick)
            if signals:
                self._active = self._short
                self._long.cancel("OCO 另一方向已觸發")
        if self._active is not None:
            self._sync_active_state("OCO 已選定觸發方向")
        return signals

    def on_fill(self, fill: FillEvent) -> list[Signal]:
        """將成交回報委派給已觸發方向。"""
        if self._active is None:
            return []
        signals = self._active.on_fill(fill)
        self._sync_active_state("委派策略處理成交")
        return signals

    def on_reject(self, reject: RejectEvent) -> list[Signal]:
        """將拒單回報委派給已觸發方向。"""
        if self._active is None:
            return []
        signals = self._active.on_reject(reject)
        self._sync_active_state("委派策略處理拒單")
        return signals

    def force_close(self, reason: str) -> list[Signal]:
        """取消未觸發 OCO，或強平已觸發方向的實際曝險。"""
        if self._state is StrategyState.EXIT_PENDING or self._state.is_terminal:
            return []
        if self._active is None:
            self._long.force_close(reason)
            self._short.force_close(reason)
            self._transition(StrategyState.CANCELLED, reason)
            return []
        signals = self._active.force_close(reason)
        self._sync_active_state(reason)
        return signals

    def describe(self) -> str:
        """回傳雙向策略的一行摘要。"""
        return (
            f"OCO {self._spec.symbol} qty={self._quantity} upper={self._upper_trigger:g} "
            f"lower={self._lower_trigger:g} legs=[{self._long.describe()} | "
            f"{self._short.describe()}] state={self._state.value}"
        )

    def _sync_active_state(self, reason: str) -> None:
        if self._active is not None and self._state is not self._active.state:
            self._transition(self._active.state, reason)
