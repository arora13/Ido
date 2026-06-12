import json
from pathlib import Path

import pytest

from shared.ir import EngineeringIR
from shared.validation import IRValidationError, parse_and_validate_ir

FIXTURE = Path(__file__).parent / "fixtures" / "house_ir.json"


def load_house() -> dict:
    return json.loads(FIXTURE.read_text())


def test_house_fixture_matches_schema() -> None:
    ir = parse_and_validate_ir(load_house())

    assert ir.intent == "make a house"
    assert len(ir.scene.objects) == 5


def test_duplicate_labels_are_rejected() -> None:
    data = load_house()
    data["scene"]["objects"][1]["label"] = "main_body"

    with pytest.raises(IRValidationError, match="labels must be unique"):
        parse_and_validate_ir(data)


def test_unknown_operation_reference_is_rejected() -> None:
    data = load_house()
    data["scene"]["objects"].append(
        {
            "id": "bad_union",
            "type": "operation",
            "operation": "union",
            "label": "bad_union",
            "children": ["house_body", "missing"],
        }
    )

    with pytest.raises(IRValidationError, match="unknown children"):
        parse_and_validate_ir(data)


def test_cyclic_operation_graph_is_rejected() -> None:
    data = load_house()
    data["scene"]["objects"].extend(
        [
            {
                "id": "group_a",
                "type": "group",
                "label": "group_a",
                "children": ["group_b"],
            },
            {
                "id": "group_b",
                "type": "group",
                "label": "group_b",
                "children": ["group_a"],
            },
        ]
    )

    with pytest.raises(IRValidationError, match="contains a cycle"):
        parse_and_validate_ir(data)


def test_generated_json_schema_can_validate_fixture() -> None:
    ir = EngineeringIR.model_validate(load_house())

    assert ir.version == "1.0"

