"""狀態快照的原子性、降級路徑與機密隔離測試。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, RLock, Thread

from microtx.broker.paper_gateway import PaperGateway
from microtx.config import Settings
from microtx.contracts import TMF
from microtx.engine.engine import TradingEngine
from microtx.engine.status import StatusWriter


def _engine(tmp_path: Path) -> TradingEngine:
    settings = Settings(
        _env_file=None,
        pid_file=tmp_path / "engine.pid",
        status_file=tmp_path / "status.json",
    )
    return TradingEngine(settings, PaperGateway(spec=TMF))


def test_snapshot_serialization_never_contains_secret_values(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        shioaji_api_key="API_KEY_NEVER_WRITE",
        shioaji_secret_key="SECRET_NEVER_WRITE",
        shioaji_ca_password="CA_PASSWORD_NEVER_WRITE",
        shioaji_person_id="PERSON_ID_NEVER_WRITE",
        telegram_bot_token="BOT_TOKEN_NEVER_WRITE",
        telegram_chat_id="ACCOUNT_CODE_NEVER_WRITE",
        status_file=tmp_path / "status.json",
    )
    engine = TradingEngine(settings, PaperGateway(spec=TMF))

    serialized = json.dumps(engine._full_status_snapshot().to_dict(), ensure_ascii=False)

    for secret in (
        "API_KEY_NEVER_WRITE",
        "SECRET_NEVER_WRITE",
        "CA_PASSWORD_NEVER_WRITE",
        "PERSON_ID_NEVER_WRITE",
        "BOT_TOKEN_NEVER_WRITE",
        "ACCOUNT_CODE_NEVER_WRITE",
    ):
        assert secret not in serialized


def test_writer_uses_degraded_snapshot_when_shared_lock_is_busy(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    locked = Event()
    release = Event()

    def hold_lock() -> None:
        with engine._shared_lock:
            locked.set()
            release.wait(2.0)

    holder = Thread(target=hold_lock)
    holder.start()
    assert locked.wait(1.0)
    started = time.monotonic()
    engine._status_writer.write_once()
    elapsed = time.monotonic() - started
    release.set()
    holder.join(1.0)

    payload = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert elapsed < 1.0
    assert payload["degraded"] is True
    assert payload["degraded_reason"] == "無法取得共用鎖"


def test_atomic_writer_never_exposes_partial_json(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._status_writer.write_once()
    errors: list[Exception] = []

    def read_repeatedly() -> None:
        for _ in range(200):
            try:
                json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(exc)

    reader = Thread(target=read_repeatedly)
    reader.start()
    for _ in range(30):
        engine._status_writer.write_once()
    reader.join()
    assert errors == []


def test_write_failure_only_logs_warning(mocker, tmp_path: Path) -> None:
    lock = RLock()
    engine = _engine(tmp_path)
    writer = StatusWriter(
        tmp_path / "status.json",
        interval_sec=1.0,
        lock=lock,
        full_snapshot=engine._full_status_snapshot,
        degraded_snapshot=engine._degraded_status_snapshot,
    )
    warning = mocker.patch("microtx.engine.status.logger.warning")
    mocker.patch.object(writer, "_write_payload", side_effect=OSError("唯讀"))

    writer.write_once()

    warning.assert_called_once()
