from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from time import perf_counter
from typing import Any

from shared.contracts import TraceEvent

logger = logging.getLogger("cad_agent.trace")


class TraceStore:
    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def record(
        self,
        request_id: str,
        step: str,
        status: str,
        *,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            request_id=request_id,
            step=step,
            status=status,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        async with self._lock:
            self._events[request_id].append(event)
        logger.info("trace_event %s", json.dumps(event.model_dump(mode="json")))
        return event

    async def get(self, request_id: str) -> list[TraceEvent]:
        async with self._lock:
            return list(self._events.get(request_id, []))


class StepTimer:
    def __init__(self) -> None:
        self._started = perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return round((perf_counter() - self._started) * 1000, 3)
