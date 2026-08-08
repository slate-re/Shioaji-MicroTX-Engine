"""日誌機密遮蔽測試——開源專案的最後一道防線。"""

from __future__ import annotations

import logging
from pathlib import Path

from microtx.utils.logger import SecretMaskingFilter, get_logger, setup_logging


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="microtx.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TestSecretMaskingFilter:
    """各種常見機密寫法都要被遮蔽。"""

    def test_masks_api_key(self) -> None:
        rec = _record("login with api_key=sk_live_abcd1234efgh")
        SecretMaskingFilter().filter(rec)
        assert "sk_live_abcd1234efgh" not in rec.msg
        assert SecretMaskingFilter.MASK in rec.msg

    def test_masks_various_forms(self) -> None:
        samples = [
            "secret_key: mysecret999",
            "PASSWORD=hunter2",
            "token = ghp_XXXXYYYYZZZZ",
            "person_id='A123456789'",
        ]
        leaked = ["mysecret999", "hunter2", "ghp_XXXXYYYYZZZZ", "A123456789"]
        for sample, secret in zip(samples, leaked, strict=True):
            rec = _record(sample)
            SecretMaskingFilter().filter(rec)
            assert secret not in rec.msg, f"{sample!r} 未被遮蔽"

    def test_keeps_normal_message_intact(self) -> None:
        rec = _record("已送出委託 TMFR1 Buy 1 口 @ 23150")
        SecretMaskingFilter().filter(rec)
        assert rec.msg == "已送出委託 TMFR1 Buy 1 口 @ 23150"

    def test_filter_never_drops_records(self) -> None:
        assert SecretMaskingFilter().filter(_record("anything")) is True


class TestSetupLogging:
    def test_writes_masked_content_to_file(self, tmp_path: Path) -> None:
        """實際寫入檔案時也必須是遮蔽後的內容。"""
        logger = setup_logging(level="DEBUG", log_dir=tmp_path, logger_name="microtx_test_file")
        logger.info("connecting with api_key=LEAKED_SECRET_XYZ")
        for handler in logger.handlers:
            handler.flush()

        content = (tmp_path / "microtx.log").read_text(encoding="utf-8")
        assert "LEAKED_SECRET_XYZ" not in content
        assert SecretMaskingFilter.MASK in content

    def test_idempotent_no_duplicate_handlers(self, tmp_path: Path) -> None:
        """重複初始化不應造成日誌重複輸出。"""
        for _ in range(3):
            logger = setup_logging(level="INFO", log_dir=tmp_path, logger_name="microtx_test_idem")
        assert len(logger.handlers) == 2  # console + file

    def test_get_logger_namespacing(self) -> None:
        assert get_logger("engine.risk").name == "microtx.engine.risk"
        assert get_logger("microtx.market.feed").name == "microtx.market.feed"
