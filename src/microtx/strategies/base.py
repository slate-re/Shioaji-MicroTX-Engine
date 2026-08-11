"""純邏輯交易策略的共用契約與狀態機。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from microtx.broker.base import FillEvent, RejectEvent
from microtx.contracts import FuturesSpec
from microtx.enums import Direction, OrderIntent, StrategyState
from microtx.exceptions import StrategyError
from microtx.market.tick import TickEvent


@dataclass(frozen=True, slots=True)
class Signal:
    """策略輸出的交易意圖。"""

    intent: OrderIntent
    action: Direction
    quantity: int
    reason: str
    limit_price: float | None = None


_VALID_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.IDLE: frozenset(
        {
            StrategyState.ARMED,
            StrategyState.CANCELLED,
            StrategyState.ABORTED,
            StrategyState.ERROR,
        }
    ),
    StrategyState.ARMED: frozenset(
        {
            StrategyState.ENTRY_PENDING,
            StrategyState.CANCELLED,
            StrategyState.ABORTED,
            StrategyState.ERROR,
        }
    ),
    StrategyState.ENTRY_PENDING: frozenset(
        {
            StrategyState.IN_POSITION,
            StrategyState.EXIT_PENDING,
            StrategyState.CANCELLED,
            StrategyState.ABORTED,
            StrategyState.ERROR,
        }
    ),
    StrategyState.IN_POSITION: frozenset(
        {StrategyState.EXIT_PENDING, StrategyState.ABORTED, StrategyState.ERROR}
    ),
    StrategyState.EXIT_PENDING: frozenset(
        {StrategyState.CLOSED, StrategyState.ABORTED, StrategyState.ERROR}
    ),
    StrategyState.CLOSED: frozenset(),
    StrategyState.CANCELLED: frozenset(),
    StrategyState.ABORTED: frozenset(),
    StrategyState.ERROR: frozenset(),
}


class Strategy(ABC):
    """無 I/O、無執行緒的策略抽象基底。"""

    def __init__(self, *, spec: FuturesSpec, quantity: int) -> None:
        """初始化策略共用狀態。

        Args:
            spec: 交易商品規格。
            quantity: 目標進場口數。

        Raises:
            ValueError: 口數不為正數。
        """
        if quantity <= 0:
            raise ValueError("策略口數必須大於 0")
        self._spec = spec
        self._quantity = quantity
        self._state = StrategyState.IDLE
        self._last_transition_reason = "初始化"

    @property
    def state(self) -> StrategyState:
        """回傳目前策略狀態。"""
        return self._state

    @property
    def last_transition_reason(self) -> str:
        """回傳最近一次狀態轉換原因。"""
        return self._last_transition_reason

    def _transition(self, new_state: StrategyState, reason: str) -> None:
        """執行唯一合法的狀態轉換入口。

        Args:
            new_state: 目標狀態。
            reason: 人類可讀的轉換原因。

        Raises:
            StrategyError: 目前狀態不允許轉至目標狀態。
        """
        if new_state not in _VALID_TRANSITIONS[self._state]:
            raise StrategyError(f"非法策略狀態轉換：{self._state.value} → {new_state.value}")
        self._state = new_state
        self._last_transition_reason = reason

    def abort(self, reason: str) -> None:
        """緊急中止策略；任何狀態下都不拋例外且不產生訊號。"""
        if self._state.is_terminal:
            return
        self._transition(StrategyState.ABORTED, reason)

    @abstractmethod
    def on_tick(self, tick: TickEvent) -> list[Signal]:
        """處理行情事件。"""

    @abstractmethod
    def on_fill(self, fill: FillEvent) -> list[Signal]:
        """處理成交事件。"""

    @abstractmethod
    def on_reject(self, reject: RejectEvent) -> list[Signal]:
        """處理拒單事件。"""

    @abstractmethod
    def force_close(self, reason: str) -> list[Signal]:
        """依目前曝險狀態取消策略或產生強制平倉訊號。"""

    @abstractmethod
    def describe(self) -> str:
        """回傳供 CLI 與日誌使用的一行摘要。"""
