from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Vector3(StrictModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class Rotation(StrictModel):
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class Dimensions(StrictModel):
    width: float | None = Field(default=None, gt=0)
    depth: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    radius: float | None = Field(default=None, gt=0)


class Material(StrictModel):
    color: str = Field(default="#B8B8B8", pattern=r"^#[0-9A-Fa-f]{6}$")
    metallic: float = Field(default=0.0, ge=0.0, le=1.0)
    roughness: float = Field(default=0.5, ge=0.0, le=1.0)


class BaseSceneObject(StrictModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    position: Vector3 = Field(default_factory=Vector3)
    rotation: Rotation = Field(default_factory=Rotation)
    material: Material = Field(default_factory=Material)
    visible: bool = True


class PrimitiveObject(BaseSceneObject):
    type: Literal["primitive"] = "primitive"
    shape: Literal["box", "cylinder", "sphere", "cone", "prism"]
    dimensions: Dimensions

    @model_validator(mode="after")
    def validate_shape_dimensions(self) -> PrimitiveObject:
        rectangular = {"box", "prism"}
        if self.shape in rectangular:
            required = ("width", "depth", "height")
        elif self.shape == "sphere":
            required = ("radius",)
        else:
            required = ("radius", "height")

        missing = [name for name in required if getattr(self.dimensions, name) is None]
        if missing:
            raise ValueError(
                f"{self.shape} requires dimensions: {', '.join(required)}"
            )
        return self


class OperationObject(BaseSceneObject):
    type: Literal["operation"] = "operation"
    operation: Literal["union", "difference", "intersection"]
    children: list[str] = Field(min_length=2)


class GroupObject(BaseSceneObject):
    type: Literal["group"] = "group"
    children: list[str] = Field(min_length=1)


# Plain union (not a discriminated union): OpenAI structured outputs rejects
# the `oneOf` schema that a pydantic discriminator generates; `anyOf` is fine
# and the `type` Literal on each member still disambiguates during validation.
SceneObject = Union[PrimitiveObject, OperationObject, GroupObject]


class Scene(StrictModel):
    objects: list[SceneObject] = Field(default_factory=list)


class EngineeringIR(StrictModel):
    version: Literal["1.0"] = "1.0"
    intent: str = Field(min_length=1)
    history: list[str] = Field(default_factory=list)
    units: Literal["meters"] = "meters"
    scene: Scene = Field(default_factory=Scene)

    @model_validator(mode="after")
    def validate_scene_identity_and_references(self) -> EngineeringIR:
        ids = [item.id for item in self.scene.objects]
        labels = [item.label for item in self.scene.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("scene object ids must be unique")
        if len(labels) != len(set(labels)):
            raise ValueError("scene object labels must be unique")

        known_ids = set(ids)
        for item in self.scene.objects:
            if isinstance(item, (OperationObject, GroupObject)):
                unknown = set(item.children) - known_ids
                if unknown:
                    raise ValueError(
                        f"{item.id} references unknown children: {sorted(unknown)}"
                    )
                if item.id in item.children:
                    raise ValueError(f"{item.id} cannot reference itself")
        return self

