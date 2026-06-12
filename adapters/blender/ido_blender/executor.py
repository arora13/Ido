from __future__ import annotations

import math
from typing import Any

import bpy

from .planner import plan_scene
from .state import COLLECTION_NAME

ID_PROPERTY = "cad_agent_id"
SHAPE_PROPERTY = "cad_agent_shape"


def execute_ir(context: Any, ir: dict[str, Any]) -> int:
    count = 0
    for count, _total, _label, _item in iter_execute(context, ir):
        pass
    return count


def iter_execute(context: Any, ir: dict[str, Any]):
    """Apply the IR one object at a time, yielding (index, total, label, item).

    Lets callers (the modal generate operator) build the scene progressively
    with viewport redraws between objects.
    """
    plan = plan_scene(ir)
    collection = _get_collection(context.scene)
    existing = {
        obj.get(ID_PROPERTY): obj
        for obj in collection.objects
        if obj.get(ID_PROPERTY)
    }

    for object_id, obj in tuple(existing.items()):
        if object_id not in plan.desired_ids:
            _remove_object(obj)
            existing.pop(object_id, None)

    total = len(plan.primitives) + len(plan.composites)
    index = 0
    resolved: dict[str, Any] = {}
    for item in plan.primitives:
        obj = _upsert_primitive(collection, existing.get(item["id"]), item)
        resolved[item["id"]] = obj
        index += 1
        yield index, total, item["label"], item

    for item in plan.composites:
        if item["type"] == "operation":
            obj = _upsert_operation(
                collection,
                existing.get(item["id"]),
                item,
                resolved,
            )
        else:
            obj = _upsert_group(
                collection,
                existing.get(item["id"]),
                item,
                resolved,
            )
        resolved[item["id"]] = obj
        index += 1
        yield index, total, item["label"], item


def clear_generated(scene: Any) -> None:
    collection = bpy.data.collections.get(COLLECTION_NAME)
    if collection is None:
        return
    for obj in tuple(collection.objects):
        _remove_object(obj)
    bpy.data.collections.remove(collection)


def _get_collection(scene: Any) -> Any:
    collection = bpy.data.collections.get(COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION_NAME)
    if collection.name not in {child.name for child in scene.collection.children}:
        scene.collection.children.link(collection)
    return collection


def _upsert_primitive(collection: Any, existing: Any, item: dict[str, Any]) -> Any:
    shape = item["shape"]
    if existing is None or existing.type != "MESH" or existing.get(SHAPE_PROPERTY) != shape:
        if existing is not None:
            _remove_object(existing)
        obj = _create_primitive(collection, shape, item["id"])
    else:
        obj = existing

    dimensions = item["dimensions"]
    if shape in {"box", "prism"}:
        size = (
            dimensions["width"],
            dimensions["depth"],
            dimensions["height"],
        )
    elif shape == "sphere":
        diameter = dimensions["radius"] * 2
        size = (diameter, diameter, diameter)
    else:
        diameter = dimensions["radius"] * 2
        size = (diameter, diameter, dimensions["height"])

    obj.name = f"CAD_{item['label']}"
    obj.dimensions = size
    obj.location = _vector(item.get("position"))
    rotation = item.get("rotation", {})
    obj.rotation_euler = tuple(
        math.radians(float(rotation.get(axis, 0.0))) for axis in ("x", "y", "z")
    )
    obj.hide_viewport = not item.get("visible", True)
    obj.hide_render = not item.get("visible", True)
    obj[ID_PROPERTY] = item["id"]
    obj[SHAPE_PROPERTY] = shape
    obj["cad_agent_label"] = item["label"]
    _assign_material(obj, item.get("material", {}))
    return obj


def _create_primitive(collection: Any, shape: str, object_id: str) -> Any:
    if shape == "box":
        bpy.ops.mesh.primitive_cube_add(size=1.0)
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=0.5, depth=1.0)
    elif shape == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.5)
    elif shape == "cone":
        bpy.ops.mesh.primitive_cone_add(vertices=48, radius1=0.5, depth=1.0)
    elif shape == "prism":
        obj = _create_unit_prism(object_id)
        _move_to_collection(obj, collection)
        return obj
    else:
        raise ValueError(f"Unsupported primitive shape: {shape}")

    obj = bpy.context.active_object
    obj.name = f"CAD_{object_id}"
    _move_to_collection(obj, collection)
    return obj


def _create_unit_prism(object_id: str) -> Any:
    vertices = [
        (-0.5, -0.5, -0.5),
        (0.5, -0.5, -0.5),
        (0.0, -0.5, 0.5),
        (-0.5, 0.5, -0.5),
        (0.5, 0.5, -0.5),
        (0.0, 0.5, 0.5),
    ]
    faces = [
        (0, 2, 1),
        (3, 4, 5),
        (0, 1, 4, 3),
        (1, 2, 5, 4),
        (2, 0, 3, 5),
    ]
    mesh = bpy.data.meshes.new(f"CAD_{object_id}_mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return bpy.data.objects.new(f"CAD_{object_id}", mesh)


def _upsert_operation(
    collection: Any,
    existing: Any,
    item: dict[str, Any],
    resolved: dict[str, Any],
) -> Any:
    children = [resolved[child_id] for child_id in item["children"]]
    base = children[0]
    if existing is not None:
        _remove_object(existing)
    result = base.copy()
    result.data = base.data.copy()
    result.name = f"CAD_{item['label']}"
    collection.objects.link(result)
    result[ID_PROPERTY] = item["id"]
    result["cad_agent_label"] = item["label"]
    operation = item["operation"].upper()
    for index, operand in enumerate(children[1:], start=1):
        modifier = result.modifiers.new(
            name=f"CAD_{operation}_{index}",
            type="BOOLEAN",
        )
        modifier.operation = operation
        modifier.solver = "EXACT"
        modifier.object = operand
    for child in children:
        child.hide_viewport = True
        child.hide_render = True
    return result


def _upsert_group(
    collection: Any,
    existing: Any,
    item: dict[str, Any],
    resolved: dict[str, Any],
) -> Any:
    if existing is None or existing.type != "EMPTY":
        if existing is not None:
            _remove_object(existing)
        existing = bpy.data.objects.new(f"CAD_{item['label']}", None)
        collection.objects.link(existing)
    existing.name = f"CAD_{item['label']}"
    existing.empty_display_type = "PLAIN_AXES"
    existing.location = _vector(item.get("position"))
    existing[ID_PROPERTY] = item["id"]
    existing["cad_agent_label"] = item["label"]
    for child_id in item["children"]:
        resolved[child_id].parent = existing
    return existing


def _assign_material(obj: Any, material_data: dict[str, Any]) -> None:
    color = material_data.get("color", "#B8B8B8")
    name = f"CAD_AGENT_{color.lstrip('#').upper()}"
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    red, green, blue = (
        int(color[index : index + 2], 16) / 255.0 for index in (1, 3, 5)
    )
    material.diffuse_color = (red, green, blue, 1.0)
    material.metallic = float(material_data.get("metallic", 0.0))
    material.roughness = float(material_data.get("roughness", 0.5))
    obj.data.materials.clear()
    obj.data.materials.append(material)


def _move_to_collection(obj: Any, collection: Any) -> None:
    for linked_collection in tuple(obj.users_collection):
        linked_collection.objects.unlink(obj)
    collection.objects.link(obj)


def _remove_object(obj: Any) -> None:
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and getattr(data, "users", 1) == 0:
        if isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)


def _vector(value: dict[str, Any] | None) -> tuple[float, float, float]:
    value = value or {}
    return tuple(float(value.get(axis, 0.0)) for axis in ("x", "y", "z"))

