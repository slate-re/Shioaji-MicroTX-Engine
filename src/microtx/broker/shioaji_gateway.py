"""Shioaji 1.7+ 的期貨券商閘道實作。"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from microtx.broker._mapping import (
    _require_shioaji,
    from_shioaji_action,
    infer_cancel_reason,
    to_shioaji_action,
    to_shioaji_octype,
    to_shioaji_price_type,
    to_shioaji_time_in_force,
)
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
from microtx.config import Settings
from microtx.enums import PriceType
from microtx.exceptions import BrokerError, ConnectionLostError
from microtx.utils.logger import get_logger

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")
_OPEN_STATUSES = frozenset({"Submitted", "PreSubmitted", "PartFilled"})
_DISCONNECTED_CODES = frozenset({1, 2, 12})
_RESUBSCRIBE_CODES = frozenset({13, 17})
_INFO_TTL_SEC = 60.0


class _TickLike(Protocol):
    code: object
    datetime: object
    close: object
    volume: object
    total_volume: object
    tick_type: object
    simtrade: object


class _TradeLike(Protocol):
    status: Any
    order: Any
    contract: Any


class ShioajiGateway(BrokerGateway):
    """將 Shioaji SDK 隔離在 broker 邊界內。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sj = _require_shioaji()
        self._api: Any = self._sj.Shioaji(simulation=settings.simulation)
        self._connected = False
        self._state_lock = Lock()
        self._tick_callbacks: dict[str, Callable[[RawTick], None]] = {}
        self._order_event_callback: Callable[[OrderEvent], None] | None = None
        self._contract_cache: dict[str, object] = {}
        self._info_cache: dict[str, tuple[float, tuple[float, float]]] = {}
        self._client_id_map: dict[str, str] = {}
        self._request_map: dict[str, OrderRequest] = {}
        self._trade_cache: dict[str, _TradeLike] = {}
        self._pending_cancels: set[str] = set()
        self._dispatch_queue: queue.Queue[OrderEvent | int | None] = queue.Queue()
        self._dispatch_stop = Event()
        self._dispatch_thread: Thread | None = None

    @property
    def is_connected(self) -> bool:
        """回傳 SDK session 是否可接受新委託。"""
        with self._state_lock:
            return self._connected

    def connect(self) -> None:
        """登入、驗證期貨帳戶並註冊所有 SDK callback。"""
        try:
            self._api.login(
                api_key=self._settings.shioaji_api_key.get_secret_value(),
                secret_key=self._settings.shioaji_secret_key.get_secret_value(),
                subscribe_trade=True,
                receive_window=30_000,
            )
            account = self._api.futopt_account
            if account is None or account.signed is not True:
                raise BrokerError("期貨帳戶尚未完成 API 簽署，無法下單")
            if self._settings.is_live:
                ca_path = self._settings.shioaji_ca_path
                if ca_path is None:
                    raise BrokerError("實盤模式缺少憑證路徑")
                self._api.activate_ca(
                    ca_path=str(ca_path),
                    ca_passwd=self._settings.shioaji_ca_password.get_secret_value(),
                    person_id=self._settings.shioaji_person_id.get_secret_value(),
                )
            self._register_callbacks()
            self._start_dispatcher()
            with self._state_lock:
                self._connected = True
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError("Shioaji 登入失敗") from exc

    def disconnect(self) -> None:
        """停止分派並登出，避免占用有限的 Shioaji 連線數。"""
        with self._state_lock:
            self._connected = False
        try:
            self._api.logout()
        except Exception as exc:
            raise BrokerError("Shioaji 登出失敗") from exc
        finally:
            self._stop_dispatcher()

    def subscribe_ticks(self, symbol: str, callback: Callable[[RawTick], None]) -> None:
        """保存訂閱意圖並訂閱期貨 Tick。"""
        self._tick_callbacks[symbol] = callback
        self._subscribe_symbol(symbol)

    def unsubscribe_ticks(self, symbol: str) -> None:
        """取消期貨 Tick 訂閱。"""
        contract = self._resolve_contract(symbol)
        try:
            self._api.quote.unsubscribe(contract, quote_type=self._sj.QuoteType.Tick)
        except Exception as exc:
            raise BrokerError(f"取消行情訂閱失敗：{symbol}") from exc
        self._tick_callbacks.pop(symbol, None)

    def place_order(self, request: OrderRequest) -> OrderAck:
        """驗證漲跌停後送出期貨委託。"""
        if not self.is_connected:
            raise ConnectionLostError("Shioaji 連線中斷，暫停接受新委託")
        contract = self._resolve_contract(request.symbol)
        self._validate_price_limits(request)
        try:
            order = self._sj.FuturesOrder(
                action=to_shioaji_action(request.action),
                price=request.price or 0,
                quantity=request.quantity,
                price_type=to_shioaji_price_type(request.price_type),
                order_type=to_shioaji_time_in_force(request.time_in_force),
                octype=to_shioaji_octype(self._settings.futures_octype),
                account=self._api.futopt_account,
            )
            trade = self._api.place_order(contract, order, timeout=30_000)
            broker_order_id = str(trade.status.id)
            self._client_id_map[request.client_id] = broker_order_id
            self._request_map[request.client_id] = request
            self._trade_cache[broker_order_id] = trade
            status = self._enum_text(trade.status.status)
            accepted = status not in {"Failed", "Inactive"}
            return OrderAck(
                request.client_id, broker_order_id, accepted, str(trade.status.msg or "")
            )
        except (ValueError, BrokerError):
            raise
        except Exception as exc:
            raise BrokerError(f"Shioaji 下單失敗 client_id={request.client_id}") from exc

    def cancel_order(self, broker_order_id: str) -> bool:
        """更新狀態取得 ordno 後取消指定委託。"""
        trade = self._find_trade(broker_order_id)
        if trade is None:
            return False
        client_id = self._client_id_for(broker_order_id)
        try:
            self._api.update_status(self._api.futopt_account)
            if client_id is not None:
                self._pending_cancels.add(client_id)
            self._api.cancel_order(trade)
            return True
        except Exception as exc:
            if client_id is not None:
                self._pending_cancels.discard(client_id)
            raise BrokerError(f"Shioaji 刪單失敗 broker_order_id={broker_order_id}") from exc

    def cancel_all_orders(self) -> int:
        """盡力取消所有未完成委託；單筆失敗不影響其他委託。"""
        try:
            self._api.update_status(self._api.futopt_account)
            trades = tuple(self._api.list_trades())
        except Exception as exc:
            raise BrokerError("Shioaji 查詢未成交委託失敗") from exc
        cancelled = 0
        for trade in trades:
            if self._enum_text(trade.status.status) not in _OPEN_STATUSES:
                continue
            broker_order_id = str(trade.status.id)
            client_id = self._client_id_for(broker_order_id)
            try:
                if client_id is not None:
                    self._pending_cancels.add(client_id)
                self._api.cancel_order(trade)
                cancelled += 1
            except Exception as exc:
                if client_id is not None:
                    self._pending_cancels.discard(client_id)
                logger.warning("單筆刪單失敗 broker_order_id=%s：%s", broker_order_id, exc)
        return cancelled

    def list_open_orders(self) -> list[OpenOrder]:
        """更新狀態並將所有未完成 Trade 轉為 OpenOrder。"""
        try:
            self._api.update_status(self._api.futopt_account)
            trades = self._api.list_trades()
        except Exception as exc:
            raise BrokerError("Shioaji 查詢未成交委託失敗") from exc
        results: list[OpenOrder] = []
        for trade in trades:
            if self._enum_text(trade.status.status) not in _OPEN_STATUSES:
                continue
            broker_order_id = str(trade.status.id)
            self._trade_cache[broker_order_id] = trade
            results.append(
                OpenOrder(
                    broker_order_id,
                    self._client_id_for(broker_order_id),
                    str(trade.contract.code),
                    from_shioaji_action(trade.order.action),
                    float(trade.order.price),
                    int(trade.status.order_quantity),
                    int(trade.status.deal_quantity),
                )
            )
        return results

    def list_positions(self) -> list[Position]:
        """將期貨帳戶的真實部位逐欄轉為 Position。"""
        try:
            positions = self._api.list_positions(account=self._api.futopt_account, timeout=5000)
            return [
                Position(
                    str(position.code),
                    from_shioaji_action(position.direction),
                    int(position.quantity),
                    float(position.price),
                    float(position.pnl),
                )
                for position in positions
            ]
        except Exception as exc:
            raise BrokerError("Shioaji 查詢期貨部位失敗") from exc

    def set_order_event_callback(self, callback: Callable[[OrderEvent], None]) -> None:
        """設定由 gateway dispatcher 執行的委託事件 callback。"""
        self._order_event_callback = callback

    def get_price_limits(self, symbol: str) -> tuple[float, float]:
        """以 60 秒 TTL 快取 FuturesInfo 的跌停價與漲停價。"""
        cached = self._info_cache.get(symbol)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _INFO_TTL_SEC:
            return cached[1]
        contract = self._resolve_contract(symbol)
        try:
            info = self._api.contracts.info(contract)
            limits = (float(info.limit_down), float(info.limit_up))
        except Exception as exc:
            raise BrokerError(f"Shioaji 查詢漲跌停失敗：{symbol}") from exc
        self._info_cache[symbol] = (now, limits)
        return limits

    @staticmethod
    def raw_tick_from_sdk(tick: _TickLike) -> RawTick:
        """只搬運 TickFOPv1 欄位，保留 simtrade 並統一台北時區。"""
        timestamp = ShioajiGateway._as_datetime(tick.datetime)
        return RawTick(
            str(tick.code),
            timestamp,
            float(cast(Any, tick.close)),
            int(cast(Any, tick.volume)),
            int(cast(Any, tick.total_volume)),
            int(cast(Any, tick.tick_type)),
            bool(tick.simtrade),
        )

    def _register_callbacks(self) -> None:
        self._api.quote.set_on_tick_fop_v1_callback(self._on_tick)
        self._api.set_order_callback(self._order_callback)
        self._api.quote.set_event_callback(self._event_callback)

    def _on_tick(self, exchange: object, tick: _TickLike) -> None:
        del exchange
        raw = self.raw_tick_from_sdk(tick)
        callback = self._tick_callbacks.get(raw.code)
        if callback is None and len(self._tick_callbacks) == 1:
            callback = next(iter(self._tick_callbacks.values()))
        if callback is not None:
            callback(raw)

    def _order_callback(self, state: object, msg: dict[str, Any]) -> None:
        try:
            event = self._convert_order_event(state, msg)
        except Exception:
            logger.exception("無法轉換 Shioaji 委託回報")
            return
        if event is not None:
            self._dispatch_queue.put_nowait(event)

    def _event_callback(self, resp_code: int, event_code: int, info: str, event: str) -> None:
        del resp_code, info, event
        if event_code == 0:
            with self._state_lock:
                self._connected = True
        elif event_code in _DISCONNECTED_CODES:
            with self._state_lock:
                self._connected = False
        elif event_code in _RESUBSCRIBE_CODES:
            with self._state_lock:
                self._connected = True
            self._dispatch_queue.put_nowait(event_code)
        elif event_code == 16:
            logger.debug("Shioaji 行情訂閱成功")

    def _convert_order_event(self, state: object, msg: dict[str, Any]) -> OrderEvent | None:
        if state == self._sj.OrderState.FuturesDeal:
            broker_order_id = str(msg["trade_id"])
            client_id = self._client_id_for(broker_order_id)
            if client_id is None:
                logger.warning("成交回報早於委託對映 broker_order_id=%s", broker_order_id)
            return FillEvent(
                client_id,
                broker_order_id,
                str(msg["code"]),
                from_shioaji_action(msg["action"]),
                float(msg["price"]),
                int(msg["quantity"]),
                self._as_datetime(msg["ts"]),
            )
        if state != self._sj.OrderState.FuturesOrder:
            return None
        operation = msg["operation"]
        order = msg["order"]
        status = msg["status"]
        contract = msg["contract"]
        broker_order_id = str(order["id"])
        client_id = self._client_id_for(broker_order_id)
        timestamp = self._as_datetime(status.get("exchange_ts", datetime.now(_TAIPEI)))
        if str(operation["op_code"]) != "00":
            return RejectEvent(
                client_id,
                broker_order_id,
                str(contract["code"]),
                str(operation.get("op_msg", "")),
                timestamp,
            )
        op_type = self._enum_text(operation["op_type"])
        if op_type == "New":
            return AckEvent(
                client_id,
                broker_order_id,
                str(order["ordno"]),
                str(contract["code"]),
                from_shioaji_action(order["action"]),
                float(order["price"]),
                int(order["quantity"]),
                timestamp,
            )
        if op_type == "Cancel":
            request = self._request_for(client_id)
            reason = infer_cancel_reason(client_id, self._pending_cancels, request)
            if client_id is not None:
                self._pending_cancels.discard(client_id)
            cancelled = int(status["cancel_quantity"])
            remaining = (
                int(status["order_quantity"]) - cancelled - int(status.get("deal_quantity", 0))
            )
            return CancelEvent(
                client_id,
                broker_order_id,
                str(contract["code"]),
                cancelled,
                max(remaining, 0),
                timestamp,
                reason,
            )
        return None

    def _subscribe_symbol(self, symbol: str) -> None:
        if not self.is_connected:
            raise ConnectionLostError("Shioaji 連線中斷，無法訂閱行情")
        contract = self._resolve_contract(symbol)
        try:
            self._api.quote.subscribe(contract, quote_type=self._sj.QuoteType.Tick)
        except Exception as exc:
            raise BrokerError(f"行情訂閱失敗：{symbol}") from exc

    def _resubscribe_all(self) -> None:
        for symbol in tuple(self._tick_callbacks):
            try:
                self._subscribe_symbol(symbol)
            except BrokerError as exc:
                logger.warning("重連後恢復行情訂閱失敗 symbol=%s：%s", symbol, exc)

    def _resolve_contract(self, symbol: str) -> object:
        cached = self._contract_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            contract = self._api.contracts.get(symbol)
        except Exception as exc:
            raise BrokerError(f"Shioaji 商品解析失敗：{symbol}") from exc
        if contract is None:
            raise BrokerError(f"找不到 Shioaji 商品：{symbol}")
        self._contract_cache[symbol] = contract
        return contract

    def _validate_price_limits(self, request: OrderRequest) -> None:
        if request.price_type is not PriceType.LMT or request.price is None:
            return
        limit_down, limit_up = self.get_price_limits(request.symbol)
        if not limit_down <= request.price <= limit_up:
            raise BrokerError(
                f"委託價 {request.price:g} 超出漲跌停範圍 {limit_down:g}–{limit_up:g}"
            )

    def _find_trade(self, broker_order_id: str) -> _TradeLike | None:
        cached = self._trade_cache.get(broker_order_id)
        if cached is not None:
            return cached
        try:
            trades = self._api.list_trades()
        except Exception as exc:
            raise BrokerError("Shioaji 查詢委託失敗") from exc
        for trade in trades:
            trade_id = str(trade.status.id)
            self._trade_cache[trade_id] = trade
            if trade_id == broker_order_id:
                return cast(_TradeLike, trade)
        return None

    def _client_id_for(self, broker_order_id: str) -> str | None:
        return next(
            (
                client_id
                for client_id, mapped_id in self._client_id_map.items()
                if mapped_id == broker_order_id
            ),
            None,
        )

    def _request_for(self, client_id: str | None) -> OrderRequest | None:
        return self._request_map.get(client_id) if client_id is not None else None

    def _start_dispatcher(self) -> None:
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return
        self._dispatch_stop.clear()
        self._dispatch_thread = Thread(
            target=self._dispatch_loop, name="shioaji-dispatcher", daemon=True
        )
        self._dispatch_thread.start()

    def _stop_dispatcher(self) -> None:
        self._dispatch_stop.set()
        self._dispatch_queue.put_nowait(None)
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=2.0)

    def _dispatch_loop(self) -> None:
        while not self._dispatch_stop.is_set():
            item = self._dispatch_queue.get()
            if item is None:
                return
            if isinstance(item, int):
                self._resubscribe_all()
                continue
            callback = self._order_event_callback
            if callback is not None:
                callback(item)

    @staticmethod
    def _as_datetime(value: object) -> datetime:
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=_TAIPEI) if value.tzinfo is None else value.astimezone(_TAIPEI)
            )
        numeric = float(cast(Any, value))
        if numeric > 10_000_000_000:
            numeric /= 1_000_000_000
        return datetime.fromtimestamp(numeric, _TAIPEI)

    @staticmethod
    def _enum_text(value: object) -> str:
        raw = getattr(value, "value", value)
        return str(raw).split(".")[-1]
