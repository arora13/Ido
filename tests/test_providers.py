import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

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


def test_bedroom_includes_bed_desk_and_chair() -> None:
    ir = run(DeterministicProvider().generate("make a cozy bedroom", None))
    labels = {item.label for item in ir.scene.objects}
    assert {"bed_mattress", "desk_top", "chair_seat"}.issubset(labels)


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


class FakeStream:
    def __init__(self, valid_ir: EngineeringIR) -> None:
        encoded = valid_ir.model_dump_json()
        self.events = [
            SimpleNamespace(type="response.output_text.delta", delta=encoded[:30]),
            SimpleNamespace(type="response.output_text.delta", delta=encoded[30:]),
        ]
        self.response = FakeResponse(output_parsed=valid_ir)

    def __aiter__(self):
        self._events = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def get_final_response(self):
        return self.response


class FakeStreamManager:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, *_args):
        return None


class FakeStreamingResponses:
    def __init__(self, valid_ir: EngineeringIR) -> None:
        self.valid_ir = valid_ir

    def stream(self, **_kwargs):
        return FakeStreamManager(FakeStream(self.valid_ir))


class FakeStreamingClient:
    def __init__(self, valid_ir: EngineeringIR) -> None:
        self.responses = FakeStreamingResponses(valid_ir)


def test_openai_provider_streams_structured_output_deltas() -> None:
    baseline = run(DeterministicProvider().generate("make a chair", None))
    provider = OpenAIProvider(client=FakeStreamingClient(baseline), model="test-model")
    events = []

    generated = run(
        provider.generate_stream(
            "make a new chair",
            None,
            lambda event: _record_event(events, event),
        )
    )

    assert [event["type"] for event in events] == ["code", "code"]
    assert "".join(event["content"] for event in events) == baseline.model_dump_json()
    assert generated.intent == "make a new chair"


async def _record_event(events, event):
    events.append(event)
