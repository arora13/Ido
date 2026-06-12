from __future__ import annotations

import json
from typing import Any

IR_PROPERTY = "cad_agent_current_ir"
REQUEST_PROPERTY = "cad_agent_last_request_id"
COLLECTION_NAME = "CAD_AGENT"


def load_ir(scene: Any) -> dict[str, Any] | None:
    serialized = scene.get(IR_PROPERTY)
    if not serialized:
        return None
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def save_ir(scene: Any, ir: dict[str, Any], request_id: str | None = None) -> None:
    scene[IR_PROPERTY] = json.dumps(ir, separators=(",", ":"), sort_keys=True)
    if request_id:
        scene[REQUEST_PROPERTY] = request_id


def clear_ir(scene: Any) -> None:
    scene.pop(IR_PROPERTY, None)
    scene.pop(REQUEST_PROPERTY, None)
