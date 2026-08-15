"""CLI 危險操作、健康狀態與離線 Demo 測試。"""

from __future__ import annotations

import json
import signal
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from microtx.cli.commands import _strategy_from_args, build_parser, main
from microtx.config import Settings
from microtx.strategies.oco import OcoStrategy

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


def test_scalp_cli_rejects_mixed_take_profit_modes(capsys) -> None:
    result = main(
        [
            "run",
            "--strategy",
            "scalp",
            "--direction",
            "long",
            "--trigger",
            "46500",
            "--tp",
            "50",
            "--tp-price",
            "46600",
            "--sl-price",
            "46400",
        ]
    )
    assert result == 2
    assert "只能擇一" in capsys.readouterr().err


def test_oco_cli_builds_four_independent_absolute_levels() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--strategy",
            "oco",
            "--upper",
            "46500",
            "--lower",
            "46300",
            "--long-tp-price",
            "46600",
            "--long-sl-price",
            "46450",
            "--short-tp-price",
            "46200",
            "--short-sl-price",
            "46350",
        ]
    )
    strategy = _strategy_from_args(args, Settings(_env_file=None))
    assert isinstance(strategy, OcoStrategy)
    assert strategy._long.stop_price == 46_450.0
    assert strategy._short.stop_price == 46_350.0


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


def test_cli_builds_independent_execution_styles() -> None:
    from microtx.enums import ExecutionStyle

    args = build_parser().parse_args(
        [
            "run",
            "--strategy",
            "scalp",
            "--direction",
            "long",
            "--trigger",
            "46500",
            "--tp-price",
            "46600",
            "--sl-price",
            "46400",
            "--entry-order",
            "limit",
            "--tp-order",
            "limit",
        ]
    )
    strategy = _strategy_from_args(args, Settings(_env_file=None))
    assert strategy is not None
    assert strategy._entry_style is ExecutionStyle.LIMIT
    assert strategy._take_profit_style is ExecutionStyle.LIMIT
    assert strategy._stop_loss_style is ExecutionStyle.MARKET


def test_cli_rejects_invalid_execution_style(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["run", "--entry-order", "iceberg"])
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "market" in error
    assert "limit" in error


def test_panic_and_flatten_expose_no_execution_style_switches() -> None:
    panic = build_parser().parse_args(["panic", "--yes"])
    flatten = build_parser().parse_args(["flatten", "--yes"])
    assert not hasattr(panic, "entry_order")
    assert not hasattr(panic, "tp_order")
    assert not hasattr(panic, "sl_order")
    assert not hasattr(flatten, "entry_order")
    assert not hasattr(flatten, "tp_order")
    assert not hasattr(flatten, "sl_order")


def test_limit_stop_prints_startup_warning(mocker, capsys) -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--strategy",
            "scalp",
            "--direction",
            "long",
            "--trigger",
            "46500",
            "--tp-price",
            "46600",
            "--sl-price",
            "46400",
            "--sl-order",
            "limit",
        ]
    )
    settings = Settings(_env_file=None)
    mocker.patch("microtx.broker.shioaji_gateway.ShioajiGateway")
    engine = mocker.patch("microtx.cli.commands.TradingEngine")

    from microtx.cli.commands import _run

    assert _run(args, settings) == 0
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "SL@46400" in warning
    assert "持續裸露" in warning
    engine.return_value.run_forever.assert_called_once_with()
