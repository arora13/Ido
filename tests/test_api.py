from fastapi.testclient import TestClient

from backend.main import create_app
from backend.providers.base import IRGenerationError
from backend.providers.fallback import DeterministicProvider


class FailingProvider:
    name = "failing"

    async def generate(self, _prompt, _current_ir):
        raise IRGenerationError("provider unavailable")


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

    assert response.json()["step"] == "execute"
    assert response.json()["status"] == "completed"
