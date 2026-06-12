from __future__ import annotations

from threading import RLock
from typing import Any

from shared.contracts import RuntimeStatus


class StatusStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._status = RuntimeStatus()

    def get(self) -> RuntimeStatus:
        with self._lock:
            return self._status.model_copy(deep=True)

    def update(self, **changes: Any) -> RuntimeStatus:
        with self._lock:
            payload = self._status.model_dump()
            payload.update(changes)
            payload.pop("updated_at", None)
            self._status = RuntimeStatus(**payload)
            return self._status.model_copy(deep=True)

    def fail(self, *, tool: str, message: str, request_id: str | None = None) -> RuntimeStatus:
        errors = [message, *self.get().recent_errors][:5]
        return self.update(
            tool=tool,
            phase="failed",
            message=message,
            request_id=request_id,
            recent_errors=errors,
        )
