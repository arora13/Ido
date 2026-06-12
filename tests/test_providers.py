import asyncio
from dataclasses import dataclass

import pytest

from backend.providers.base import IRGenerationError
from backend.providers.fallback import DeterministicProvider
from backend.providers.openai_provider import OpenAIProvider
from shared.ir import EngineeringIR


def run(coroutine):
    return asyncio.run(coroutine)


def test_deterministic_provider_preserves_scene_during_iteration() -> None:
    provider = DeterministicProvider()

    first = run(provider.generate("make a house", None))
    second = run(provider.generate("add more windows", first))

    assert len(first.scene.objects) == 5
    assert len(second.scene.objects) == 9
    assert second.scene.objects[0].id == "house_body"
    assert second.history == ["make a house", "add more windows"]


def test_deterministic_provider_rejects_unsupported_prompt() -> None:
    with pytest.raises(IRGenerationError, match="Offline demo mode"):
        run(DeterministicProvider().generate("make a turbine", None))


@dataclass
class FakeResponse:
    output_parsed: object


class FakeResponses:
    def __init__(self, valid_ir: EngineeringIR) -> None:
        self.calls = 0
        self.valid_ir = valid_ir

    async def parse(self, **_kwargs) -> FakeResponse:
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(output_parsed={"invalid": True})
        return FakeResponse(output_parsed=self.valid_ir)


class FakeClient:
    def __init__(self, valid_ir: EngineeringIR) -> None:
        self.responses = FakeResponses(valid_ir)


def test_openai_provider_repairs_invalid_structured_output() -> None:
    baseline = run(DeterministicProvider().generate("make a house", None))
    client = FakeClient(baseline)
    provider = OpenAIProvider(client=client, model="test-model")

    generated = run(provider.generate("make a new house", None))

    assert client.responses.calls == 2
    assert generated.intent == "make a new house"
    assert generated.history == ["make a new house"]

