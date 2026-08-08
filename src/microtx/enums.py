"""列舉型別定義。

集中管理全專案共用的列舉，避免各模組散落魔術字串（magic string）。
所有對外行為以本檔的列舉為準，僅在 broker 層轉換為 Shioaji 原生常數。
"""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """交易方向（策略層語意）。"""

    LONG = "LONG"
    """做多：向上觸價進場，停利在上、停損在下。"""

    SHORT = "SHORT"
    """做空：向下觸價進場，停利在下、停損在上。"""

    @property
    def sign(self) -> int:
        """方向係數：多單 +1、空單 -1。

        用於統一計算損益與停利停損價位，避免到處寫 if/else。

        Returns:
            多單回傳 ``1``，空單回傳 ``-1``。
        """
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        """反向（出場時使用）。"""
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class TriggerMode(str, Enum):
    """觸價判定模式。

    官方 TouchOrder 範例使用 ``price == trigger`` 精確相等比對，
    在跳空或快市時可能永遠不成立，本專案一律採用「穿越」判定。
    """

    CROSS_UP = "CROSS_UP"
    """由下往上穿越觸發價（成交價 >= trigger）。"""

    CROSS_DOWN = "CROSS_DOWN"
    """由上往下穿越觸發價（成交價 <= trigger）。"""


class OrderIntent(str, Enum):
    """委託意圖，決定 Shioaji ``octype`` 與價格策略。"""

    ENTRY = "ENTRY"
    """進場（新倉）。"""

    TAKE_PROFIT = "TAKE_PROFIT"
    """停利出場。"""

    STOP_LOSS = "STOP_LOSS"
    """停損出場。"""

    FORCE_CLOSE = "FORCE_CLOSE"
    """收盤前強制平倉，或風控觸發的緊急平倉。"""

    EMERGENCY = "EMERGENCY"
    """緊急平倉，繞過 RiskManager。"""


class PriceType(str, Enum):
    """委託價格別，對應 Shioaji FuturesPriceType。"""

    LMT = "LMT"
    """限價。"""

    MKP = "MKP"
    """範圍市價；本專案的市價首選，滑價有上限。"""

    MKT = "MKT"
    """市價；不建議使用，極端行情滑價無上限。"""


class TimeInForce(str, Enum):
    """委託時效，對應 Shioaji OrderType。"""

    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"


class CloseMode(str, Enum):
    """緊急平倉語意。"""

    FLATTEN = "FLATTEN"
    """平倉後繼續運行。"""

    PANIC = "PANIC"
    """平倉後維持停機。"""


class EventOrder(str, Enum):
    """立即成交時的回報送出順序。"""

    FILL_FIRST = "FILL_FIRST"
    """成交回報先於委託回報，為本專案預設值。"""

    ACK_FIRST = "ACK_FIRST"
    """委託回報先於成交回報。"""


class StrategyState(str, Enum):
    """單一策略實例的生命週期狀態機。

    狀態流轉::

        IDLE ──(啟動)──> ARMED ──(觸價)──> ENTRY_PENDING
          ▲                 │                    │
          │                 │(取消)              │(成交)
          │                 ▼                    ▼
          └──────────── CANCELLED            IN_POSITION
                                                  │(停利/停損/強平)
                                                  ▼
                                            EXIT_PENDING ──> CLOSED
    """

    IDLE = "IDLE"
    """尚未啟動。"""

    ARMED = "ARMED"
    """已武裝，正在監控行情等待觸價。"""

    ENTRY_PENDING = "ENTRY_PENDING"
    """進場委託已送出，等待成交回報。"""

    IN_POSITION = "IN_POSITION"
    """持倉中，監控停利停損。"""

    EXIT_PENDING = "EXIT_PENDING"
    """出場委託已送出，等待成交回報。"""

    CLOSED = "CLOSED"
    """已平倉，本次交易結束。"""

    CANCELLED = "CANCELLED"
    """使用者或風控主動取消，未進場。"""

    ERROR = "ERROR"
    """發生不可恢復錯誤，需人工介入。"""

    @property
    def is_terminal(self) -> bool:
        """是否為終態（不再接收行情事件）。"""
        return self in {StrategyState.CLOSED, StrategyState.CANCELLED, StrategyState.ERROR}


class EngineState(str, Enum):
    """引擎整體狀態。"""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    HALTED = "HALTED"
    """被風控停機（例如觸及單日最大虧損），僅允許平倉不允許新倉。"""
    SHUTTING_DOWN = "SHUTTING_DOWN"


class SessionType(str, Enum):
    """交易時段。"""

    DAY = "DAY"
    """日盤 08:45–13:45。"""

    NIGHT = "NIGHT"
    """夜盤 15:00–次日 05:00。"""

    CLOSED = "CLOSED"
    """非交易時段。"""
