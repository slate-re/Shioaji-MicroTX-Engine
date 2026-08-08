"""MicroTX 專案例外體系。"""

from __future__ import annotations


class MicroTXError(Exception):
    """本專案所有可預期例外的根類別。"""


class ConfigError(MicroTXError):
    """設定缺漏、格式錯誤或安全限制不符時拋出。"""


class BrokerError(MicroTXError):
    """券商操作或回應發生錯誤時拋出。"""


class ConnectionLostError(BrokerError):
    """與券商的連線中斷，且當下操作無法繼續時拋出。"""


class OrderRejectedError(BrokerError):
    """券商拒絕委託時拋出，並保留可供追查的拒絕代碼與委託識別碼。"""

    def __init__(self, message: str, *, code: str = "", client_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.client_id = client_id

    def __str__(self) -> str:
        """回傳包含拒絕代碼與委託識別碼的診斷文字。"""
        return f"{super().__str__()} (code={self.code}, client_id={self.client_id})"


class RiskViolationError(MicroTXError):
    """委託違反風控限制，無法放行時拋出。"""


class StrategyError(MicroTXError):
    """策略狀態或輸入不合法，導致策略無法繼續時拋出。"""


class EmergencyCloseError(MicroTXError):
    """緊急平倉內部流程無法繼續，或 CLI 無法完成請求時拋出。"""

    def __init__(
        self,
        message: str,
        *,
        mode: str = "",
        source: str = "",
        residual_quantity: int = 0,
    ) -> None:
        super().__init__(message)
        self.mode = mode
        self.source = source
        self.residual_quantity = residual_quantity

    def __str__(self) -> str:
        """回傳包含平倉模式、來源與殘餘口數的診斷文字。"""
        return (
            f"{super().__str__()} (mode={self.mode}, source={self.source}, "
            f"residual_quantity={self.residual_quantity})"
        )
