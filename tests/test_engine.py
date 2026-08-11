"""TradingEngine 緊急 worker、訊號處理與 PID 生命週期測試。"""

from __future__ import annotations

import os
import signal
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Thread

from microtx.broker.base import OrderRequest
from microtx.broker.paper_gateway import PaperGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.engine.engine import TradingEngine
from microtx.enums import CloseMode, Direction, EngineState, OrderIntent, PriceType, TimeInForce
from microtx.exceptions import StrategyError
from microtx.market.tick import TickEvent
from microtx.strategies.base import Signal
from microtx.strategies.scalp import ScalpStrategy
from microtx.utils.pidfile import PidFile


def _seed_position(gateway: PaperGateway) -> None:
    gateway.place_order(
        OrderRequest(
            TMF.symbol,
            Direction.LONG,
            1,
            None,
            PriceType.MKP,
            TimeInForce.IOC,
            OrderIntent.ENTRY,
            "seed",
        )
    )


def _strategy() -> ScalpStrategy:
    return ScalpStrategy(
        spec=TMF,
        direction=Direction.LONG,
        trigger_price=23_000.0,
        take_profit_points=50,
        stop_loss_points=30,
    )


def test_pending_close_is_consumed_by_emergency_worker_at_open(mocker, tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    _seed_position(gateway)
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    engine._closer._is_tradable = lambda: False
    report = engine.panic("closed")
    assert report.succeeded is False
    assert engine._closer.pending is CloseMode.PANIC

    engine._closer._is_tradable = lambda: True
    engine._scheduler.is_tradable = lambda now=None: True
    completed = Event()
    original = gateway.place_order

    def mark_close(request: OrderRequest):
        result = original(request)
        if request.intent is OrderIntent.EMERGENCY:
            completed.set()
        return result

    mocker.patch.object(gateway, "place_order", side_effect=mark_close)
    mocker.patch("microtx.engine.emergency.time.sleep")
    worker = Thread(target=engine._emergency_loop)
    worker.start()

    assert completed.wait(1.0)
    engine._stop_event.set()
    worker.join(1.0)
    assert gateway.list_positions() == []


def test_halted_engine_drops_strategy_signal_without_order(mocker, tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    engine._set_state(EngineState.HALTED)
    place = mocker.spy(gateway, "place_order")

    engine._submit_signals(
        "strategy-1",
        [Signal(OrderIntent.ENTRY, Direction.LONG, 1, "觸價")],
    )

    assert place.call_count == 0


def test_stale_pid_is_removed_and_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "microtx.pid"
    path.parent.mkdir()
    path.write_text("99999999", encoding="utf-8")

    assert PidFile.read_pid(path) is None
    pidfile = PidFile(path)
    pidfile.acquire()

    assert path.read_text(encoding="utf-8") == str(os.getpid())
    pidfile.release()
    assert not path.exists()


def test_pidfile_context_and_live_pid_rejection(tmp_path: Path) -> None:
    path = tmp_path / "microtx.pid"
    with PidFile(path):
        assert PidFile.read_pid(path) == os.getpid()
    assert not path.exists()


def test_sigusr1_handler_only_sets_mode_and_event(tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    old_handler = signal.getsignal(signal.SIGUSR1)
    signal.signal(signal.SIGUSR1, engine._on_signal)
    try:
        started = time.perf_counter()
        engine._on_signal(signal.SIGUSR1, None)
        elapsed = time.perf_counter() - started
        engine._emergency_event.clear()

        os.kill(os.getpid(), signal.SIGUSR1)

        assert engine._emergency_event.wait(0.1)
        assert engine._pending_mode is CloseMode.PANIC
        assert elapsed < 0.001
    finally:
        signal.signal(signal.SIGUSR1, old_handler)


def test_shutdown_handler_only_sets_event(tmp_path: Path) -> None:
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), PaperGateway(spec=TMF))
    engine._on_shutdown(signal.SIGTERM, None)
    assert engine._shutdown_requested.is_set()


def test_start_and_stop_manage_all_resources(mocker, tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    engine = TradingEngine(
        Settings(pid_file=tmp_path / "engine.pid", flatten_on_shutdown=False), gateway
    )
    mocker.patch.object(engine, "_install_signal_handlers")

    engine.start()
    engine.start()

    assert engine.state is EngineState.RUNNING
    assert gateway.is_connected is True
    assert all(thread.is_alive() for thread in engine._threads)
    engine.stop()
    engine.stop()
    assert engine.state is EngineState.STOPPED
    assert gateway.is_connected is False
    assert not (tmp_path / "engine.pid").exists()


def test_stop_writes_final_stopped_status(mocker, tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    gateway = PaperGateway(spec=TMF)
    engine = TradingEngine(
        Settings(
            _env_file=None,
            pid_file=tmp_path / "engine.pid",
            status_file=status_file,
            flatten_on_shutdown=False,
        ),
        gateway,
    )
    mocker.patch.object(engine, "_install_signal_handlers")
    engine.start()
    engine.stop()

    import json

    assert json.loads(status_file.read_text(encoding="utf-8"))["engine_state"] == "STOPPED"


def test_shutdown_with_flatten_calls_emergency_closer(mocker, tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    mocker.patch.object(engine, "_install_signal_handlers")
    panic = mocker.spy(engine._closer, "execute")
    engine.start()

    engine.stop()

    panic.assert_called_with(CloseMode.PANIC, "shutdown")


def test_add_strategy_returns_ids_and_rejects_invalid_engine_state(tmp_path: Path) -> None:
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), PaperGateway(spec=TMF))
    assert engine.add_strategy(_strategy()) == "strategy-1"
    assert engine.add_strategy(_strategy()) == "strategy-2"
    engine._set_state(EngineState.HALTED)
    try:
        engine.add_strategy(_strategy())
    except StrategyError:
        pass
    else:
        raise AssertionError("HALTED 不得新增策略")


def test_run_forever_starts_then_stops(mocker, tmp_path: Path) -> None:
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), PaperGateway(spec=TMF))

    def request_shutdown() -> None:
        engine._shutdown_requested.set()

    mocker.patch.object(engine, "start", side_effect=request_shutdown)
    stop = mocker.patch.object(engine, "stop")
    engine.run_forever()
    stop.assert_called_once()


def test_strategy_loop_routes_crossing_signal(mocker, tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    strategy = _strategy()
    strategy.arm()
    engine.add_strategy(strategy)
    engine._set_state(EngineState.RUNNING)
    now = time.time()
    tick = TickEvent(
        TMF.symbol,
        TMF.symbol,
        datetime.fromtimestamp(now),
        23_000.0,
        1,
        1,
        0,
        datetime.fromtimestamp(now),
    )

    def one_tick(timeout: float | None = None):
        del timeout
        engine._stop_event.set()
        return tick

    mocker.patch.object(engine._feed, "get", side_effect=one_tick)
    engine._strategy_loop()
    assert strategy.state.value == "ENTRY_PENDING"


def test_scheduled_flatten_discards_report(mocker, tmp_path: Path) -> None:
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), PaperGateway(spec=TMF))
    flatten = mocker.patch.object(engine, "flatten")
    engine._scheduled_flatten("scheduler")
    flatten.assert_called_once_with("scheduler")


def test_flatten_and_abort_strategy_delegate_to_safety_components(tmp_path: Path) -> None:
    gateway = PaperGateway(spec=TMF)
    gateway.connect()
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), gateway)
    strategy = _strategy()
    strategy.arm()
    engine.add_strategy(strategy)
    engine._closer._is_tradable = lambda: True

    report = engine.flatten("manual")

    assert report.succeeded is True
    assert strategy.state.value == "ABORTED"
    assert engine.state is EngineState.RUNNING


def test_guard_worker_panics_on_unhandled_exception(mocker, tmp_path: Path) -> None:
    engine = TradingEngine(Settings(pid_file=tmp_path / "engine.pid"), PaperGateway(spec=TMF))
    execute = mocker.patch.object(engine._closer, "execute")

    def broken() -> None:
        raise RuntimeError("worker crash")

    engine._guard_worker(broken)
    execute.assert_called_once_with(CloseMode.PANIC, "unhandled_exception")
