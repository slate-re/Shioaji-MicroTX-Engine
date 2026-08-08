"""券商閘道的共用資料型別與抽象介面。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from microtx.enums import Direction, OrderIntent, PriceType, TimeInForce


def new_client_id() -> str:
    """產生供委託冪等控制使用的短識別碼。

    Returns:
        UUID4 十六進位表示的前 16 碼。
    """
    return uuid4().hex[:16]


@dataclass(frozen=True, slots=True)
class Position:
    """券商回報的實際部位。"""

    code: str
    direction: Direction
    quantity: int
    average_price: float
    unrealized_pnl: float


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """送往券商的委託請求。"""

    symbol: str
    action: Direction
    quantity: int
    price: float | None
    price_type: PriceType
    time_in_force: TimeInForce
    intent: OrderIntent
    client_id: str

    def __post_init__(self) -> None:
        """驗證委託數量與限價委託價格。"""
        if self.quantity <= 0:
            raise ValueError("委託數量必須大於 0")
        if self.price_type is PriceType.LMT and self.price is None:
            raise ValueError("限價委託必須提供價格")


@dataclass(frozen=True, slots=True)
class OrderAck:
    """下單當下的同步回應，並非成交回報。"""

    client_id: str
    broker_order_id: str | None
    accepted: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """尚未完全成交的委託。"""

    broker_order_id: str
    client_id: str | None
    code: str
    action: Direction
    price: float
    quantity: int
    filled_quantity: int


@dataclass(frozen=True, slots=True)
class RawTick:
    """券商原生 tick 的最小共通表示，尚未過濾試撮資料。"""

    code: str
    timestamp: datetime
    price: float
    volume: int
    total_volume: int
    tick_type: int
    simtrade: bool


@dataclass(frozen=True, slots=True)
class FillEvent:
    """委託的單次成交回報。"""

    client_id: str | None
    broker_order_id: str
    code: str
    action: Direction
    price: float
    quantity: int
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class RejectEvent:
    """券商拒絕委託的回報。"""

    client_id: str | None
    broker_order_id: str | None
    code: str
    message: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class AckEvent:
    """交易所接受委託的回報，包含後續改刪單所需的交易所單號。"""

    client_id: str | None
    broker_order_id: str
    exchange_order_no: str
    code: str
    action: Direction
    price: float
    quantity: int
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class CancelEvent:
    """委託被刪除或失效的回報，包含取消後的剩餘數量。"""

    client_id: str | None
    broker_order_id: str
    code: str
    cancelled_quantity: int
    remaining_quantity: int
    timestamp: datetime
    reason: str = ""


OrderEvent = FillEvent | RejectEvent | AckEvent | CancelEvent


class BrokerGateway(ABC):
    """券商閘道抽象介面。"""

    @abstractmethod
    def connect(self) -> None:
        """建立券商連線。"""

    @abstractmethod
    def disconnect(self) -> None:
        """關閉券商連線。"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """回傳目前是否已連線。"""

    @abstractmethod
    def subscribe_ticks(self, symbol: str, callback: Callable[[RawTick], None]) -> None:
        """訂閱指定商品的成交行情。"""

    @abstractmethod
    def unsubscribe_ticks(self, symbol: str) -> None:
        """取消指定商品的成交行情訂閱。"""

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderAck:
        """送出委託並回傳同步確認。"""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """取消指定券商委託。"""

    @abstractmethod
    def cancel_all_orders(self) -> int:
        """取消全部未完成委託並回傳成功筆數。"""

    @abstractmethod
    def list_open_orders(self) -> list[OpenOrder]:
        """查詢全部未完成委託。"""

    @abstractmethod
    def list_positions(self) -> list[Position]:
        """查詢券商端的實際部位。"""

    @abstractmethod
    def set_order_event_callback(self, callback: Callable[[OrderEvent], None]) -> None:
        """設定委託事件回呼。"""

    @abstractmethod
    def get_price_limits(self, symbol: str) -> tuple[float, float]:
        """查詢商品的跌停價與漲停價。"""
