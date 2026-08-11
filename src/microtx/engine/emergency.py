"""不信任引擎內部狀態的緊急平倉安全路徑。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, RLock
from zoneinfo import ZoneInfo

from microtx.broker.base import (
    BrokerGateway,
    OrderAck,
    OrderRequest,
    Position,
    new_client_id,
)
from microtx.config import Settings
from microtx.engine.order_router import OrderRouter
from microtx.engine.position import PositionTracker
from microtx.enums import (
    CloseMode,
    Direction,
    EngineState,
    NotifyLevel,
    OrderIntent,
    PriceType,
    TimeInForce,
)
from microtx.notify.base import Notifier
from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")
_PENDING_NOTE = "非交易時段，已排入下次開盤執行"
_MANUAL_CLOSE_NOTE = "券商重連失敗，請立即至下單軟體手動平倉"
_UNLOCKED_NOTE = "未取得共用鎖，以無鎖模式執行"


@dataclass(frozen=True, slots=True)
class CloseReport:
    """一次緊急平倉操作的完整稽核結果。"""

    mode: CloseMode
    trigger_source: str
    triggered_at: datetime
    cancelled_orders: int
    positions_before: tuple[Position, ...]
    orders_sent: tuple[OrderAck, ...]
    residual_positions: tuple[Position, ...]
    succeeded: bool
    elapsed_sec: float
    notes: tuple[str, ...] = ()


class EmergencyCloser:
    """以券商部位為準、繞過風控且具有限重試的 kill switch。"""

    def __init__(
        self,
        gateway: BrokerGateway,
        router: OrderRouter,
        tracker: PositionTracker,
        settings: Settings,
        *,
        lock: RLock,
        on_state_change: Callable[[EngineState], None],
        on_cancel_strategies: Callable[[str], None],
        is_tradable: Callable[[], bool],
        notifier: Notifier | None = None,
    ) -> None:
        self._gateway = gateway
        self._router = router
        self._tracker = tracker
        self._settings = settings
        self._lock = lock
        self._on_state_change = on_state_change
        self._on_cancel_strategies = on_cancel_strategies
        self._is_tradable = is_tradable
        self._notifier = notifier
        self._reentry_lock = Lock()
        self._is_closing = False
        self._pending: CloseMode | None = None
        self._last_report: CloseReport | None = None
        self._lock_bypassed = False

    @property
    def is_closing(self) -> bool:
        """回傳是否已有平倉流程正在執行。"""
        with self._reentry_lock:
            return self._is_closing

    @property
    def pending(self) -> CloseMode | None:
        """回傳休市期間暫存、等待開盤執行的模式。"""
        with self._reentry_lock:
            return self._pending

    def execute(self, mode: CloseMode, source: str) -> CloseReport:
        """執行緊急平倉；所有失敗皆轉為報告，絕不向外拋例外。"""
        triggered_at = datetime.now(_TAIPEI)
        started_at = time.monotonic()
        with self._reentry_lock:
            if self._is_closing:
                logger.warning("緊急平倉已在執行中，忽略重複觸發 source=%s", source)
                return self._last_report or self._empty_report(
                    mode, source, triggered_at, started_at, "緊急平倉已在執行中"
                )
            self._is_closing = True
            self._lock_bypassed = False

        report: CloseReport | None = None
        try:
            report = self._execute(mode, source, triggered_at, started_at)
        except Exception as exc:
            logger.exception("緊急平倉內部失敗 source=%s", source)
            residual = self._safe_list_positions()
            report = CloseReport(
                mode,
                source,
                triggered_at,
                0,
                (),
                (),
                residual,
                False,
                time.monotonic() - started_at,
                (f"緊急平倉內部失敗：{exc}",),
            )
            self._finish_strategies_and_state(mode, source)
            self._notify_report(report, NotifyLevel.CRITICAL)
        finally:
            with self._reentry_lock:
                if report is not None:
                    self._last_report = report
                self._is_closing = False
        if report is None:  # pragma: no cover - finally 前所有路徑皆會建立報告
            return self._empty_report(mode, source, triggered_at, started_at, "未知錯誤")
        return report

    def _execute(
        self, mode: CloseMode, source: str, triggered_at: datetime, started_at: float
    ) -> CloseReport:
        # 先鎖門；callback 在共享券商鎖外執行，避免外部程式進入鎖區。
        self._on_state_change(EngineState.HALTED)
        if not self._tradable_or_fail_open():
            self._on_cancel_strategies(source)
            with self._reentry_lock:
                self._pending = mode
            report = CloseReport(
                mode,
                source,
                triggered_at,
                0,
                (),
                (),
                (),
                False,
                time.monotonic() - started_at,
                (_PENDING_NOTE,),
            )
            logger.warning("%s source=%s", _PENDING_NOTE, source)
            self._notify_report(report, NotifyLevel.WARNING)
            return report

        with self._reentry_lock:
            self._pending = None
        if not self._ensure_connected():
            report = self._empty_report(mode, source, triggered_at, started_at, _MANUAL_CLOSE_NOTE)
            self._finish_strategies_and_state(mode, source)
            logger.critical("%s source=%s", _MANUAL_CLOSE_NOTE, source)
            self._notify_report(report, NotifyLevel.CRITICAL)
            return report

        # 核心安全順序：先撤掉所有殘留委託，才允許送出第一張平倉單；否則殘單
        # 事後成交可能讓已平部位反向。撤單失敗仍繼續平倉，以有界風險換掉裸露風險。
        with self._emergency_lock() as locked:
            pass
        cancelled = self._router.cancel_all() if locked else self._gateway.cancel_all_orders()
        positions = self._list_positions()
        positions_before = tuple(positions)
        notes = tuple(self._tracker.reconcile(positions))
        if self._lock_bypassed:
            notes = (*notes, _UNLOCKED_NOTE)
        if notes:
            logger.warning("緊急平倉發現部位不同步：%s", "；".join(notes))

        acknowledgements: list[OrderAck] = []
        for attempt in range(1, self._settings.emergency_max_retries + 1):
            if not positions:
                break
            for position in positions:
                try:
                    request = self._close_request(position)
                    acknowledgements.append(self._router.submit_unchecked(request))
                except Exception as exc:
                    logger.exception("緊急平倉委託失敗 attempt=%d code=%s", attempt, position.code)
                    notes = (*notes, f"{position.code} 第 {attempt} 輪送單失敗：{exc}")
            time.sleep(self._settings.emergency_retry_interval_sec)
            positions = self._list_positions()

        residual = tuple(positions)
        succeeded = not residual
        if self._lock_bypassed and _UNLOCKED_NOTE not in notes:
            notes = (*notes, _UNLOCKED_NOTE)
        report = CloseReport(
            mode,
            source,
            triggered_at,
            cancelled,
            positions_before,
            tuple(acknowledgements),
            residual,
            succeeded,
            time.monotonic() - started_at,
            notes,
        )
        if succeeded:
            logger.info("緊急平倉完成 mode=%s source=%s", mode.value, source)
        else:
            logger.critical("緊急平倉未完成，殘餘部位=%s", residual)
        self._finish_strategies_and_state(mode, source)
        self._notify_report(report, NotifyLevel.INFO if succeeded else NotifyLevel.CRITICAL)
        return report

    def _tradable_or_fail_open(self) -> bool:
        try:
            return self._is_tradable()
        except Exception:
            logger.exception("交易時段判定失敗，依安全原則視為可交易並繼續平倉")
            return True

    def _ensure_connected(self) -> bool:
        try:
            if self._gateway.is_connected:
                return True
        except Exception:
            logger.exception("讀取券商連線狀態失敗，嘗試重連")
        for attempt in range(1, 4):
            try:
                self._gateway.connect()
                if self._gateway.is_connected:
                    return True
            except Exception:
                logger.exception("券商重連失敗 attempt=%d/3", attempt)
        return False

    def _list_positions(self) -> list[Position]:
        with self._emergency_lock():
            return self._gateway.list_positions()

    @contextmanager
    def _emergency_lock(self) -> Iterator[bool]:
        """有界取得共享鎖；逾時後切換為無鎖模式並繼續救援。"""
        if self._lock_bypassed:
            yield False
            return
        acquired = self._lock.acquire(timeout=self._settings.emergency_lock_timeout_sec)
        if not acquired:
            self._lock_bypassed = True
            logger.critical(
                "緊急平倉無法在 %.1f 秒內取得共用鎖，判定有其他路徑卡住，改以無鎖模式強制繼續平倉",
                self._settings.emergency_lock_timeout_sec,
            )
        # 等鎖到底會讓持倉風險無界；逾時後併發送單的風險有界，且另有
        # close-only 夾擠與殘餘成交補償兜底，因此 kill switch 必須 fail-open。
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def _safe_list_positions(self) -> tuple[Position, ...]:
        try:
            return tuple(self._list_positions())
        except Exception:
            logger.exception("失敗報告無法取得券商殘餘部位")
            return ()

    def _close_request(self, position: Position) -> OrderRequest:
        action = position.direction.opposite
        price_type = PriceType.MKP
        price: float | None = None
        if not self._settings.emergency_use_market_order:
            try:
                limit_down, limit_up = self._gateway.get_price_limits(position.code)
                price_type = PriceType.LMT
                price = limit_up if action is Direction.LONG else limit_down
            except Exception:
                logger.exception("無法取得漲跌停價 code=%s，降級使用 MKP", position.code)
        return OrderRequest(
            symbol=position.code,
            action=action,
            quantity=position.quantity,
            price=price,
            price_type=price_type,
            time_in_force=TimeInForce.IOC,
            intent=OrderIntent.EMERGENCY,
            client_id=new_client_id(),
        )

    def _finish_strategies_and_state(self, mode: CloseMode, source: str) -> None:
        try:
            self._on_cancel_strategies(source)
        except Exception:
            logger.exception("取消策略失敗 source=%s", source)
        if mode is CloseMode.FLATTEN:
            try:
                self._on_state_change(EngineState.RUNNING)
            except Exception:
                logger.exception("恢復引擎 RUNNING 狀態失敗")

    def _notify_report(self, report: CloseReport, level: NotifyLevel) -> None:
        if self._notifier is None:
            return
        title = f"緊急平倉 {report.mode.value}：{'完成' if report.succeeded else '未完成'}"
        residual = sum(position.quantity for position in report.residual_positions)
        body = (
            f"來源={report.trigger_source}，撤單={report.cancelled_orders}，"
            f"送單={len(report.orders_sent)}，殘餘口數={residual}"
        )
        if report.notes:
            body = f"{body}；{'；'.join(report.notes)}"
        try:
            self._notifier.notify(level, title, body)
        except Exception:
            logger.exception("緊急平倉通知失敗，但不影響平倉結果")

    @staticmethod
    def _empty_report(
        mode: CloseMode,
        source: str,
        triggered_at: datetime,
        started_at: float,
        note: str,
    ) -> CloseReport:
        return CloseReport(
            mode,
            source,
            triggered_at,
            0,
            (),
            (),
            (),
            False,
            time.monotonic() - started_at,
            (note,),
        )
