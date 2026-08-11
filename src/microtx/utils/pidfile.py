"""常駐程序 PID 檔的安全生命週期管理。"""

from __future__ import annotations

import os
from pathlib import Path

from microtx.exceptions import MicroTXError


class PidFile:
    """避免引擎重複啟動，並自動清除陳舊 PID 檔。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._acquired = False

    def acquire(self) -> None:
        """寫入目前 PID；已有存活程序時拒絕重複啟動。

        Raises:
            MicroTXError: PID 檔指向仍存活的程序，或無法安全建立 PID 檔。
        """
        live_pid = self.read_pid(self._path)
        if live_pid is not None:
            raise MicroTXError(f"引擎已在運行（PID {live_pid}）")
        try:
            self._path.unlink(missing_ok=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
        except OSError as exc:
            raise MicroTXError(f"無法建立 PID 檔：{self._path}") from exc
        self._acquired = True

    def release(self) -> None:
        """僅移除由目前實例取得的 PID 檔。"""
        if not self._acquired:
            return
        try:
            self._path.unlink(missing_ok=True)
        finally:
            self._acquired = False

    def __enter__(self) -> PidFile:
        """取得 PID 檔並回傳自身。"""
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        """離開 context 時釋放 PID 檔。"""
        self.release()

    @staticmethod
    def read_pid(path: Path) -> int | None:
        """讀取 PID 並確認程序仍存活；無效或陳舊時回傳 None。"""
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            if pid <= 0:
                return None
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, PermissionError, ProcessLookupError, OSError):
            return None
        return pid
