"""交易引擎的生命週期、worker 與緊急路徑協調器。"""

from __future__ import annotations

import os
import queue
import signal
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock, RLock, Thread, current_thread
from zoneinfo import ZoneInfo

from microtx.broker.base import (
    BrokerGateway,
    FillEvent,
    OrderEvent,
    OrderRequest,
    RejectEvent,
    new_client_id,
)
from microtx.config import Settings
from microtx.engine.daily_state import DailyState, DailyStateStore
from microtx.engine.emergency import CloseReport, EmergencyCloser
from microtx.engine.order_router import OrderRouter
from microtx.engine.position import PositionTracker
from microtx.engine.risk import RiskContext, RiskManager
from microtx.engine.scheduler import Scheduler
from microtx.engine.status import StatusSnapshot, StatusWriter, snapshot_time
from microtx.engine.trading_day import trading_date
from microtx.enums import (
    CloseMode,
    EngineState,
    LoadOutcome,
    NotifyLevel,
    PriceType,
    TimeInForce,
)
from microtx.exceptions import MicroTXError, StrategyError
from microtx.market.feed import MarketFeed
from microtx.notify.base import Notifier
from microtx.strategies.base import Signal, Strategy
from microtx.utils.logger import get_logger
from microtx.utils.pidfile import PidFile

logger = get_logger(__name__)
_TAIPEI = ZoneInfo("Asia/Taipei")


class TradingEngine:
    """串接行情、策略、風控、下單與獨立 kill switch worker。"""

    def __init__(
        self,
        settings: Settings,
        gateway: BrokerGateway,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._state = EngineState.STOPPED
        self._state_lock = Lock()
        self._shared_lock = RLock()
        self._risk = RiskManager(settings)
        self._tracker = PositionTracker(settings.spec)
        self._daily_store = DailyStateStore(
            settings.daily_state_file, boundary=settings.trading_day_boundary
        )
        self._daily_state_writable = True
        self._notifier = notifier
        self._router = OrderRouter(gateway, risk=self._risk, lock=self._shared_lock)
        self._feed = MarketFeed(gateway, symbol=settings.symbol)
        self._scheduler = Scheduler(
            settings,
            on_force_close=self._scheduled_flatten,
            on_reset_daily=self._reset_daily_state,
        )
        self._strategies: dict[str, Strategy] = {}
        self._strategy_sequence = 0
        self._event_queue: queue.Queue[OrderEvent] = queue.Queue()
        self._stop_event = Event()
        self._shutdown_requested = Event()
        self._emergency_event = Event()
        self._pending_mode: CloseMode | None = None
        self._pending_source = "signal"
        self._threads: list[Thread] = []
        self._pidfile = PidFile(settings.pid_file)
        self._closer = EmergencyCloser(
            gateway,
            self._router,
            self._tracker,
            settings,
            lock=self._shared_lock,
            on_state_change=self._set_state,
            on_cancel_strategies=self._cancel_strategies,
            is_tradable=self._scheduler.is_tradable,
            notifier=notifier,
        )
        self._status_writer = StatusWriter(
            settings.status_file,
            interval_sec=settings.status_write_interval_sec,
            lock=self._shared_lock,
            full_snapshot=self._full_status_snapshot,
            degraded_snapshot=self._degraded_status_snapshot,
        )

    @property
    def state(self) -> EngineState:
        """回傳目前引擎狀態。"""
        with self._state_lock:
            return self._state

    def add_strategy(self, strategy: Strategy) -> str:
        """加入尚未結束的策略並回傳穩定識別碼。"""
        if self.state not in {EngineState.STOPPED, EngineState.RUNNING}:
            raise StrategyError("目前引擎狀態不允許新增策略")
        self._strategy_sequence += 1
        strategy_id = f"strategy-{self._strategy_sequence}"
        self._strategies[strategy_id] = strategy
        return strategy_id

    def start(self) -> None:
        """啟動券商連線與五類背景 worker。"""
        if self.state is not EngineState.STOPPED:
            return
        self._set_state(EngineState.STARTING)
        try:
            daily_state_unknown = self._load_daily_state()
            self._pidfile.acquire()
            self._gateway.connect()
            self._gateway.set_order_event_callback(self._event_queue.put_nowait)
            self._stop_event.clear()
            self._shutdown_requested.clear()
            self._install_signal_handlers()
            self._feed.start()
            self._scheduler.start()
            workers = (
                ("StrategyWorker", self._strategy_worker),
                ("EventWorker", self._event_worker),
                ("EmergencyWorker", self._emergency_worker),
                ("ReconcileWorker", self._reconcile_worker),
            )
            self._threads = [
                Thread(target=target, name=name, daemon=True) for name, target in workers
            ]
            for thread in self._threads:
                thread.start()
            self._set_state(EngineState.HALTED if daily_state_unknown else EngineState.RUNNING)
            self._status_writer.start()
        except Exception:
            logger.exception("引擎啟動失敗")
            self._set_state(EngineState.STOPPED)
            self._pidfile.release()
            raise

    def run_forever(self) -> None:
        """啟動後等待關機訊號。"""
        self.start()
        self._shutdown_requested.wait()
        self.stop()

    def stop(self) -> None:
        """依安全順序停止引擎並釋放券商連線與 PID 檔。"""
        if self.state is EngineState.STOPPED:
            return
        self._set_state(EngineState.SHUTTING_DOWN)
        self._feed.stop()
        self._scheduler.stop()
        if self._settings.flatten_on_shutdown:
            self._closer.execute(CloseMode.PANIC, "shutdown")
        else:
            try:
                self._gateway.cancel_all_orders()
            except Exception:
                logger.exception("關機取消委託失敗")
        self._stop_event.set()
        self._emergency_event.set()
        current = current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(timeout=10.0)
        try:
            self._gateway.disconnect()
        finally:
            self._save_daily_state()
            self._pidfile.release()
            self._set_state(EngineState.STOPPED)
            self._status_writer.stop()
            self._status_writer.write_once()

    def panic(self, source: str = "api") -> CloseReport:
        """同步執行全面緊急平倉並維持 HALTED。"""
        return self._closer.execute(CloseMode.PANIC, source)

    def flatten(self, source: str = "api") -> CloseReport:
        """同步平倉並回到 RUNNING 待命。"""
        return self._closer.execute(CloseMode.FLATTEN, source)

    def _scheduled_flatten(self, source: str) -> None:
        """供不需要回傳報告的排程器觸發強平。"""
        self.flatten(source)

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGUSR1, self._on_signal)
        signal.signal(signal.SIGUSR2, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _on_signal(self, signum: int, frame: object) -> None:
        del frame
        self._pending_mode = CloseMode.PANIC if signum == signal.SIGUSR1 else CloseMode.FLATTEN
        self._emergency_event.set()

    def _on_shutdown(self, signum: int, frame: object) -> None:
        del signum, frame
        self._shutdown_requested.set()

    def _strategy_worker(self) -> None:
        self._guard_worker(self._strategy_loop)

    def _strategy_loop(self) -> None:
        while not self._stop_event.is_set():
            tick = self._feed.get(timeout=0.1)
            if tick is None:
                continue
            self._tracker.on_tick(tick)
            if self.state is not EngineState.RUNNING or self._closer.is_closing:
                logger.warning("引擎非 RUNNING，丟棄觸價訊號")
                continue
            for strategy_id, strategy in tuple(self._strategies.items()):
                self._submit_signals(strategy_id, strategy.on_tick(tick))

    def _event_worker(self) -> None:
        self._guard_worker(self._event_loop)

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            request = self._router.in_flight.get(event.client_id or "")
            self._router.on_event(event)
            if isinstance(event, FillEvent):
                self._tracker.on_fill(event)
                self._save_daily_state()
            if request is None or not request.strategy_id:
                continue
            strategy = self._strategies.get(request.strategy_id)
            if strategy is None:
                continue
            if isinstance(event, FillEvent):
                self._submit_signals(request.strategy_id, strategy.on_fill(event))
            elif isinstance(event, RejectEvent):
                self._submit_signals(request.strategy_id, strategy.on_reject(event))

    def _emergency_worker(self) -> None:
        self._guard_worker(self._emergency_loop)

    def _emergency_loop(self) -> None:
        while not self._stop_event.is_set():
            self._emergency_event.wait(0.1)
            self._emergency_event.clear()
            mode = self._pending_mode
            source = self._pending_source
            if mode is not None:
                self._pending_mode = None
                self._closer.execute(mode, source)
            pending = self._closer.pending
            if pending is not None and self._scheduler.is_tradable():
                self._closer.execute(pending, "pending_open")

    def _reconcile_worker(self) -> None:
        self._guard_worker(self._reconcile_loop)

    def _reconcile_loop(self) -> None:
        while not self._stop_event.wait(60.0):
            differences = self._tracker.reconcile(self._gateway.list_positions())
            if differences:
                logger.warning("券商與引擎部位不同步：%s", "；".join(differences))

    def _guard_worker(self, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception:
            logger.exception("worker 發生未預期例外，觸發 PANIC")
            self._closer.execute(CloseMode.PANIC, "unhandled_exception")

    def _submit_signals(self, strategy_id: str, signals: list[Signal]) -> None:
        for strategy_signal in signals:
            if self.state is not EngineState.RUNNING:
                return
            request = self._request_from_signal(strategy_id, strategy_signal)
            context = RiskContext(
                now=datetime.now(_TAIPEI),
                session=self._scheduler.current_session(),
                engine_state=self.state,
                position=self._tracker.snapshot(),
                realized_pnl_ntd=self._tracker.realized_pnl_ntd,
                total_pnl_ntd=self._tracker.total_pnl_ntd,
                trade_count=self._tracker.trade_count,
                last_order_at=None,
                price_limits=None,
            )
            self._router.submit(request, context)

    def _request_from_signal(self, strategy_id: str, strategy_signal: Signal) -> OrderRequest:
        price_type = PriceType.LMT if strategy_signal.limit_price is not None else PriceType.MKP
        time_in_force = TimeInForce.ROD if price_type is PriceType.LMT else TimeInForce.IOC
        return OrderRequest(
            symbol=self._settings.symbol,
            action=strategy_signal.action,
            quantity=strategy_signal.quantity,
            price=strategy_signal.limit_price,
            price_type=price_type,
            time_in_force=time_in_force,
            intent=strategy_signal.intent,
            client_id=new_client_id(),
            strategy_id=strategy_id,
        )

    def _cancel_strategies(self, reason: str) -> None:
        for strategy in self._strategies.values():
            strategy.abort(reason)

    def _set_state(self, state: EngineState) -> None:
        with self._state_lock:
            self._state = state

    def _full_status_snapshot(self) -> StatusSnapshot:
        position = self._tracker.snapshot()
        feed = self._feed.stats
        session = self._scheduler.current_session()
        return StatusSnapshot(
            schema_version=1,
            written_at=snapshot_time(),
            pid=os.getpid(),
            engine_state=self.state.value,
            mode="LIVE" if self._settings.is_live else "SIMULATION",
            symbol=self._settings.symbol,
            session=session.value if session is not None else None,
            broker_connected=self._gateway.is_connected,
            degraded=False,
            degraded_reason="",
            position={
                "direction": position.direction.value if position.direction else None,
                "quantity": position.quantity,
                "average_price": position.average_price,
                "unrealized_ntd": position.unrealized_ntd,
            },
            pnl={
                "realized_ntd": self._tracker.realized_pnl_ntd,
                "total_ntd": self._tracker.total_pnl_ntd,
            },
            trade_count=self._tracker.trade_count,
            strategies=[
                {
                    "id": strategy_id,
                    "kind": type(strategy).__name__.removesuffix("Strategy").lower(),
                    "state": strategy.state.value,
                    "summary": strategy.describe(),
                }
                for strategy_id, strategy in self._strategies.items()
            ],
            feed={
                "received": feed.received,
                "evicted_overflow": feed.evicted_overflow,
                "max_latency_ms": feed.max_latency_ms,
                "last_tick_at": feed.last_tick_at.isoformat() if feed.last_tick_at else None,
            },
            emergency={
                "is_closing": self._closer.is_closing,
                "pending": self._closer.pending.value if self._closer.pending else None,
                "last_succeeded": None,
            },
        )

    def _degraded_status_snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            schema_version=1,
            written_at=snapshot_time(),
            pid=os.getpid(),
            engine_state=self.state.value,
            mode="LIVE" if self._settings.is_live else "SIMULATION",
            symbol=self._settings.symbol,
            session=None,
            broker_connected=None,
            degraded=True,
            degraded_reason="無法取得共用鎖",
            position=None,
            pnl=None,
            trade_count=None,
            strategies=None,
            feed=None,
            emergency=None,
        )

    def _load_daily_state(self) -> bool:
        result = self._daily_store.load(datetime.now(_TAIPEI))
        if result.outcome is LoadOutcome.UNREADABLE:
            self._daily_state_writable = False
            message = f"當日風控狀態無法讀取，禁止新倉：{result.error}"
            logger.critical(message)
            self._notify_daily_state_failure(message)
            return True
        state = result.state
        if state is None:
            raise MicroTXError("可讀取的當日狀態缺少 state")
        self._tracker.restore_daily(
            realized_pnl_ntd=state.realized_pnl_ntd, trade_count=state.trade_count
        )
        if result.outcome is LoadOutcome.RESTORED:
            logger.info(
                "已還原當日累計：損益 %.0f 元 / %d 筆",
                state.realized_pnl_ntd,
                state.trade_count,
            )
        elif result.outcome is LoadOutcome.ROLLED_OVER and result.previous is not None:
            logger.info(
                "交易日切換，前日結算：損益 %.0f 元 / %d 筆",
                result.previous.realized_pnl_ntd,
                result.previous.trade_count,
            )
        else:
            logger.info("本交易日首次啟動，當日累計從零開始")
        self._save_daily_state()
        return False

    def _save_daily_state(self) -> None:
        if not self._daily_state_writable:
            return
        try:
            now = datetime.now(_TAIPEI)
            self._daily_store.save(
                DailyState(
                    schema_version=1,
                    trading_date=trading_date(now, boundary=self._settings.trading_day_boundary),
                    realized_pnl_ntd=self._tracker.realized_pnl_ntd,
                    trade_count=self._tracker.trade_count,
                    updated_at=now,
                )
            )
        except Exception:
            logger.warning("當日狀態寫入失敗", exc_info=True)

    def _reset_daily_state(self) -> None:
        self._tracker.reset_daily()
        try:
            self._daily_store.clear()
        except Exception:
            logger.warning("清除前一交易日狀態失敗", exc_info=True)
        self._save_daily_state()

    def _notify_daily_state_failure(self, message: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(NotifyLevel.CRITICAL, "當日風控狀態損毀", message)
        except Exception:
            logger.warning("當日狀態異常通知失敗", exc_info=True)
