"""只讀取 PID 與 JSON 快照的獨立監看程式。"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

from microtx.config import Settings
from microtx.contracts import get_spec
from microtx.exceptions import MicroTXError
from microtx.utils.pidfile import PidFile

_CONNECTED = "CONNECTED"
_DEGRADED = "DEGRADED"
_DISCONNECTED = "DISCONNECTED"
_NO_RESPONSE = "NO RESPONSE"
_STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class WatchSnapshot:
    """一次唯讀畫面更新所需的資料。"""

    health: str
    color: str
    status: dict[str, Any]
    quote: dict[str, Any]
    displayed_time: str
    unrealized_ntd: float | None
    warning: str = ""


class _ConsoleLike(Protocol):
    width: int


class _LiveLike(Protocol):
    def __enter__(self) -> _LiveLike: ...

    def __exit__(self, *exc: object) -> None: ...

    def update(self, renderable: object) -> None: ...


@dataclass(frozen=True, slots=True)
class _RichFactories:
    console: Callable[..., _ConsoleLike]
    live: Callable[..., _LiveLike]
    panel: Callable[..., object]
    markup_text: Callable[[str], object]


def watch(settings: Settings, *, interval: float) -> None:
    """持續讀取檔案並顯示，不建立任何引擎或交易物件。"""
    if interval <= 0:
        raise ValueError("刷新間隔必須大於 0")
    rich = _require_rich()
    if not sys.stdout.isatty():
        _line_loop(settings, interval=interval)
        return
    console = rich.console()
    live = rich.live(console=console, refresh_per_second=max(int(1 / interval), 1))
    with live:
        while True:
            live.update(_render_rich(rich, read_snapshot(settings), console.width))
            time.sleep(interval)


def read_snapshot(settings: Settings, *, now: datetime | None = None) -> WatchSnapshot:
    """讀取一次狀態並判定五態，供 Rich 與純文字共用。"""
    status = _read_json(settings.status_file)
    quote = _read_json(settings.quote_file)
    pid = PidFile.read_pid(settings.pid_file)
    health, color, warning = _health(
        pid,
        status,
        now=now,
        stale_after=settings.status_write_interval_sec * 3,
    )
    written_at = str(status.get("written_at", ""))
    displayed_time = "--"
    if written_at:
        try:
            displayed_time = datetime.fromisoformat(written_at).strftime("%H:%M:%S")
        except ValueError:
            displayed_time = "--"
    return WatchSnapshot(
        health,
        color,
        status,
        quote,
        displayed_time,
        calculate_unrealized(status, quote),
        warning,
    )


def calculate_unrealized(status: dict[str, Any], quote: dict[str, Any]) -> float | None:
    """用慢速部位與快速價格計算即時未實現損益。"""
    position = status.get("position")
    price = quote.get("last_price")
    symbol = status.get("symbol")
    if not isinstance(position, dict) or price is None or not isinstance(symbol, str):
        return None
    direction = position.get("direction")
    if direction not in {"LONG", "SHORT"}:
        return None
    sign = 1 if direction == "LONG" else -1
    return (
        (float(price) - float(position["average_price"]))
        * sign
        * int(position["quantity"])
        * get_spec(symbol).point_value
    )


def _health(
    pid: int | None,
    status: dict[str, Any],
    *,
    now: datetime | None,
    stale_after: float,
) -> tuple[str, str, str]:
    if pid is None:
        return _STOPPED, "dim", ""
    try:
        written = datetime.fromisoformat(str(status["written_at"]))
        current = now or datetime.now(written.tzinfo)
        if (current - written).total_seconds() > stale_after:
            return _NO_RESPONSE, "red", "引擎無回應"
    except (KeyError, ValueError, TypeError):
        return _NO_RESPONSE, "red", "狀態快照無法讀取"
    if status.get("degraded") is True:
        return _DEGRADED, "yellow", "引擎卡在共用鎖，建議立即 microtx panic"
    if status.get("broker_connected") is False:
        return _DISCONNECTED, "yellow", "券商連線中斷，SDK 正在重連"
    return _CONNECTED, "green", ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _require_rich() -> _RichFactories:
    try:
        console_module = importlib.import_module("rich.console")
        live_module = importlib.import_module("rich.live")
        panel_module = importlib.import_module("rich.panel")
        text_module = importlib.import_module("rich.text")
    except ModuleNotFoundError as exc:
        raise MicroTXError(
            '未安裝 rich。請執行 pip install -e ".[tui]" 後再使用 microtx watch。'
        ) from exc
    text_class = text_module.Text
    return _RichFactories(
        cast(Callable[..., _ConsoleLike], console_module.Console),
        cast(Callable[..., _LiveLike], live_module.Live),
        cast(Callable[..., object], panel_module.Panel),
        cast(Callable[[str], object], text_class.from_markup),
    )


def _render_rich(rich: _RichFactories, snapshot: WatchSnapshot, width: int) -> object:
    text = _plain_text(snapshot, compact=width < 60)
    return rich.panel(
        rich.markup_text(text),
        title=f"MicroTX [{snapshot.color}]{snapshot.health}[/] {snapshot.displayed_time}",
    )


def _plain_text(snapshot: WatchSnapshot, *, compact: bool) -> str:
    price = snapshot.quote.get("last_price")
    price_text = "--" if price is None else f"{float(price):g}"
    pnl = snapshot.unrealized_ntd
    pnl_text = "--" if pnl is None else f"{pnl:+,.0f} 元"
    separator = "\n" if compact else " | "
    parts = [
        f"{snapshot.health} {snapshot.displayed_time}",
        f"價格 {price_text}",
        f"未實現 {pnl_text}",
        f"引擎 {snapshot.status.get('engine_state', '--')}",
    ]
    if snapshot.warning:
        parts.append(snapshot.warning)
    return separator.join(parts)


def _line_loop(settings: Settings, *, interval: float) -> None:
    while True:
        print(_plain_text(read_snapshot(settings), compact=False), flush=True)
        time.sleep(max(interval, 1.0))


def terminal_width() -> int:
    """回傳終端寬度，無法取得時使用保守預設。"""
    return shutil.get_terminal_size(fallback=(80, 24)).columns
