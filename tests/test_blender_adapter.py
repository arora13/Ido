import json
from pathlib import Path

import pytest

from adapters.blender.ido_blender.planner import PlanningError, plan_scene
from adapters.blender.ido_blender.state import clear_ir, load_ir, save_ir

FIXTURE = Path(__file__).parent / "fixtures" / "house_ir.json"


def load_house() -> dict:
    return json.loads(FIXTURE.read_text())


def test_planner_orders_nested_composites() -> None:
    ir = load_house()
    ir["scene"]["objects"].extend(
        [
            {
                "id": "front_group",
                "type": "group",
                "label": "front_group",
                "children": ["front_door", "window_front_left"],
            },
            {
                "id": "whole_house",
                "type": "group",
                "label": "whole_house",
                "children": ["house_body", "front_group"],
            },
        ]
    )

    plan = plan_scene(ir)

    assert [item["id"] for item in plan.composites] == [
        "front_group",
        "whole_house",
    ]


def test_planner_rejects_unresolved_graph() -> None:
    ir = load_house()
    ir["scene"]["objects"].append(
        {
            "id": "bad_group",
            "type": "group",
            "label": "bad_group",
            "children": ["missing"],
        }
    )

    with pytest.raises(PlanningError, match="missing objects"):
        plan_scene(ir)


def test_scene_state_round_trip_and_clear() -> None:
    scene = {}
    ir = load_house()

    save_ir(scene, ir, "request-id")

    assert load_ir(scene) == ir
    assert scene["cad_agent_last_request_id"] == "request-id"
    clear_ir(scene)
    assert load_ir(scene) is None


def test_invalid_persisted_state_is_ignored() -> None:
    assert load_ir({"cad_agent_current_ir": "not-json"}) is None

