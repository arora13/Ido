from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from backend.providers.base import IRGenerationError, ProviderProgressCallback
from shared.ir import EngineeringIR
from shared.validation import IRValidationError, parse_and_validate_ir


SYSTEM_INSTRUCTIONS = """You are an engineering design agent.
Return a complete updated Engineering IR matching the supplied schema.

Requirements:
- Use only the primitive shapes and operations defined by the schema.
- All dimensions and positions are in meters.
- Preserve existing objects unless the user explicitly removes them.
- Reuse existing object IDs and labels when modifying objects.
- Use descriptive, unique IDs and labels for new objects.
- Treat a prism as a triangular roof prism aligned along its depth axis.
- For visible house details such as doors and windows, use thin colored box
  primitives placed just outside the wall unless the user requests cutouts.
- Return the entire scene, not a patch.

Composing spaces (rooms, houses, buildings, interiors):
- Decompose the space into explicit structural parts; never represent a room
  or building as a single box.
- Build rooms from a floor slab (~0.1 m thick), individual wall slabs
  (0.1-0.2 m thick) per side, and optionally a ceiling. Leave a gap or place a
  door-sized box where openings belong.
- Typical sizes: interior rooms 3-6 m wide, ceilings 2.4-3 m high, doors
  about 0.9 x 2.1 m, windows about 1.2 x 1.2 m with sills near 0.9 m height.
- Furnish recognizable spaces with their expected contents (a bedroom gets a
  bed and nightstand; a living room gets a sofa, rug and TV), each built from
  a few primitives.
- Position objects so they rest on the floor (z = height/2 for boxes) and do
  not intersect walls or each other; think through the floor plan first.
- Use 10-40 objects for spaces; more, smaller primitives read far better
  than a few oversized ones.
- Group related parts (one group per room or furniture piece) so the
  hierarchy stays readable.
- Vary materials sensibly: walls light and matte, floors wood- or
  carpet-toned, metal parts metallic with low roughness.
"""


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: AsyncOpenAI | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if client is None and not resolved_key:
            raise IRGenerationError("OPENAI_API_KEY is not configured")
        if client is None:
            client_kwargs: dict[str, str] = {"api_key": resolved_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = AsyncOpenAI(**client_kwargs)
        self._client = client
        self._model = model or os.getenv("OPENAI_MODEL", "gpt-5.5")

    async def generate(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
    ) -> EngineeringIR:
        prior_history = current_ir.history if current_ir else []
        payload = {
            "current_ir": current_ir.model_dump(mode="json") if current_ir else None,
            "history": prior_history,
            "user_prompt": prompt,
        }

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    instructions=SYSTEM_INSTRUCTIONS,
                    input=json.dumps(payload, separators=(",", ":")),
                    text_format=EngineeringIR,
                    max_output_tokens=32_000,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise IRGenerationError("OpenAI returned no parsed IR")
                validated = parse_and_validate_ir(parsed)
                return validated.model_copy(
                    update={
                        "intent": prompt,
                        "history": [*prior_history, prompt],
                    }
                )
            except (IRValidationError, IRGenerationError, ValueError) as exc:
                last_error = exc
                payload["repair_instruction"] = (
                    "The previous output failed validation. Return a corrected complete IR."
                )
                payload["validation_error"] = str(exc)
            except Exception as exc:
                raise IRGenerationError(f"OpenAI request failed: {exc}") from exc

        raise IRGenerationError(f"OpenAI returned invalid IR: {last_error}")

    async def generate_stream(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
        progress: ProviderProgressCallback,
    ) -> EngineeringIR:
        prior_history = current_ir.history if current_ir else []
        payload = {
            "current_ir": current_ir.model_dump(mode="json") if current_ir else None,
            "history": prior_history,
            "user_prompt": prompt,
        }

        last_error: Exception | None = None
        for attempt in range(2):
            if attempt:
                await progress({"type": "code_reset"})
                await progress(
                    {
                        "type": "status",
                        "message": "Repairing generated scene...",
                        "phase": "generating",
                    }
                )
            try:
                async with self._client.responses.stream(
                    model=self._model,
                    instructions=SYSTEM_INSTRUCTIONS,
                    input=json.dumps(payload, separators=(",", ":")),
                    text_format=EngineeringIR,
                    max_output_tokens=32_000,
                ) as stream:
                    async for event in stream:
                        if event.type == "response.output_text.delta" and event.delta:
                            await progress({"type": "code", "content": event.delta})
                    response = await stream.get_final_response()

                parsed = response.output_parsed
                if parsed is None:
                    raise IRGenerationError("OpenAI returned no parsed IR")
                validated = parse_and_validate_ir(parsed)
                return validated.model_copy(
                    update={
                        "intent": prompt,
                        "history": [*prior_history, prompt],
                    }
                )
            except (IRValidationError, IRGenerationError, ValueError) as exc:
                last_error = exc
                payload["repair_instruction"] = (
                    "The previous output failed validation. Return a corrected complete IR."
                )
                payload["validation_error"] = str(exc)
            except Exception as exc:
                raise IRGenerationError(f"OpenAI request failed: {exc}") from exc

        raise IRGenerationError(f"OpenAI returned invalid IR: {last_error}")
