bl_info = {
    "name": "ido-blender",
    "author": "ido",
    "version": (0, 3, 0),
    "blender": (4, 5, 0),
    "location": "3D View > Sidebar > ido",
    "description": "Generate and edit 3D scenes from natural language",
    "category": "3D View",
}

def register() -> None:
    from .ui import register_ui

    register_ui()


def unregister() -> None:
    from .ui import unregister_ui

    unregister_ui()
