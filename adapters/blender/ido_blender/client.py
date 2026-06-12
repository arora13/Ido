from __future__ import annotations

import codecs
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BackendError(RuntimeError):
    pass


class SSEDecoder:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer += self._decoder.decode(chunk)
        return self._drain()

    def finish(self) -> list[dict[str, Any]]:
        self._buffer += self._decoder.decode(b"", final=True)
        events = self._drain()
        if self._buffer.strip():
            events.extend(self._parse_frame(self._buffer))
        self._buffer = ""
        return events

    def _drain(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        normalized = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        frames = normalized.split("\n\n")
        self._buffer = frames.pop()
        for frame in frames:
            events.extend(self._parse_frame(frame))
        return events

    @staticmethod
    def _parse_frame(frame: str) -> list[dict[str, Any]]:
        data = "\n".join(
            line[5:].lstrip()
            for line in frame.splitlines()
            if line.startswith("data:")
        )
        if not data:
            return []
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise BackendError("Backend returned an invalid stream event") from exc
        if not isinstance(event, dict):
            raise BackendError("Backend returned a non-object stream event")
        return [event]


class BackendClient:
    def __init__(self, base_url: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def prompt(
        self,
        prompt: str,
        current_ir: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/prompt",
            {
                "prompt": prompt,
                "current_ir": current_ir,
                "target_tool": "blender",
            },
        )

    def prompt_stream(
        self,
        prompt: str,
        current_ir: dict[str, Any] | None,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "prompt": prompt,
                "current_ir": current_ir,
                "target_tool": "blender",
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/prompt/stream",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        decoder = SSEDecoder()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                while chunk := response.readline():
                    for event in decoder.feed(chunk):
                        result = self._handle_stream_event(event, on_event)
                        if result is not None:
                            return result
                for event in decoder.finish():
                    result = self._handle_stream_event(event, on_event)
                    if result is not None:
                        return result
        except HTTPError as exc:
            if exc.code in {404, 405}:
                on_event(
                    {
                        "type": "status",
                        "message": "Streaming unavailable; waiting for generation...",
                    }
                )
                return self.prompt(prompt, current_ir)
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendError(f"Backend returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BackendError(
                f"Cannot reach CAD-Agent backend at {self.base_url}: {exc.reason}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise BackendError("Backend returned an invalid UTF-8 stream") from exc
        raise BackendError("Backend stream ended before a final result")

    @staticmethod
    def _handle_stream_event(
        event: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        on_event(event)
        event_type = event.get("type")
        result = event.get("result")
        if event_type in {"done", "error"} and isinstance(result, dict):
            return result
        if event_type == "error":
            raise BackendError(str(event.get("message") or "Generation failed"))
        return None

    def report_execution(
        self,
        *,
        request_id: str,
        status: str,
        duration_ms: float,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/execution",
            {
                "request_id": request_id,
                "target_tool": "blender",
                "status": status,
                "duration_ms": duration_ms,
                "error": error,
            },
        )

    def get_trace(self, request_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/api/traces/{request_id}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendError(f"Backend returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BackendError(
                f"Cannot reach CAD-Agent backend at {self.base_url}: {exc.reason}"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackendError("Backend returned an invalid JSON response") from exc
