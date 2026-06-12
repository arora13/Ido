from __future__ import annotations

import os
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters" / "blender"))

import ido_blender  # noqa: E402
from ido_blender.client import BackendClient  # noqa: E402
from ido_blender.executor import execute_ir  # noqa: E402
from ido_blender.state import load_ir, save_ir  # noqa: E402


def main() -> None:
    backend_url = os.getenv("CAD_AGENT_BACKEND_URL", "http://127.0.0.1:8010")
    output_path = Path(
        os.getenv("CAD_AGENT_BLEND_OUTPUT", ROOT / "blender-smoke-test.blend")
    ).resolve()
    client = BackendClient(backend_url)
    ido_blender.register()

    first = client.prompt("make a house", None)
    assert first["status"] == "ok", first
    first_count = execute_ir(bpy.context, first["ir"])
    save_ir(bpy.context.scene, first["ir"], first["request_id"])
    body_pointer = bpy.data.objects["CAD_main_body"].as_pointer()

    second = client.prompt("add more windows", load_ir(bpy.context.scene))
    assert second["status"] == "ok", second
    second_count = execute_ir(bpy.context, second["ir"])
    save_ir(bpy.context.scene, second["ir"], second["request_id"])

    assert first_count == 5
    assert second_count == 9
    assert bpy.data.objects["CAD_main_body"].as_pointer() == body_pointer
    assert load_ir(bpy.context.scene)["history"] == [
        "make a house",
        "add more windows",
    ]

    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))
    bpy.ops.wm.open_mainfile(filepath=str(output_path))
    assert load_ir(bpy.context.scene)["history"][-1] == "add more windows"
    print(f"CAD_AGENT_SMOKE_OK first={first_count} second={second_count}")


if __name__ == "__main__":
    main()
