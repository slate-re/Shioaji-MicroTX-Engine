"""正規化行情 tick 型別。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from microtx.broker.base import RawTick

_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class TickEvent:
    """已排除試撮資料的正規化成交 tick。"""

    symbol: str
    code: str
    timestamp: datetime
    price: float
    volume: int
    total_volume: int
    tick_type: int
    received_at: datetime

    @property
    def latency_ms(self) -> float:
        """交易所時間到本機收到的延遲毫秒數。"""
        return (self.received_at - self.timestamp).total_seconds() * 1000.0

    @classmethod
    def from_raw(cls, raw: RawTick, *, symbol: str) -> TickEvent:
        """由尚未過濾的券商 tick 建構正規化事件。

        Args:
            raw: 券商原生 tick；呼叫端須先排除試撮資料。
            symbol: 設定與訂閱使用的商品代碼。

        Returns:
            保留交易所欄位並記錄本機接收時間的 TickEvent。
        """
        return cls(
            symbol=symbol,
            code=raw.code,
            timestamp=raw.timestamp,
            price=raw.price,
            volume=raw.volume,
            total_volume=raw.total_volume,
            tick_type=raw.tick_type,
            received_at=datetime.now(_TAIPEI),
        )
