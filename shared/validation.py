from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from shared.ir import EngineeringIR, GroupObject, OperationObject


class IRValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def parse_and_validate_ir(data: Mapping[str, Any] | EngineeringIR) -> EngineeringIR:
    try:
        ir = data if isinstance(data, EngineeringIR) else EngineeringIR.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise IRValidationError(errors) from exc

    graph = {
        item.id: item.children
        for item in ir.scene.objects
        if isinstance(item, (OperationObject, GroupObject))
    }
    cycle = _find_cycle(graph)
    if cycle:
        raise IRValidationError(
            [f"scene operation graph contains a cycle: {' -> '.join(cycle)}"]
        )
    return ir


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            start = path.index(node)
            return [*path[start:], node]
        if node in visited:
            return None

        visiting.add(node)
        path.append(node)
        for child in graph.get(node, []):
            cycle = visit(child)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in graph:
        cycle = visit(node)
        if cycle:
            return cycle
    return None

