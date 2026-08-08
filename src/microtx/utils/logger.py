"""日誌設定。

設計要點：

* **雙通道輸出**：終端機（人類閱讀，帶顏色）+ 檔案（按日輪替，供事後稽核）。
* **機密遮蔽**：透過 :class:`SecretMaskingFilter` 攔截疑似 API Key / Token 的字串，
  避免任何機密意外寫進日誌檔（開源專案的最後一道防線）。
* 日誌檔統一放在 ``logs/``，該目錄已被 ``.gitignore`` 排除。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Final

_LOG_FORMAT: Final = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

#: 需要遮蔽的敏感關鍵字樣式（key=value 或 key: value 形式）
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(api[_-]?key|secret[_-]?key|password|token|person[_-]?id)"
        r"(\s*[=:]\s*)(['\"]?)([^\s'\",;]+)",
        re.IGNORECASE,
    ),
)

#: 終端機顏色（ANSI），非 TTY 時自動停用
_COLORS: Final[dict[int, str]] = {
    logging.DEBUG: "\033[38;5;244m",
    logging.INFO: "\033[38;5;39m",
    logging.WARNING: "\033[38;5;214m",
    logging.ERROR: "\033[38;5;196m",
    logging.CRITICAL: "\033[1;97;41m",
}
_RESET: Final = "\033[0m"


class SecretMaskingFilter(logging.Filter):
    """遮蔽日誌訊息中疑似機密的內容。

    將 ``api_key=abcd1234`` 這類字串改寫為 ``api_key=***MASKED***``。
    這是防禦性設計：即使某處程式碼誤把設定物件整包印出，也不會外洩。
    """

    MASK: Final = "***MASKED***"

    def filter(self, record: logging.LogRecord) -> bool:
        """就地改寫 record 訊息。

        Args:
            record: 日誌記錄。

        Returns:
            永遠回傳 ``True``（不丟棄任何記錄，只做遮蔽）。
        """
        if isinstance(record.msg, str):
            record.msg = self._mask(record.msg)
        if record.args:
            record.args = tuple(
                self._mask(a) if isinstance(a, str) else a for a in _as_tuple(record.args)
            )
        return True

    @classmethod
    def _mask(cls, text: str) -> str:
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(rf"\1\2\3{cls.MASK}", text)
        return text


def _as_tuple(args: object) -> tuple[object, ...]:
    """將 ``LogRecord.args`` 正規化為 tuple（可能是 dict 或 tuple）。"""
    if isinstance(args, tuple):
        return args
    return (args,)


class _ColorFormatter(logging.Formatter):
    """終端機用的彩色格式器。"""

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self._use_color:
            return text
        color = _COLORS.get(record.levelno, "")
        return f"{color}{text}{_RESET}" if color else text


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path | None = None,
    retention_days: int = 30,
    logger_name: str = "microtx",
) -> logging.Logger:
    """初始化並回傳專案根 logger。

    重複呼叫是安全的（會先清除既有 handler，避免日誌重複輸出）。

    Args:
        level: 日誌等級字串，如 ``"INFO"``。
        log_dir: 日誌輸出目錄；``None`` 表示只輸出到終端機。
        retention_days: 日誌檔保留天數（按日輪替）。
        logger_name: 根 logger 名稱。

    Returns:
        設定完成的 :class:`logging.Logger`。
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # 清除舊 handler，確保冪等
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    masking_filter = SecretMaskingFilter()

    # --- 終端機 ---
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))
    console.addFilter(masking_filter)
    logger.addHandler(console)

    # --- 檔案（按日輪替）---
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / "microtx.log",
            when="midnight",
            backupCount=retention_days,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.addFilter(masking_filter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """取得子模組 logger。

    Args:
        name: 通常直接傳入 ``__name__``。

    Returns:
        名稱掛在 ``microtx`` 底下的 logger，共用根 logger 的 handler 與遮蔽過濾器。
    """
    if name.startswith("microtx"):
        return logging.getLogger(name)
    return logging.getLogger(f"microtx.{name}")
