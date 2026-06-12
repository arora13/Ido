import asyncio
import json
from time import monotonic
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from backend.clickhouse_exporter import ClickHouseExportResult, NullClickHouseTraceExporter
from backend.guild_exporter import GuildExportResult, NullGuildTraceExporter
from backend.main import create_app
from backend.providers.base import IRGenerationError
from backend.providers.fallback import DeterministicProvider


class FailingProvider:
    name = "failing"

    async def generate(self, _prompt, _current_ir):
        raise IRGenerationError("provider unavailable")


class SlowProvider:
    name = "slow"

    async def generate(self, prompt, current_ir):
        await asyncio.sleep(1.1)
        return await DeterministicProvider().generate(prompt, current_ir)


class StreamingProvider:
    name = "streaming"

    async def generate(self, prompt, current_ir):
        return await DeterministicProvider().generate(prompt, current_ir)

    async def generate_stream(self, prompt, current_ir, progress):
        result = await self.generate(prompt, current_ir)
        encoded = result.model_dump_json()
        for start in range(0, len(encoded), 37):
            await progress({"type": "code", "content": encoded[start : start + 37]})
        return result


def stream_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.iter_lines()
        if line.startswith("data: ")
    ]


class RecordingGuildExporter(NullGuildTraceExporter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, list]] = []
        self.export = AsyncMock(side_effect=self._export)

    async def _export(self, request_id, events, *, prompt=None, target_tool=None, scene_snapshot=None):
        self.calls.append((request_id, list(events)))
        return GuildExportResult(
            exported=True,
            trace_view_url=f"https://app.guild.ai/sessions/{request_id}",
        )

    @property
    def enabled(self) -> bool:
        return True


class RecordingClickHouseExporter(NullClickHouseTraceExporter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, list]] = []
        self.export = AsyncMock(side_effect=self._export)

    async def _export(self, request_id, events, *, prompt=None, target_tool=None, scene_snapshot=None):
        self.calls.append((request_id, list(events)))
        return ClickHouseExportResult(exported=True, rows=len(events))

    @property
    def enabled(self) -> bool:
        return True


def test_health_and_iterative_prompt_flow() -> None:
    client = TestClient(create_app(provider=DeterministicProvider()))

    health = client.get("/api/health")
    first = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    )
    second = client.post(
        "/api/prompt",
        json={
            "prompt": "add more windows",
            "current_ir": first.json()["ir"],
            "target_tool": "blender",
        },
    )

    assert health.json()["provider"] == "deterministic"
    assert first.json()["status"] == "ok"
    assert len(second.json()["ir"]["scene"]["objects"]) == 9
    assert [event["step"] for event in first.json()["trace"]] == [
        "parse",
        "parse",
        "validate",
        "validate",
        "route",
        "route",
    ]


def test_provider_error_is_returned_without_server_exception() -> None:
    client = TestClient(create_app(provider=FailingProvider()))

    response = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"] == "provider unavailable"
    assert "Stack(" in response.json()["openui_lang"]


def test_prompt_stream_matches_prompt_response_and_emits_objects() -> None:
    client = TestClient(create_app(provider=DeterministicProvider()))
    payload = {
        "prompt": "make a chair",
        "current_ir": None,
        "target_tool": "blender",
    }

    regular = client.post("/api/prompt", json=payload).json()
    streamed = stream_events(client.post("/api/prompt/stream", json=payload))

    assert streamed[0]["type"] == "status"
    assert streamed[0]["elapsed_seconds"] == 0
    assert any(event["type"] == "object" for event in streamed)
    assert streamed[-1]["type"] == "done"
    result = streamed[-1]["result"]
    assert result["status"] == regular["status"] == "ok"
    assert result["ir"] == regular["ir"]
    assert result["provider"] == regular["provider"]


def test_prompt_stream_emits_heartbeat_during_slow_generation() -> None:
    client = TestClient(create_app(provider=SlowProvider()))
    started = monotonic()

    events = stream_events(
        client.post(
            "/api/prompt/stream",
            json={
                "prompt": "make a chair",
                "current_ir": None,
                "target_tool": "blender",
            },
        )
    )

    assert monotonic() - started >= 1.0
    assert any(event.get("elapsed_seconds", 0) >= 1 for event in events)
    assert events[-1]["type"] == "done"


def test_prompt_stream_emits_objects_while_provider_is_still_streaming() -> None:
    client = TestClient(create_app(provider=StreamingProvider()))

    events = stream_events(
        client.post(
            "/api/prompt/stream",
            json={
                "prompt": "make a chair",
                "current_ir": None,
                "target_tool": "blender",
            },
        )
    )

    first_object = next(i for i, event in enumerate(events) if event["type"] == "object")
    validating = next(
        i
        for i, event in enumerate(events)
        if event["type"] == "status" and event.get("phase") == "validating"
    )
    assert any(event["type"] == "code" for event in events[:first_object])
    assert first_object < validating
    assert events[first_object]["item"]["id"] == events[first_object]["id"]
    assert events[-1]["type"] == "done"


def test_prompt_stream_returns_structured_error_event() -> None:
    client = TestClient(create_app(provider=FailingProvider()))

    events = stream_events(
        client.post(
            "/api/prompt/stream",
            json={
                "prompt": "make a house",
                "current_ir": None,
                "target_tool": "blender",
            },
        )
    )

    assert events[0]["type"] == "status"
    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == "provider unavailable"
    assert events[-1]["result"]["status"] == "error"


def test_prompt_returns_openui_elements_and_scene_headline() -> None:
    client = TestClient(create_app(provider=DeterministicProvider()))
    response = client.post(
        "/api/prompt",
        json={
            "prompt": "make a chair",
            "current_ir": None,
            "target_tool": "blender",
        },
    ).json()
    assert response["status"] == "ok"
    assert response["scene_headline"]
    assert "chair" in response["scene_headline"].lower()
    assert len(response["openui_elements"]) >= 2
    assert response["openui_elements"][0]["type"] == "Heading"


def test_execution_report_completes_trace() -> None:
    client = TestClient(create_app(provider=DeterministicProvider()))
    generated = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    ).json()

    response = client.post(
        "/api/execution",
        json={
            "request_id": generated["request_id"],
            "target_tool": "blender",
            "status": "ok",
            "duration_ms": 12.5,
            "error": None,
        },
    )

    payload = response.json()
    assert payload["event"]["step"] == "execute"
    assert payload["event"]["status"] == "completed"
    assert payload["trace"][-1]["step"] == "execute"


def test_guild_exporter_runs_after_prompt_and_execution() -> None:
    guild_exporter = RecordingGuildExporter()
    client = TestClient(
        create_app(provider=DeterministicProvider(), guild_exporter=guild_exporter)
    )

    generated = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    ).json()
    request_id = generated["request_id"]
    assert generated["guild_trace_url"] == f"https://app.guild.ai/sessions/{request_id}"

    execution = client.post(
        "/api/execution",
        json={
            "request_id": request_id,
            "target_tool": "blender",
            "status": "ok",
            "duration_ms": 42.0,
            "error": None,
        },
    ).json()

    assert execution["guild_trace_url"] == f"https://app.guild.ai/sessions/{request_id}"
    assert len(guild_exporter.calls) == 2
    assert guild_exporter.calls[1][1][-1].step == "execute"


def test_clickhouse_exporter_runs_after_prompt_and_execution() -> None:
    clickhouse_exporter = RecordingClickHouseExporter()
    client = TestClient(
        create_app(
            provider=DeterministicProvider(),
            clickhouse_exporter=clickhouse_exporter,
        )
    )

    generated = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    ).json()
    request_id = generated["request_id"]
    assert generated["clickhouse_exported"] is True

    execution = client.post(
        "/api/execution",
        json={
            "request_id": request_id,
            "target_tool": "blender",
            "status": "ok",
            "duration_ms": 18.0,
            "error": None,
        },
    ).json()

    assert execution["clickhouse_exported"] is True
    assert len(clickhouse_exporter.calls) == 2
    assert clickhouse_exporter.calls[0][1][0].step == "parse"
    assert clickhouse_exporter.calls[1][1][-1].step == "execute"


def test_prompt_includes_openui_lang() -> None:
    client = TestClient(create_app(provider=DeterministicProvider()))
    response = client.post(
        "/api/prompt",
        json={
            "prompt": "make a house",
            "current_ir": None,
            "target_tool": "blender",
        },
    ).json()

    assert response["status"] == "ok"
    assert "Stack(" in response["openui_lang"]
    assert "make a house" in response["openui_lang"]
