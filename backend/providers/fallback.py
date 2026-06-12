from __future__ import annotations

from copy import deepcopy

from backend.providers.base import IRGenerationError
from shared.ir import (
    Dimensions,
    EngineeringIR,
    Material,
    PrimitiveObject,
    Scene,
    Vector3,
)


class DeterministicProvider:
    name = "deterministic"

    async def generate(
        self,
        prompt: str,
        current_ir: EngineeringIR | None,
    ) -> EngineeringIR:
        normalized = " ".join(prompt.lower().split())
        if "house" in normalized and (
            current_ir is None or "new house" in normalized or "make" in normalized
        ):
            return _house_ir(prompt)
        if current_ir is not None and "window" in normalized and (
            "more" in normalized or "add" in normalized
        ):
            return _add_windows(current_ir, prompt)
        raise IRGenerationError(
            "Offline demo mode supports 'make a house' and 'add more windows'."
        )


def _house_ir(prompt: str) -> EngineeringIR:
    objects = [
        _box(
            "house_body",
            "main_body",
            (8.0, 6.0, 4.0),
            (0.0, 0.0, 2.0),
            "#D9B38C",
        ),
        PrimitiveObject(
            id="house_roof",
            label="roof_prism",
            shape="prism",
            dimensions=Dimensions(width=8.8, depth=6.8, height=2.4),
            position=Vector3(x=0.0, y=0.0, z=5.2),
            material=Material(color="#8B3A3A", roughness=0.8),
        ),
        _box(
            "front_door",
            "front_door",
            (1.4, 0.18, 2.5),
            (0.0, -3.09, 1.25),
            "#5B3924",
        ),
        _box(
            "window_front_left",
            "window_front_left",
            (1.4, 0.16, 1.3),
            (-2.3, -3.1, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
        _box(
            "window_front_right",
            "window_front_right",
            (1.4, 0.16, 1.3),
            (2.3, -3.1, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
    ]
    return EngineeringIR(intent=prompt, history=[prompt], scene=Scene(objects=objects))


def _add_windows(current_ir: EngineeringIR, prompt: str) -> EngineeringIR:
    updated = deepcopy(current_ir)
    existing_ids = {item.id for item in updated.scene.objects}
    candidates = [
        _box(
            "window_left_front",
            "window_left_front",
            (0.16, 1.4, 1.3),
            (-4.1, -1.6, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
        _box(
            "window_left_back",
            "window_left_back",
            (0.16, 1.4, 1.3),
            (-4.1, 1.6, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
        _box(
            "window_right_front",
            "window_right_front",
            (0.16, 1.4, 1.3),
            (4.1, -1.6, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
        _box(
            "window_right_back",
            "window_right_back",
            (0.16, 1.4, 1.3),
            (4.1, 1.6, 2.3),
            "#72B7D2",
            roughness=0.2,
        ),
    ]
    updated.scene.objects.extend(
        candidate for candidate in candidates if candidate.id not in existing_ids
    )
    updated.intent = prompt
    updated.history.append(prompt)
    return updated


def _box(
    object_id: str,
    label: str,
    dimensions: tuple[float, float, float],
    position: tuple[float, float, float],
    color: str,
    *,
    roughness: float = 0.6,
) -> PrimitiveObject:
    width, depth, height = dimensions
    x, y, z = position
    return PrimitiveObject(
        id=object_id,
        label=label,
        shape="box",
        dimensions=Dimensions(width=width, depth=depth, height=height),
        position=Vector3(x=x, y=y, z=z),
        material=Material(color=color, roughness=roughness),
    )

