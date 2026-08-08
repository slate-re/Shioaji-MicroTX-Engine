"""指數退避重試工具測試。"""

from __future__ import annotations

import logging

import pytest

from microtx.exceptions import BrokerError
from microtx.utils.retry import retry


def test_success_without_retry(mocker) -> None:
    sleep = mocker.patch("microtx.utils.retry.time.sleep")
    operation = mocker.Mock(return_value="完成")

    decorated = retry()(operation)

    assert decorated() == "完成"
    operation.assert_called_once_with()
    sleep.assert_not_called()


def test_succeeds_on_nth_attempt(mocker) -> None:
    sleep = mocker.patch("microtx.utils.retry.time.sleep")
    mocker.patch(
        "microtx.utils.retry._RANDOM.uniform", side_effect=lambda low, high: (low + high) / 2
    )
    operation = mocker.Mock(side_effect=[BrokerError("暫時失敗"), BrokerError("再試"), 42])

    decorated = retry(attempts=3, base_delay=0.5, max_delay=5.0)(operation)

    assert decorated() == 42
    assert operation.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == pytest.approx([0.5, 1.0])


def test_all_attempts_fail_with_original_exception(mocker) -> None:
    mocker.patch("microtx.utils.retry.time.sleep")
    mocker.patch(
        "microtx.utils.retry._RANDOM.uniform", side_effect=lambda low, high: (low + high) / 2
    )
    original = BrokerError("仍然失敗")
    operation = mocker.Mock(side_effect=original)

    decorated = retry(attempts=3)(operation)

    with pytest.raises(BrokerError) as caught:
        decorated()
    assert caught.value is original
    assert operation.call_count == 3


def test_delay_is_capped_before_jitter(mocker) -> None:
    sleep = mocker.patch("microtx.utils.retry.time.sleep")
    uniform = mocker.patch("microtx.utils.retry._RANDOM.uniform", side_effect=lambda low, high: low)
    operation = mocker.Mock(
        side_effect=[BrokerError("一"), BrokerError("二"), BrokerError("三"), 1]
    )

    decorated = retry(attempts=4, base_delay=2.0, max_delay=3.0)(operation)

    assert decorated() == 1
    actual_bounds = [bound for call in uniform.call_args_list for bound in call.args]
    assert actual_bounds == pytest.approx([1.6, 2.4, 2.4, 3.6, 2.4, 3.6])
    assert [call.args[0] for call in sleep.call_args_list] == pytest.approx([1.6, 2.4, 2.4])


def test_unlisted_exception_is_not_retried(mocker) -> None:
    sleep = mocker.patch("microtx.utils.retry.time.sleep")
    operation = mocker.Mock(side_effect=ValueError("不可重試"))
    decorated = retry(exceptions=(BrokerError,))(operation)

    with pytest.raises(ValueError, match="不可重試"):
        decorated()
    operation.assert_called_once_with()
    sleep.assert_not_called()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit()])
def test_process_control_exceptions_are_not_caught(mocker, error: BaseException) -> None:
    operation = mocker.Mock(side_effect=error)
    decorated = retry(exceptions=(Exception,))(operation)

    with pytest.raises(type(error)):
        decorated()
    operation.assert_called_once_with()


def test_retry_logs_attempt_and_delay(mocker, caplog) -> None:
    mocker.patch("microtx.utils.retry.time.sleep")
    mocker.patch("microtx.utils.retry._RANDOM.uniform", return_value=0.5)
    operation = mocker.Mock(side_effect=[BrokerError("暫時失敗"), "完成"])
    decorated = retry(logger_name="microtx.retry.test")(operation)

    with caplog.at_level(logging.WARNING, logger="microtx.retry.test"):
        assert decorated() == "完成"

    assert "第 1/3 次嘗試失敗" in caplog.text
    assert "下次等待 0.500 秒" in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"attempts": 0}, "嘗試次數"),
        ({"base_delay": -0.1}, "等待秒數"),
        ({"max_delay": -0.1}, "等待秒數"),
    ],
)
def test_invalid_retry_configuration(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        retry(**kwargs)
