from __future__ import annotations

import logging
import os

from backend.config import Settings
from backend.providers.base import (
    IRGenerationError,
    IRProvider,
    ProviderProgressCallback,
)
from backend.providers.fallback import DeterministicProvider
from backend.providers.openai_provider import OpenAIProvider
from backend.providers.pioneer_provider import PioneerProvider
from shared.ir import EngineeringIR

logger = logging.getLogger(__name__)


class ResilientProvider:
    name = "openai+deterministic"

    def __init__(self, primary: IRProvider, fallback: IRProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self.last_provider_used: str | None = None

    async def generate(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
    ) -> EngineeringIR:
        try:
            result = await self._primary.generate(prompt, current_ir)
            self.last_provider_used = self._primary.name
            return result
        except IRGenerationError as exc:
            logger.warning("primary_provider_failed", extra={"provider_error": str(exc)})
            result = await self._fallback.generate(prompt, current_ir)
            self.last_provider_used = self._fallback.name
            return result

    async def generate_stream(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
        progress: ProviderProgressCallback,
    ) -> EngineeringIR:
        stream_generate = getattr(self._primary, "generate_stream", None)
        if stream_generate is None:
            return await self.generate(prompt, current_ir)
        try:
            result = await stream_generate(prompt, current_ir, progress)
            self.last_provider_used = self._primary.name
            return result
        except IRGenerationError as exc:
            logger.warning("primary_provider_failed", extra={"provider_error": str(exc)})
            await progress({"type": "code_reset"})
            await progress(
                {
                    "type": "status",
                    "message": "Using offline fallback...",
                    "phase": "generating",
                }
            )
            result = await self._fallback.generate(prompt, current_ir)
            self.last_provider_used = self._fallback.name
            return result


class NamedResilientProvider(ResilientProvider):
    def __init__(self, name: str, primary: IRProvider, fallback: IRProvider) -> None:
        super().__init__(primary, fallback)
        self.name = name


def create_provider(settings: Settings) -> IRProvider:
    fallback = DeterministicProvider()
    if settings.demo_mode or settings.provider == "deterministic":
        return fallback
    if settings.provider == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            logger.warning("OPENAI_API_KEY is missing; using deterministic provider")
            return fallback
        return ResilientProvider(
            OpenAIProvider(model=settings.openai_model),
            fallback,
        )
    if settings.provider == "pioneer":
        if not os.getenv("PIONEER_API_KEY"):
            logger.warning("PIONEER_API_KEY is missing; using deterministic provider")
            return fallback
        return NamedResilientProvider(
            "pioneer+deterministic",
            PioneerProvider(model=settings.pioneer_model_id),
            fallback,
        )
    raise ValueError(f"Unsupported CAD_AGENT_PROVIDER: {settings.provider}")


def inference_provider_name(provider: IRProvider) -> str | None:
    last = getattr(provider, "last_provider_used", None)
    return last if isinstance(last, str) else None
