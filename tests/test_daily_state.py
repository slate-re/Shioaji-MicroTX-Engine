"""當日累計狀態的載入分類、原子寫入與安全失敗測試。"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

import pytest

from microtx.engine.daily_state import DailyState, DailyStateStore
from microtx.enums import LoadOutcome

_TAIPEI = ZoneInfo("Asia/Taipei")
_NOW = datetime(2026, 1, 9, 22, 0, tzinfo=_TAIPEI)


def _store(path: Path) -> DailyStateStore:
    return DailyStateStore(path, boundary=time(6, 0))


def _state(*, day: date = date(2026, 1, 9), pnl: float = -1_240.0) -> DailyState:
    return DailyState(1, day, pnl, 6, _NOW)


def test_missing_file_is_fresh_zero_state(tmp_path: Path) -> None:
    result = _store(tmp_path / "daily.json").load(_NOW)
    assert result.outcome is LoadOutcome.FRESH
    assert result.state is not None
    assert (result.state.realized_pnl_ntd, result.state.trade_count) == (0.0, 0)


def test_same_trading_date_is_restored(tmp_path: Path) -> None:
    store = _store(tmp_path / "daily.json")
    store.save(_state())
    result = store.load(_NOW)
    assert result.outcome is LoadOutcome.RESTORED
    assert result.state == _state()


def test_different_trading_date_rolls_over_and_keeps_previous(tmp_path: Path) -> None:
    store = _store(tmp_path / "daily.json")
    previous = _state(day=date(2026, 1, 8))
    store.save(previous)
    result = store.load(_NOW)
    assert result.outcome is LoadOutcome.ROLLED_OVER
    assert result.previous == previous
    assert result.state is not None
    assert result.state.trading_date == date(2026, 1, 9)
    assert result.state.realized_pnl_ntd == 0.0


@pytest.mark.parametrize(
    "content",
    ["{broken", "{}", '{"schema_version": 2}', '["not-object"]'],
)
def test_corrupt_or_invalid_file_is_unreadable(tmp_path: Path, content: str) -> None:
    path = tmp_path / "daily.json"
    path.write_text(content, encoding="utf-8")
    result = _store(path).load(_NOW)
    assert result.outcome is LoadOutcome.UNREADABLE
    assert result.state is None
    assert result.error


def test_atomic_save_never_exposes_partial_json(tmp_path: Path) -> None:
    path = tmp_path / "daily.json"
    store = _store(path)
    store.save(_state())
    errors: list[Exception] = []

    def read_repeatedly() -> None:
        for _ in range(200):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(exc)

    reader = Thread(target=read_repeatedly)
    reader.start()
    for index in range(30):
        store.save(_state(pnl=-float(index)))
    reader.join()
    assert errors == []


def test_clear_removes_state_and_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "daily.json"
    store = _store(path)
    store.save(_state())
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text("partial", encoding="utf-8")
    store.clear()
    assert not path.exists()
    assert not temporary.exists()


def test_serialized_state_contains_no_unrelated_secret_values(tmp_path: Path) -> None:
    path = tmp_path / "daily.json"
    _store(path).save(_state())
    serialized = path.read_text(encoding="utf-8")
    assert "API_SECRET_NEVER_WRITE" not in serialized
    assert set(json.loads(serialized)) == {
        "schema_version",
        "trading_date",
        "realized_pnl_ntd",
        "trade_count",
        "updated_at",
    }


def test_save_rejects_unknown_schema_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _store(tmp_path / "daily.json").save(DailyState(2, date(2026, 1, 9), 0.0, 0, _NOW))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("realized_pnl_ntd", "not-number"),
        ("trade_count", -1),
        ("updated_at", "2026-01-09T22:00:00"),
    ],
)
def test_invalid_field_types_are_unreadable(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "daily.json"
    payload: dict[str, object] = {
        "schema_version": 1,
        "trading_date": "2026-01-09",
        "realized_pnl_ntd": -100.0,
        "trade_count": 1,
        "updated_at": _NOW.isoformat(),
    }
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _store(path).load(_NOW).outcome is LoadOutcome.UNREADABLE
