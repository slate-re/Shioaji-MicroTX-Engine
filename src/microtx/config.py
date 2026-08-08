"""集中式設定管理。

所有機密與環境相關參數一律由 ``.env`` / 環境變數注入，
**程式碼中不出現任何金鑰、密碼或帳號字面值**。

設計要點：

* 使用 ``pydantic-settings`` 做型別驗證與範圍檢查，錯誤在啟動時就爆，而非交易中才爆。
* API 金鑰以 :class:`~pydantic.SecretStr` 儲存，``repr()`` 與日誌輸出自動遮蔽。
* 實盤需**兩道開關**同時打開（``SIMULATION=false`` 且 ``ALLOW_LIVE_TRADING=true``），
  避免誤觸真實下單。
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from microtx.contracts import FuturesSpec, get_spec

# 專案根目錄（.../src/microtx/config.py -> 往上三層）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_hhmm(value: str | time) -> time:
    """將 ``"HH:MM"`` 字串解析為 :class:`datetime.time`。"""
    if isinstance(value, time):
        return value
    hour, _, minute = value.strip().partition(":")
    return time(hour=int(hour), minute=int(minute))


class Settings(BaseSettings):
    """引擎執行期設定。

    欄位名稱即為對應的環境變數名稱（不分大小寫）。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    #  1. Shioaji 憑證（機密）
    # ------------------------------------------------------------------
    shioaji_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="永豐 Shioaji API Key",
    )
    shioaji_secret_key: SecretStr = Field(
        default=SecretStr(""),
        description="永豐 Shioaji Secret Key",
    )
    shioaji_ca_path: Path | None = Field(
        default=None,
        description="憑證檔（.pfx）路徑，僅正式環境下單需要",
    )
    shioaji_ca_password: SecretStr = Field(
        default=SecretStr(""),
        description="憑證密碼",
    )
    shioaji_person_id: SecretStr = Field(
        default=SecretStr(""),
        description="身分證字號，啟用憑證用",
    )

    # ------------------------------------------------------------------
    #  2. 執行模式（雙重防呆）
    # ------------------------------------------------------------------
    simulation: bool = Field(
        default=True,
        description="是否使用模擬環境。預設 True，clone 下來即可安全試跑",
    )
    allow_live_trading: bool = Field(
        default=False,
        description="實盤二次確認開關。即使 simulation=False，此項為 False 也不會送單",
    )

    # ------------------------------------------------------------------
    #  3. 交易標的
    # ------------------------------------------------------------------
    symbol: str = Field(
        default="TMFR1",
        description="交易商品代碼：TMFR1（微台）/ MXFR1（小台）/ TXFR1（大台）",
    )

    # ------------------------------------------------------------------
    #  4. 風險控管（硬性上限）
    # ------------------------------------------------------------------
    order_quantity: int = Field(default=1, ge=1, le=50, description="單筆下單口數")
    max_position_size: int = Field(default=2, ge=1, le=100, description="同時最大持倉口數")
    max_daily_loss: float = Field(default=3000.0, gt=0, description="單日最大虧損（NTD）")
    max_daily_trades: int = Field(default=10, ge=1, le=500, description="單日最大交易次數")
    order_cooldown_sec: float = Field(
        default=3.0, ge=0, le=300, description="兩次下單最短間隔（秒），防重複委託"
    )

    # ------------------------------------------------------------------
    #  5. 交易時段
    # ------------------------------------------------------------------
    session_start: time = Field(default=time(8, 45), description="日盤開盤")
    session_end: time = Field(default=time(13, 45), description="日盤收盤")
    force_close_time: time = Field(
        default=time(13, 40), description="強制平倉時間，建議早於收盤以留滑價餘裕"
    )
    enable_night_session: bool = Field(default=False, description="是否啟用夜盤")

    # ------------------------------------------------------------------
    #  6. 日誌
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO", description="DEBUG / INFO / WARNING / ERROR")
    log_dir: Path = Field(default=Path("logs"), description="日誌輸出目錄")
    log_retention_days: int = Field(default=30, ge=1, le=365, description="日誌保留天數")

    # ------------------------------------------------------------------
    #  7. 通知（選用）
    # ------------------------------------------------------------------
    telegram_bot_token: SecretStr = Field(default=SecretStr(""))
    telegram_chat_id: str = Field(default="")

    # ==================================================================
    #  驗證器
    # ==================================================================

    @field_validator("session_start", "session_end", "force_close_time", mode="before")
    @classmethod
    def _validate_time(cls, value: str | time) -> time:
        """允許 ``.env`` 以 ``"13:40"`` 形式提供時間。"""
        return _parse_hhmm(value)

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        """啟動時就驗證商品代碼合法，避免執行到一半才失敗。"""
        get_spec(value)  # 不合法會拋 ValueError
        return value.strip().upper()

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level 必須是 {sorted(allowed)} 之一，收到 {value!r}")
        return level

    @model_validator(mode="after")
    def _validate_session_window(self) -> Settings:
        """檢查時段設定的邏輯一致性。"""
        if self.session_start >= self.session_end:
            raise ValueError(
                f"session_start ({self.session_start}) 必須早於 session_end ({self.session_end})"
            )
        if not (self.session_start < self.force_close_time <= self.session_end):
            raise ValueError(
                f"force_close_time ({self.force_close_time}) 必須落在 "
                f"({self.session_start}, {self.session_end}] 區間內"
            )
        return self

    @model_validator(mode="after")
    def _validate_live_credentials(self) -> Settings:
        """實盤模式的前置條件檢查。"""
        if self.is_live:
            if not self.shioaji_api_key.get_secret_value():
                raise ValueError("實盤模式必須提供 SHIOAJI_API_KEY")
            if not self.shioaji_secret_key.get_secret_value():
                raise ValueError("實盤模式必須提供 SHIOAJI_SECRET_KEY")
            if self.shioaji_ca_path is None or not self.shioaji_ca_path.exists():
                raise ValueError(
                    f"實盤模式必須提供有效的憑證檔路徑，目前為 {self.shioaji_ca_path!r}"
                )
        return self

    # ==================================================================
    #  衍生屬性
    # ==================================================================

    @property
    def is_live(self) -> bool:
        """是否為真正的實盤下單模式（兩道開關同時打開）。"""
        return (not self.simulation) and self.allow_live_trading

    @property
    def spec(self) -> FuturesSpec:
        """目前交易商品的規格。"""
        return get_spec(self.symbol)

    @property
    def max_daily_loss_points(self) -> float:
        """單日最大虧損換算成點數，方便與策略的點數邏輯比較。"""
        return self.spec.ntd_to_points(self.max_daily_loss)

    @property
    def log_path(self) -> Path:
        """日誌目錄的絕對路徑。"""
        path = self.log_dir
        return path if path.is_absolute() else PROJECT_ROOT / path

    def summary(self) -> str:
        """產生可安全寫入日誌的設定摘要（不含任何機密）。"""
        mode = "🔴 實盤 LIVE" if self.is_live else "🟢 模擬 SIMULATION"
        return (
            f"模式={mode} | 商品={self.spec.display_name}({self.symbol}) "
            f"每點={self.spec.point_value}元 | 口數={self.order_quantity} "
            f"最大持倉={self.max_position_size} | 單日停損={self.max_daily_loss:,.0f}元 "
            f"({self.max_daily_loss_points:.0f}點) | 單日上限={self.max_daily_trades}筆 "
            f"| 強平={self.force_close_time.strftime('%H:%M')}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取得全域唯一的設定實例（單例，帶快取）。

    Returns:
        已驗證的 :class:`Settings`。

    Raises:
        pydantic.ValidationError: 任一設定不合法。
    """
    return Settings()
