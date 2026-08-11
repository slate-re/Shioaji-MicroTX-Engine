"""具冪等、風控、撤單補償與共享鎖的下單路由。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from zoneinfo import ZoneInfo

from microtx.broker.base import (
    AckEvent,
    BrokerGateway,
    CancelEvent,
    FillEvent,
    OrderAck,
    OrderEvent,
    OrderRequest,
    RejectEvent,
    new_client_id,
)
from microtx.engine.risk import RiskContext, RiskManager
from microtx.enums import OrderIntent, PriceType, TimeInForce
from microtx.exceptions import BrokerError
from microtx.utils.logger import get_logger
from microtx.utils.retry import retry

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")
_CLOSE_ONLY_INTENTS = frozenset(
    {
        OrderIntent.TAKE_PROFIT,
        OrderIntent.STOP_LOSS,
        OrderIntent.FORCE_CLOSE,
        OrderIntent.EMERGENCY,
    }
)


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """指定策略進場單的盡力撤單結果。"""

    cancelled: tuple[str, ...]
    abandoned: tuple[str, ...]


class OrderRouter:
    """所有一般與緊急下單操作的單一互斥出口。"""

    def __init__(self, gateway: BrokerGateway, *, risk: RiskManager, lock: RLock) -> None:
        self._gateway = gateway
        self._risk = risk
        self._lock = lock
        self._in_flight: dict[str, OrderRequest] = {}
        self._completed: set[str] = set()
        self._filled_quantities: dict[str, int] = {}
        self._order_no_cache: dict[str, str] = {}
        self._abandoned_entries: dict[str, OrderRequest] = {}
        self._abandoned_filled: dict[str, int] = {}
        self._last_submit_at: datetime | None = None

    @property
    def in_flight(self) -> dict[str, OrderRequest]:
        """回傳在途委託的防禦性複本。"""
        with self._lock:
            return dict(self._in_flight)

    @property
    def last_order_at(self) -> datetime | None:
        """回傳最近一次券商接受委託的時間。"""
        with self._lock:
            return self._last_submit_at

    def submit(self, request: OrderRequest, ctx: RiskContext) -> OrderAck:
        """經風控核准後送出一般委託。"""
        # 漲跌停查詢可能是網路 I/O，絕不可占用共享鎖。
        price_limits = self._get_price_limits(request.symbol)
        with self._lock:
            if self._is_duplicate(request.client_id):
                return OrderAck(request.client_id, None, False, "重複的 client_id")
            checked_ctx = replace(
                ctx,
                last_order_at=self._last_submit_at,
                price_limits=price_limits,
            )
            decision = self._risk.check(request, checked_ctx)
            if not decision.approved:
                logger.warning("風控拒絕委託 client_id=%s：%s", request.client_id, decision.reason)
                return OrderAck(request.client_id, None, False, decision.reason)
        if request.intent in _CLOSE_ONLY_INTENTS:
            self._cancel_working_entries(request.strategy_id)
        return self._reserve_submit_and_finalize(request, accepted_at=ctx.now)

    def submit_unchecked(self, request: OrderRequest) -> OrderAck:
        """僅限 EmergencyCloser 緊急平倉；其他呼叫者一律使用 submit。"""
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            # 緊急平倉不可因共用鎖被故障路徑占用而失效；此時犧牲 Router 帳本的
            # 完整性，直接送往券商。券商部位才是 kill switch 的真相來源。
            logger.critical("緊急委託未取得共用鎖，以無鎖模式直接送往券商")
            return self._place_gateway(request)
        try:
            if self._is_duplicate(request.client_id):
                return OrderAck(request.client_id, None, False, "重複的 client_id")
            self._in_flight[request.client_id] = request
        finally:
            self._lock.release()
        try:
            ack = self._place_gateway(request)
        except BrokerError:
            if self._lock.acquire(blocking=False):
                try:
                    self._in_flight.pop(request.client_id, None)
                finally:
                    self._lock.release()
            raise
        if self._lock.acquire(blocking=False):
            try:
                self._finalize_ack_locked(request, ack, datetime.now(_TAIPEI))
            finally:
                self._lock.release()
        else:
            logger.critical("緊急委託回應無法取得共用鎖，略過 Router 帳本更新")
        return ack

    def cancel(self, broker_order_id: str) -> bool:
        """在共享鎖外重試取消指定委託。"""
        return self._cancel_gateway(broker_order_id)

    def cancel_all(self) -> int:
        """在共享鎖外取消全部委託，再於鎖內清理帳本。"""
        count = self._gateway.cancel_all_orders()
        with self._lock:
            self._completed.update(self._in_flight)
            self._in_flight.clear()
            self._filled_quantities.clear()
            self._abandoned_entries.clear()
            self._abandoned_filled.clear()
            return count

    def on_event(self, event: OrderEvent) -> None:
        """更新委託帳本，並補償撤銷失敗後成交的殘餘進場單。"""
        compensation: OrderRequest | None = None
        with self._lock:
            client_id = event.client_id
            if isinstance(event, AckEvent) and client_id is not None:
                self._order_no_cache[client_id] = event.broker_order_id
            if isinstance(event, FillEvent) and client_id is not None:
                self._record_fill_locked(event)
                if client_id in self._abandoned_entries:
                    compensation = self._prepare_compensation_locked(event)
            elif isinstance(event, (RejectEvent, CancelEvent)) and client_id is not None:
                if isinstance(event, RejectEvent) or event.remaining_quantity == 0:
                    self._mark_completed_locked(client_id)
                    self._abandoned_entries.pop(client_id, None)
                    self._abandoned_filled.pop(client_id, None)
        if compensation is not None:
            logger.critical(
                "殘餘進場委託成交 client_id=%s 口數=%d，立即反向平倉",
                client_id,
                compensation.quantity,
            )
            self.submit_unchecked(compensation)
        self._retry_abandoned()

    def _is_duplicate(self, client_id: str) -> bool:
        return client_id in self._in_flight or client_id in self._completed

    def _reserve_submit_and_finalize(
        self, request: OrderRequest, *, accepted_at: datetime
    ) -> OrderAck:
        with self._lock:
            if self._is_duplicate(request.client_id):
                return OrderAck(request.client_id, None, False, "重複的 client_id")
            # 網路呼叫前先登記，讓同 client_id 的併發請求仍受冪等閘門保護。
            self._in_flight[request.client_id] = request
        try:
            ack = self._place_gateway(request)
        except BrokerError:
            with self._lock:
                self._in_flight.pop(request.client_id, None)
            raise
        orphaned = False
        with self._lock:
            orphaned = request.client_id not in self._in_flight
            if not orphaned:
                self._finalize_ack_locked(request, ack, accepted_at)
        if orphaned and ack.accepted and request.intent is OrderIntent.ENTRY:
            self._neutralize_late_entry(request, ack)
        return ack

    def _neutralize_late_entry(self, request: OrderRequest, ack: OrderAck) -> None:
        """補償跨越全撤屏障後才回應的進場單。"""
        cancelled = False
        if ack.broker_order_id is not None:
            try:
                cancelled = self._cancel_gateway(ack.broker_order_id)
            except BrokerError as exc:
                logger.critical("取消逾期進場單失敗 client_id=%s：%s", request.client_id, exc)
        if cancelled:
            return
        logger.critical("逾期進場單可能已成交 client_id=%s，立即送反向緊急平倉", request.client_id)
        compensation = OrderRequest(
            symbol=request.symbol,
            action=request.action.opposite,
            quantity=request.quantity,
            price=None,
            price_type=PriceType.MKP,
            time_in_force=TimeInForce.IOC,
            intent=OrderIntent.EMERGENCY,
            client_id=new_client_id(),
        )
        self.submit_unchecked(compensation)

    def _finalize_ack_locked(
        self, request: OrderRequest, ack: OrderAck, accepted_at: datetime
    ) -> None:
        if ack.accepted:
            self._last_submit_at = accepted_at
        else:
            self._mark_completed_locked(request.client_id)
        if ack.broker_order_id is not None:
            self._order_no_cache[request.client_id] = ack.broker_order_id

    @retry(attempts=3, exceptions=(BrokerError,))
    def _place_gateway(self, request: OrderRequest) -> OrderAck:
        return self._gateway.place_order(request)

    @retry(attempts=3, exceptions=(BrokerError,))
    def _cancel_gateway(self, broker_order_id: str) -> bool:
        return self._gateway.cancel_order(broker_order_id)

    def _get_price_limits(self, symbol: str) -> tuple[float, float] | None:
        try:
            return self._gateway.get_price_limits(symbol)
        except BrokerError as exc:
            logger.debug("無法取得漲跌停資料，略過價格檢查：%s", exc)
            return None

    def _cancel_working_entries(self, strategy_id: str) -> CancelOutcome:
        if not strategy_id:
            return CancelOutcome((), ())
        with self._lock:
            targets = [
                request
                for request in self._in_flight.values()
                if request.strategy_id == strategy_id and request.intent is OrderIntent.ENTRY
            ]
            cached_ids = {
                request.client_id: self._order_no_cache.get(request.client_id)
                for request in targets
            }
        open_orders = self._list_open_orders_by_client()
        cancelled: list[str] = []
        abandoned: list[str] = []
        for request in targets:
            broker_order_id = cached_ids[request.client_id]
            if broker_order_id is None:
                broker_order_id = open_orders.get(request.client_id)
            succeeded = False
            if broker_order_id is not None:
                try:
                    succeeded = self._cancel_gateway(broker_order_id)
                except BrokerError as exc:
                    logger.warning("撤銷進場委託失敗 client_id=%s：%s", request.client_id, exc)
            if succeeded:
                cancelled.append(request.client_id)
                with self._lock:
                    self._mark_completed_locked(request.client_id)
            else:
                abandoned.append(request.client_id)
                with self._lock:
                    self._abandoned_entries[request.client_id] = request
                    self._abandoned_filled.setdefault(
                        request.client_id, self._filled_quantities.get(request.client_id, 0)
                    )
        return CancelOutcome(tuple(cancelled), tuple(abandoned))

    def _list_open_orders_by_client(self) -> dict[str, str]:
        try:
            orders = self._gateway.list_open_orders()
        except BrokerError as exc:
            logger.warning("查詢未成交委託失敗，改走補償機制：%s", exc)
            return {}
        return {
            order.client_id: order.broker_order_id
            for order in orders
            if order.client_id is not None
        }

    def _retry_abandoned(self) -> None:
        with self._lock:
            targets = tuple(
                (client_id, self._order_no_cache.get(client_id))
                for client_id in self._abandoned_entries
            )
        for client_id, broker_order_id in targets:
            if broker_order_id is None:
                continue
            try:
                succeeded = self._cancel_gateway(broker_order_id)
            except BrokerError as exc:
                logger.warning("重試撤銷 abandoned 進場單失敗 client_id=%s：%s", client_id, exc)
                continue
            if succeeded:
                with self._lock:
                    self._abandoned_entries.pop(client_id, None)

    def _record_fill_locked(self, event: FillEvent) -> None:
        if event.client_id is None:
            return
        total = self._filled_quantities.get(event.client_id, 0) + event.quantity
        self._filled_quantities[event.client_id] = total
        request = self._in_flight.get(event.client_id)
        if request is not None and total >= request.quantity:
            self._mark_completed_locked(event.client_id)

    def _prepare_compensation_locked(self, event: FillEvent) -> OrderRequest | None:
        if event.client_id is None:
            return None
        request = self._abandoned_entries[event.client_id]
        compensation = OrderRequest(
            symbol=request.symbol,
            action=event.action.opposite,
            quantity=event.quantity,
            price=None,
            price_type=PriceType.MKP,
            time_in_force=TimeInForce.IOC,
            intent=OrderIntent.EMERGENCY,
            client_id=new_client_id(),
        )
        filled = self._abandoned_filled.get(event.client_id, 0) + event.quantity
        self._abandoned_filled[event.client_id] = filled
        if filled >= request.quantity:
            self._abandoned_entries.pop(event.client_id, None)
        return compensation

    def _mark_completed_locked(self, client_id: str) -> None:
        self._in_flight.pop(client_id, None)
        self._filled_quantities.pop(client_id, None)
        self._completed.add(client_id)
