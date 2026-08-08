"""可重用的指數退避重試工具。"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from secrets import SystemRandom
from typing import ParamSpec, TypeVar

from microtx.exceptions import BrokerError
from microtx.utils.logger import get_logger

P = ParamSpec("P")
T = TypeVar("T")
_RANDOM = SystemRandom()


def retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    exceptions: tuple[type[Exception], ...] = (BrokerError,),
    logger_name: str = "microtx.retry",
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """以帶抖動的指數退避重試指定例外。

    Args:
        attempts: 包含第一次呼叫在內的最大嘗試次數。
        base_delay: 第一次重試前的基礎等待秒數。
        max_delay: 單次等待秒數上限。
        exceptions: 可觸發重試的例外類型。
        logger_name: 寫入重試警告的 logger 名稱。

    Returns:
        保留原函式參數與回傳型別的裝飾器。

    Raises:
        ValueError: 嘗試次數小於 1，或等待秒數為負數。
    """
    if attempts < 1:
        raise ValueError("嘗試次數必須至少為 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("重試等待秒數不可為負數")

    logger = get_logger(logger_name)

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == attempts:
                        raise
                    delay = min(base_delay * 2 ** (attempt - 1), max_delay)
                    jittered_delay = _RANDOM.uniform(delay * 0.8, delay * 1.2)
                    logger.warning(
                        "第 %d/%d 次嘗試失敗，下次等待 %.3f 秒",
                        attempt,
                        attempts,
                        jittered_delay,
                    )
                    time.sleep(jittered_delay)

            raise AssertionError("重試迴圈不應執行至此")

        return wrapper

    return decorator
