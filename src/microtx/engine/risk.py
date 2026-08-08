"""無狀態、無 I/O 的委託風控決策。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from microtx.broker.base import OrderRequest
from microtx.config import Settings
from microtx.engine.position import PositionSnapshot
from microtx.enums import EngineState, OrderIntent, SessionType
from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_CLOSE_ONLY_INTENTS = frozenset(
    {
        OrderIntent.TAKE_PROFIT,
        OrderIntent.STOP_LOSS,
        OrderIntent.FORCE_CLOSE,
        OrderIntent.EMERGENCY,
    }
)


@dataclass(frozen=True, slots=True)
class RiskContext:
    """風控決策所需的完整不可變輸入快照。"""

    now: datetime
    session: SessionType
    engine_state: EngineState
    position: PositionSnapshot
    realized_pnl_ntd: float
    total_pnl_ntd: float
    trade_count: int
    last_order_at: datetime | None
    price_limits: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """風控核准結果與人類可讀原因。"""

    approved: bool
    reason: str


class RiskManager:
    """只依 request 與 context 運算、不持有可變狀態的風控器。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check(self, request: OrderRequest, ctx: RiskContext) -> RiskDecision:
        """依固定優先序檢查委託。"""
        if request.intent is OrderIntent.EMERGENCY:
            return RiskDecision(True, "緊急平倉直接放行")
        is_close = request.intent in _CLOSE_ONLY_INTENTS
        is_entry = request.intent is OrderIntent.ENTRY
        if ctx.session is SessionType.CLOSED:
            return RiskDecision(False, f"非交易時段（目前 {ctx.session!s}）")
        if ctx.engine_state is EngineState.HALTED and not is_close:
            return RiskDecision(False, "引擎已停機，僅允許平倉")
        if ctx.total_pnl_ntd <= -self._settings.max_daily_loss and not is_close:
            return RiskDecision(False, f"已達單日停損 {self._settings.max_daily_loss:,.0f} 元")
        if ctx.trade_count >= self._settings.max_daily_trades and is_entry:
            return RiskDecision(False, f"已達單日交易上限 {self._settings.max_daily_trades} 筆")
        if is_entry and self._projected_position(request, ctx) > self._settings.max_position_size:
            return RiskDecision(False, f"將超過最大持倉 {self._settings.max_position_size} 口")
        if is_entry and ctx.last_order_at is not None:
            elapsed = (ctx.now - ctx.last_order_at).total_seconds()
            remaining = self._settings.order_cooldown_sec - elapsed
            if remaining > 0:
                return RiskDecision(False, f"下單節流中，剩餘 {remaining:.1f} 秒")
        if request.price is not None and ctx.price_limits is not None:
            down, up = ctx.price_limits
            if request.price < down:
                return RiskDecision(False, f"委託價 {request.price:g} 超出跌停 {down:g}")
            if request.price > up:
                return RiskDecision(False, f"委託價 {request.price:g} 超出漲停 {up:g}")
        elif ctx.price_limits is None:
            logger.debug("尚無漲跌停資料，略過委託價格限制檢查")
        return RiskDecision(True, "風控檢查通過")

    def should_halt(self, ctx: RiskContext) -> tuple[bool, str]:
        """判斷當日總損益是否已達強制停機門檻。"""
        if ctx.total_pnl_ntd <= -self._settings.max_daily_loss:
            return True, f"當日總損益已達停損 {self._settings.max_daily_loss:,.0f} 元"
        return False, "尚未達單日停損"

    @staticmethod
    def _projected_position(request: OrderRequest, ctx: RiskContext) -> int:
        current = ctx.position.quantity * (
            ctx.position.direction.sign if ctx.position.direction is not None else 0
        )
        requested = request.quantity * request.action.sign
        return abs(current + requested)
