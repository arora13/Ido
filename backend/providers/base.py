from __future__ import annotations

from typing import Protocol

from shared.ir import EngineeringIR


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
