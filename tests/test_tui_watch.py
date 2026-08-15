"""唯讀 TUI 的計算、五態、時間來源與隔離性測試。"""

from __future__ import annotations

import ast
import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from microtx.cli.commands import main
from microtx.config import Settings
from microtx.exceptions import MicroTXError
from microtx.tui.dashboard import (
    _line_loop,
    _plain_text,
    _read_json,
    _render_rich,
    _require_rich,
    _RichFactories,
    calculate_unrealized,
    read_snapshot,
    terminal_width,
    watch,
)

_TAIPEI = ZoneInfo("Asia/Taipei")
_NOW = datetime(2026, 8, 13, 10, 30, tzinfo=_TAIPEI)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        pid_file=tmp_path / "engine.pid",
        status_file=tmp_path / "status.json",
        quote_file=tmp_path / "quote.json",
        status_write_interval_sec=5.0,
    )


def _write_snapshots(
    settings: Settings,
    *,
    written_at: datetime = _NOW,
    degraded: bool = False,
    connected: bool = True,
    price: float | None = 23_150.0,
) -> None:
    settings.status_file.write_text(
        json.dumps(
            {
                "written_at": written_at.isoformat(),
                "degraded": degraded,
                "broker_connected": connected,
                "engine_state": "RUNNING",
                "symbol": "TMFR1",
                "position": {
                    "direction": "LONG",
                    "quantity": 1,
                    "average_price": 23_100.0,
                },
            }
        ),
        encoding="utf-8",
    )
    settings.quote_file.write_text(json.dumps({"last_price": price}), encoding="utf-8")


@pytest.mark.parametrize(
    ("direction", "quantity", "price", "expected"),
    [("LONG", 2, 23_110.0, 200.0), ("SHORT", 3, 23_090.0, 300.0)],
)
def test_unrealized_uses_fast_quote_for_long_and_short(
    direction: str, quantity: int, price: float, expected: float
) -> None:
    status = {
        "symbol": "TMFR1",
        "position": {"direction": direction, "quantity": quantity, "average_price": 23_100.0},
    }
    assert calculate_unrealized(status, {"last_price": price}) == expected


@pytest.mark.parametrize(
    ("pid", "degraded", "connected", "age", "expected"),
    [
        (123, False, True, 0, "CONNECTED"),
        (123, True, True, 0, "DEGRADED"),
        (123, False, False, 0, "DISCONNECTED"),
        (123, False, True, 16, "NO RESPONSE"),
        (None, False, True, 0, "STOPPED"),
    ],
)
def test_five_health_states(
    mocker, tmp_path: Path, pid, degraded: bool, connected: bool, age: int, expected: str
) -> None:
    settings = _settings(tmp_path)
    _write_snapshots(
        settings,
        written_at=_NOW - timedelta(seconds=age),
        degraded=degraded,
        connected=connected,
    )
    mocker.patch("microtx.tui.dashboard.PidFile.read_pid", return_value=pid)
    snapshot = read_snapshot(settings, now=_NOW)
    assert snapshot.health == expected
    if expected == "DEGRADED":
        assert "panic" in snapshot.warning


def test_displayed_time_comes_from_status_not_local_clock(mocker, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshots(settings, written_at=_NOW)
    mocker.patch("microtx.tui.dashboard.PidFile.read_pid", return_value=123)
    first = read_snapshot(settings, now=_NOW)
    later = read_snapshot(settings, now=_NOW + timedelta(seconds=10))
    assert first.displayed_time == later.displayed_time == "10:30:00"


def test_missing_price_displays_placeholder(mocker, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshots(settings, price=None)
    mocker.patch("microtx.tui.dashboard.PidFile.read_pid", return_value=123)
    assert "價格 --" in _plain_text(read_snapshot(settings, now=_NOW), compact=True)


def test_unrealized_returns_none_for_missing_or_flat_position() -> None:
    assert calculate_unrealized({}, {}) is None
    assert (
        calculate_unrealized(
            {
                "symbol": "TMFR1",
                "position": {"direction": None, "quantity": 0, "average_price": 0},
            },
            {"last_price": 23_000},
        )
        is None
    )


def test_narrow_terminal_uses_multiline_text(mocker, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshots(settings)
    mocker.patch("microtx.tui.dashboard.PidFile.read_pid", return_value=123)
    assert "\n" in _plain_text(read_snapshot(settings, now=_NOW), compact=True)


def test_missing_rich_has_actionable_install_message(monkeypatch) -> None:
    original = importlib.import_module

    def fail_rich(name: str):
        if name.startswith("rich"):
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", fail_rich)
    with pytest.raises(MicroTXError, match=r'pip install -e "\.\[tui\]"'):
        _require_rich()


def test_watch_cli_reports_missing_rich_without_module_error(monkeypatch, capsys) -> None:
    original = importlib.import_module

    def fail_rich(name: str):
        if name.startswith("rich"):
            raise ModuleNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", fail_rich)
    assert main(["watch"]) != 0
    error = capsys.readouterr().err
    assert 'pip install -e ".[tui]"' in error
    assert "ModuleNotFoundError" not in error


def test_watch_rejects_invalid_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="必須大於 0"):
        watch(_settings(tmp_path), interval=0)


def test_non_tty_watch_uses_line_mode(mocker, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    mocker.patch("microtx.tui.dashboard._require_rich")
    mocker.patch("microtx.tui.dashboard.sys.stdout.isatty", return_value=False)
    line_loop = mocker.patch("microtx.tui.dashboard._line_loop")
    watch(settings, interval=0.5)
    line_loop.assert_called_once_with(settings, interval=0.5)


def test_line_mode_prints_snapshot_once(mocker, tmp_path: Path, capsys) -> None:
    settings = _settings(tmp_path)
    _write_snapshots(settings)
    mocker.patch("microtx.tui.dashboard.PidFile.read_pid", return_value=123)
    mocker.patch("microtx.tui.dashboard.time.sleep", side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        _line_loop(settings, interval=0.1)
    output = capsys.readouterr().out
    assert "價格 23150" in output
    assert "引擎 RUNNING" in output


def test_invalid_json_and_non_object_are_empty(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("broken", encoding="utf-8")
    assert _read_json(path) == {}
    path.write_text("[]", encoding="utf-8")
    assert _read_json(path) == {}


def test_render_rich_uses_compact_layout_at_width_40() -> None:
    captured: list[str] = []

    def markup(value: str) -> object:
        captured.append(value)
        return value

    factories = _RichFactories(lambda: None, lambda: None, lambda *args, **kwargs: args, markup)
    snapshot = read_snapshot(Settings(_env_file=None), now=_NOW)
    _render_rich(factories, snapshot, 40)
    assert "\n" in captured[0]


def test_terminal_width_has_positive_fallback() -> None:
    assert terminal_width() > 0


def test_tui_package_has_no_trading_control_imports() -> None:
    tui_root = Path(__file__).parents[1] / "src/microtx/tui"
    forbidden = {"OrderRouter", "TradingEngine", "EmergencyCloser"}
    for path in tui_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert forbidden.isdisjoint(imported)
