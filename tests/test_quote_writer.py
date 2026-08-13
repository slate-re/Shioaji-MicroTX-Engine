"""報價快照效能、原子性與零開銷停用路徑測試。"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import time
from datetime import datetime
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

from microtx.broker.base import RawTick
from microtx.broker.paper_gateway import PaperGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.engine.engine import TradingEngine
from microtx.engine.quote_writer import QuoteWriter
from microtx.market.feed import MarketFeed
from microtx.market.tick import TickEvent

_TAIPEI = ZoneInfo("Asia/Taipei")
_NOW = datetime(2026, 8, 13, 10, 23, 45, tzinfo=_TAIPEI)


def _raw() -> RawTick:
    return RawTick("TMFF6", _NOW, 23_150.0, 1, 100, 1, False)


def _tick() -> TickEvent:
    return TickEvent(TMF.symbol, "TMFF6", _NOW, 23_150.0, 1, 100, 1, _NOW)


def test_callback_latest_tick_assignment_cost_is_below_one_microsecond() -> None:
    enabled = MarketFeed(PaperGateway(spec=TMF), symbol=TMF.symbol, capture_latest_tick=True)
    disabled = MarketFeed(PaperGateway(spec=TMF), symbol=TMF.symbol, capture_latest_tick=False)
    raw = _raw()
    iterations = 200_000

    started = time.perf_counter_ns()
    for _ in range(iterations):
        disabled._on_raw_tick(raw)
        disabled.get()
    baseline_ns = time.perf_counter_ns() - started
    started = time.perf_counter_ns()
    for _ in range(iterations):
        enabled._on_raw_tick(raw)
        enabled.get()
    enabled_ns = time.perf_counter_ns() - started

    # 顯示功能在 callback 端只能增加單一屬性賦值，不得拖累行情處理。
    added_microseconds = max(enabled_ns - baseline_ns, 0) / iterations / 1_000
    assert added_microseconds < 1.0


def test_callback_capture_path_has_no_lock_acquisition() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(MarketFeed._on_raw_tick)))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "_latest_tick"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assignment = assignments[0]
    enclosing_with = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith)) and assignment in ast.walk(node)
    ]
    # 顯示功能不得為最新報價賦值新增鎖；既有 _stats_lock 統計路徑仍完整保留。
    assert enclosing_with == []


def test_single_atomic_write_is_fast_and_compact(tmp_path: Path) -> None:
    path = tmp_path / "quote.json"
    writer = QuoteWriter(path, symbol=TMF.symbol, interval_sec=0.25, latest_tick=_tick)
    started = time.perf_counter_ns()
    writer.write_once()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    assert elapsed_ms < 5.0
    assert path.stat().st_size < 300


def test_atomic_write_never_exposes_partial_json(tmp_path: Path) -> None:
    path = tmp_path / "quote.json"
    writer = QuoteWriter(path, symbol=TMF.symbol, interval_sec=0.25, latest_tick=_tick)
    writer.write_once()
    errors: list[Exception] = []

    def read_repeatedly() -> None:
        for _ in range(200):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(exc)

    reader = Thread(target=read_repeatedly)
    reader.start()
    for _ in range(30):
        writer.write_once()
    reader.join()
    assert errors == []


def test_disabled_snapshot_has_no_writer_and_does_not_capture_tick(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        enable_quote_snapshot=False,
        quote_file=tmp_path / "quote.json",
    )
    engine = TradingEngine(settings, PaperGateway(spec=TMF))
    engine._feed._on_raw_tick(_raw())
    assert engine._quote_writer is None
    assert engine._feed.latest_tick is None
    assert not settings.quote_file.exists()


def test_enabled_engine_starts_and_stops_quote_writer(mocker, tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        pid_file=tmp_path / "engine.pid",
        status_file=tmp_path / "status.json",
        daily_state_file=tmp_path / "daily.json",
        quote_file=tmp_path / "quote.json",
        quote_write_interval_sec=0.05,
        flatten_on_shutdown=False,
    )
    engine = TradingEngine(settings, PaperGateway(spec=TMF))
    mocker.patch.object(engine, "_install_signal_handlers")
    engine.start()
    assert engine._quote_writer is not None
    assert engine._quote_writer._thread is not None
    assert engine._quote_writer._thread.is_alive()
    engine.stop()
    assert settings.quote_file.exists()
    assert not engine._quote_writer._thread.is_alive()


def test_write_failure_only_logs_warning(mocker, tmp_path: Path) -> None:
    writer = QuoteWriter(
        tmp_path / "quote.json", symbol=TMF.symbol, interval_sec=0.25, latest_tick=_tick
    )
    warning = mocker.patch("microtx.engine.quote_writer.logger.warning")
    mocker.patch("microtx.engine.quote_writer.os.replace", side_effect=OSError("唯讀"))
    writer.write_once()
    warning.assert_called_once()


def test_no_tick_writes_null_price(tmp_path: Path) -> None:
    path = tmp_path / "quote.json"
    writer = QuoteWriter(path, symbol=TMF.symbol, interval_sec=0.25, latest_tick=lambda: None)
    writer.write_once()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["last_price"] is None
    assert payload["tick_at"] is None


def test_writer_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    writer = QuoteWriter(
        tmp_path / "quote.json", symbol=TMF.symbol, interval_sec=0.05, latest_tick=_tick
    )
    writer.start()
    writer.start()
    assert writer._thread is not None
    assert writer._thread.is_alive()
    writer.stop()
    writer.stop()
    assert not writer._thread.is_alive()
