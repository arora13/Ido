from __future__ import annotations

import logging
import os

from backend.config import Settings
from backend.providers.base import IRGenerationError, IRProvider
from backend.providers.fallback import DeterministicProvider
from backend.providers.openai_provider import OpenAIProvider
from shared.ir import EngineeringIR

logger = logging.getLogger(__name__)


class ResilientProvider:
    name = "openai+deterministic"

    def __init__(self, primary: IRProvider, fallback: IRProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
    ) -> EngineeringIR:
        try:
            return await self._primary.generate(prompt, current_ir)
        except IRGenerationError as exc:
            logger.warning("primary_provider_failed", extra={"provider_error": str(exc)})
            return await self._fallback.generate(prompt, current_ir)


def create_provider(settings: Settings) -> IRProvider:
    fallback = DeterministicProvider()
    if settings.demo_mode or settings.provider == "deterministic":
        return fallback
    if settings.provider != "openai":
        raise ValueError(f"Unsupported CAD_AGENT_PROVIDER: {settings.provider}")
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY is missing; using deterministic provider")
        return fallback
    return ResilientProvider(
        OpenAIProvider(model=settings.openai_model),
        fallback,
    )
