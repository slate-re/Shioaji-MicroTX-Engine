"""可插拔通知管道的最底層契約。"""

from __future__ import annotations

from typing import Protocol

from microtx.enums import NotifyLevel


class Notifier(Protocol):
    """通知管道的結構型別。"""

    def notify(self, level: NotifyLevel, title: str, body: str) -> None:
        """送出一則已格式化的通知。"""
