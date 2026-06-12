from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from shared.ir import EngineeringIR

ProviderProgressCallback = Callable[[dict[str, object]], Awaitable[None]]


class IRGenerationError(RuntimeError):
    """Raised when a provider cannot produce a valid Engineering IR."""


class IRProvider(Protocol):
    name: str

    async def generate(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
    ) -> EngineeringIR:
        """Generate a complete updated IR for the requested scene change."""


class StreamingIRProvider(IRProvider, Protocol):
    async def generate_stream(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
        progress: ProviderProgressCallback,
    ) -> EngineeringIR:
        """Generate IR while reporting provider output deltas."""
