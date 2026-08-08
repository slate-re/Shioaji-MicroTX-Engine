"""設定載入與驗證單元測試。

重點在**安全性驗證**：預設必須是模擬模式、機密不得外洩至字串輸出。
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from microtx.config import Settings


def make_settings(**overrides: object) -> Settings:
    """建立不讀取 ``.env`` 的測試用設定實例。"""
    defaults: dict[str, object] = {
        "shioaji_api_key": "test_key",
        "shioaji_secret_key": "test_secret",
        "simulation": True,
        "allow_live_trading": False,
        "symbol": "TMFR1",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


class TestDefaults:
    """預設值必須對「clone 下來直接跑」的人是安全的。"""

    def test_simulation_is_default_on(self) -> None:
        """預設走模擬環境，面試官 clone 下來不會誤下實單。"""
        settings = make_settings()
        assert settings.simulation is True
        assert settings.is_live is False

    def test_default_symbol_is_micro(self) -> None:
        """預設微台，門檻最低。"""
        assert make_settings().spec.point_value == 10


class TestLiveTradingGuard:
    """實盤雙開關防呆。"""

    def test_simulation_false_alone_is_not_live(self) -> None:
        """只關掉 simulation 還不夠，仍不算實盤。"""
        settings = make_settings(simulation=False, allow_live_trading=False)
        assert settings.is_live is False

    def test_live_requires_certificate(self, tmp_path: Path) -> None:
        """兩道開關全開但缺憑證，應在啟動時就報錯。"""
        with pytest.raises(ValidationError, match="憑證"):
            make_settings(simulation=False, allow_live_trading=True, shioaji_ca_path=None)

    def test_live_with_valid_certificate(self, tmp_path: Path) -> None:
        """憑證存在時才允許進入實盤模式。"""
        ca = tmp_path / "cert.pfx"
        ca.write_bytes(b"dummy")
        settings = make_settings(simulation=False, allow_live_trading=True, shioaji_ca_path=ca)
        assert settings.is_live is True


class TestSecretMasking:
    """機密不得出現在任何字串輸出中。"""

    def test_repr_does_not_leak(self) -> None:
        settings = make_settings(shioaji_api_key="SUPER_SECRET_KEY_123")
        assert "SUPER_SECRET_KEY_123" not in repr(settings)
        assert "SUPER_SECRET_KEY_123" not in str(settings)

    def test_summary_does_not_leak(self) -> None:
        settings = make_settings(shioaji_secret_key="SUPER_SECRET_VALUE")
        assert "SUPER_SECRET_VALUE" not in settings.summary()

    def test_secret_still_retrievable(self) -> None:
        """遮蔽只針對輸出，程式仍需取得真值。"""
        settings = make_settings(shioaji_api_key="abc123")
        assert settings.shioaji_api_key.get_secret_value() == "abc123"


class TestValidation:
    """設定合法性在啟動時就驗證，不留到交易中才爆。"""

    def test_invalid_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(symbol="AAPL")

    def test_force_close_must_be_within_session(self) -> None:
        with pytest.raises(ValidationError, match="force_close_time"):
            make_settings(force_close_time=time(14, 30))

    def test_session_start_before_end(self) -> None:
        with pytest.raises(ValidationError, match="session_start"):
            make_settings(session_start=time(14, 0), session_end=time(9, 0))

    def test_time_string_parsing(self) -> None:
        """``.env`` 以 ``"13:40"`` 字串提供時間應能正確解析。"""
        settings = make_settings(force_close_time="13:30")
        assert settings.force_close_time == time(13, 30)

    def test_order_quantity_bounds(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(order_quantity=0)

    def test_max_daily_loss_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(max_daily_loss=-1)

    def test_invalid_log_level(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            make_settings(log_level="VERBOSE")


class TestDerived:
    """衍生屬性。"""

    def test_max_daily_loss_points_scales_with_symbol(self) -> None:
        """同樣 3000 元，微台是 300 點、小台只有 60 點。"""
        assert make_settings(symbol="TMFR1", max_daily_loss=3000).max_daily_loss_points == 300
        assert make_settings(symbol="MXFR1", max_daily_loss=3000).max_daily_loss_points == 60
