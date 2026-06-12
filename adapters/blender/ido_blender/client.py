from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BackendError(RuntimeError):
    pass


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

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

