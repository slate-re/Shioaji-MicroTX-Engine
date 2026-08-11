"""CLI 危險操作、健康狀態與離線 Demo 測試。"""

from __future__ import annotations

import json
import signal
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from microtx.cli.commands import main
from microtx.config import Settings

_TAIPEI = ZoneInfo("Asia/Taipei")


def test_panic_when_engine_is_not_running_warns_manual_close(mocker, capsys) -> None:
    mocker.patch("microtx.cli.commands.PidFile.read_pid", return_value=None)
    assert main(["panic", "--yes"]) == 1
    error = capsys.readouterr().err
    assert "引擎未運行" in error
    assert "永豐下單軟體手動平倉" in error


def test_panic_and_flatten_send_distinct_signals(mocker, capsys) -> None:
    mocker.patch("microtx.cli.commands.PidFile.read_pid", return_value=4321)
    kill = mocker.patch("microtx.cli.commands.os.kill")
    assert main(["panic", "--yes"]) == 0
    kill.assert_called_with(4321, signal.SIGUSR1)
    assert main(["flatten", "--yes"]) == 0
    kill.assert_called_with(4321, signal.SIGUSR2)
    assert "PANIC" in capsys.readouterr().out


def test_dangerous_command_without_yes_is_rejected_when_noninteractive(mocker) -> None:
    mocker.patch("microtx.cli.commands.PidFile.read_pid", return_value=4321)
    mocker.patch("microtx.cli.commands.sys.stdin.isatty", return_value=False)
    kill = mocker.patch("microtx.cli.commands.os.kill")
    assert main(["panic"]) == 2
    kill.assert_not_called()


def test_help_and_demo_work_without_env_or_shioaji(monkeypatch, capsys) -> None:
    monkeypatch.chdir(Path(__file__).parents[1])
    assert main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "離線 Demo 完成" in output
    assert "CLOSED" in output


def test_live_run_without_confirmation_is_rejected(mocker, monkeypatch) -> None:
    settings = mocker.Mock(spec=Settings, is_live=True, summary=lambda: "LIVE")
    mocker.patch("microtx.cli.commands.Settings", return_value=settings)
    mocker.patch("microtx.cli.commands.sys.stdin.isatty", return_value=False)
    monkeypatch.delenv("MICROTX_CONFIRM_LIVE", raising=False)
    assert main(["run"]) == 2


def _status_payload(written_at: datetime, *, degraded: bool = False) -> dict[str, object]:
    return {
        "written_at": written_at.isoformat(),
        "engine_state": "RUNNING",
        "degraded": degraded,
    }


def test_status_reports_healthy_degraded_and_stale(mocker, tmp_path: Path, capsys) -> None:
    status_file = tmp_path / "status.json"
    settings = Settings(
        _env_file=None,
        pid_file=tmp_path / "engine.pid",
        status_file=status_file,
        status_write_interval_sec=5.0,
    )
    mocker.patch("microtx.cli.commands.Settings", return_value=settings)
    mocker.patch("microtx.cli.commands.PidFile.read_pid", return_value=4321)

    status_file.write_text(json.dumps(_status_payload(datetime.now(_TAIPEI))), encoding="utf-8")
    assert main(["status"]) == 0
    assert "RUNNING" in capsys.readouterr().out

    status_file.write_text(
        json.dumps(_status_payload(datetime.now(_TAIPEI), degraded=True)), encoding="utf-8"
    )
    assert main(["status"]) == 3
    assert "panic" in capsys.readouterr().err

    status_file.write_text(
        json.dumps(_status_payload(datetime.now(_TAIPEI) - timedelta(seconds=16))),
        encoding="utf-8",
    )
    assert main(["status"]) == 2
    assert "無回應" in capsys.readouterr().err
