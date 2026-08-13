"""跨程序重啟保存券商無法提供的交易日累計值。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

from microtx.engine.trading_day import trading_date
from microtx.enums import LoadOutcome

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class DailyState:
    """當日已實現損益與交易次數；刻意不包含部位。"""

    schema_version: int
    trading_date: date
    realized_pnl_ntd: float
    trade_count: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LoadResult:
    """狀態檔載入結果；損毀時不提供看似安全的零值。"""

    outcome: LoadOutcome
    state: DailyState | None
    previous: DailyState | None = None
    error: str = ""


class DailyStateStore:
    """以原子替換讀寫交易日累計狀態。"""

    def __init__(self, path: Path, *, boundary: time) -> None:
        self._path = path
        self._boundary = boundary

    def load(self, now: datetime) -> LoadResult:
        """載入並依目前交易日分類結果。"""
        current_date = trading_date(now, boundary=self._boundary)
        if not self._path.exists():
            return LoadResult(LoadOutcome.FRESH, self._empty_state(current_date, now))
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            state = self._decode(payload)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return LoadResult(LoadOutcome.UNREADABLE, None, error=str(exc))
        if state.trading_date == current_date:
            return LoadResult(LoadOutcome.RESTORED, state)
        fresh = self._empty_state(current_date, now)
        return LoadResult(LoadOutcome.ROLLED_OVER, fresh, previous=state)

    def save(self, state: DailyState) -> None:
        """原子寫入狀態，避免讀者取得半截 JSON。"""
        if state.schema_version != _SCHEMA_VERSION:
            raise ValueError("不支援的當日狀態 schema_version")
        payload = asdict(state)
        payload["trading_date"] = state.trading_date.isoformat()
        payload["updated_at"] = state.updated_at.isoformat()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._path)

    def clear(self) -> None:
        """移除既有狀態與可能殘留的暫存檔。"""
        self._path.unlink(missing_ok=True)
        self._path.with_suffix(".json.tmp").unlink(missing_ok=True)

    @staticmethod
    def _empty_state(current_date: date, now: datetime) -> DailyState:
        return DailyState(_SCHEMA_VERSION, current_date, 0.0, 0, now)

    @staticmethod
    def _decode(payload: object) -> DailyState:
        if not isinstance(payload, dict):
            raise ValueError("當日狀態必須是 JSON object")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("不支援的當日狀態 schema_version")
        realized = payload["realized_pnl_ntd"]
        trades = payload["trade_count"]
        if isinstance(realized, bool) or not isinstance(realized, (int, float)):
            raise ValueError("realized_pnl_ntd 型別錯誤")
        if isinstance(trades, bool) or not isinstance(trades, int) or trades < 0:
            raise ValueError("trade_count 型別錯誤")
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        if updated_at.tzinfo is None:
            raise ValueError("updated_at 必須包含時區")
        return DailyState(
            _SCHEMA_VERSION,
            date.fromisoformat(str(payload["trading_date"])),
            float(realized),
            trades,
            updated_at,
        )
