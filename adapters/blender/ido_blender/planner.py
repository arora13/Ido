from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PlanningError(ValueError):
    pass


@dataclass(frozen=True)
class ScenePlan:
    primitives: tuple[dict[str, Any], ...]
    composites: tuple[dict[str, Any], ...]
    desired_ids: frozenset[str]


def plan_scene(ir: dict[str, Any]) -> ScenePlan:
    objects = ir.get("scene", {}).get("objects", [])
    if not isinstance(objects, list):
        raise PlanningError("scene.objects must be a list")

    by_id: dict[str, dict[str, Any]] = {}
    for item in objects:
        object_id = item.get("id")
        if not object_id or object_id in by_id:
            raise PlanningError("scene object IDs must be present and unique")
        by_id[object_id] = item

    primitives = tuple(
        item for item in objects if item.get("type") == "primitive"
    )
    composite_ids = {
        item["id"]
        for item in objects
        if item.get("type") in {"operation", "group"}
    }
    sorted_composites: list[dict[str, Any]] = []
    remaining = set(composite_ids)
    resolved = {item["id"] for item in primitives}

    while remaining:
        progress = False
        for object_id in tuple(remaining):
            item = by_id[object_id]
            children = item.get("children", [])
            if not isinstance(children, list) or not children:
                raise PlanningError(f"{object_id} must have child references")
            missing = set(children) - set(by_id)
            if missing:
                raise PlanningError(
                    f"{object_id} references missing objects: {sorted(missing)}"
                )
            if set(children) <= resolved:
                sorted_composites.append(item)
                resolved.add(object_id)
                remaining.remove(object_id)
                progress = True
        if not progress:
            raise PlanningError("operation graph is cyclic or unresolved")

    unsupported = [
        item.get("type")
        for item in objects
        if item.get("type") not in {"primitive", "operation", "group"}
    ]
    if unsupported:
        raise PlanningError(f"unsupported object types: {unsupported}")

    return ScenePlan(
        primitives=primitives,
        composites=tuple(sorted_composites),
        desired_ids=frozenset(by_id),
    )

