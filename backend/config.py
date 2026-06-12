from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    provider: str
    demo_mode: bool
    openai_model: str
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            provider=os.getenv("CAD_AGENT_PROVIDER", "openai").strip().lower(),
            demo_mode=_env_bool("CAD_AGENT_DEMO_MODE"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            log_level=os.getenv("CAD_AGENT_LOG_LEVEL", "INFO").upper(),
        )

