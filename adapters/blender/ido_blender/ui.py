from __future__ import annotations

import json
import threading
from time import perf_counter

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .client import BackendClient
from .executor import clear_generated, execute_ir, iter_execute
from .state import clear_ir, load_ir, save_ir

CODE_TEXT_NAME = "ido_code.json"
PREVIEW_LINES = 14


class IdoProperties(PropertyGroup):
    prompt: StringProperty(name="Prompt", default="make a house")
    backend_url: StringProperty(name="Backend", default="http://127.0.0.1:8010")
    status: StringProperty(name="Status", default="Ready")
    is_generating: BoolProperty(name="Generating", default=False)
    show_settings: BoolProperty(name="Settings", default=False)
    code_preview: StringProperty(name="Code Preview", default="")


class IDO_OT_generate(Operator):
    bl_idname = "ido.generate"
    bl_label = "Generate"
    bl_description = "Generate or update the scene from the prompt"

    _timer = None
    _thread = None
    _result: dict | None = None
    _builder = None
    _response: dict | None = None
    _started = 0.0
    _count = 0
    _built_items: list | None = None

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        properties = context.scene.ido
        if properties.is_generating:
            return {"CANCELLED"}
        prompt = properties.prompt.strip()
        if not prompt:
            properties.status = "Enter a prompt"
            return {"CANCELLED"}

        client = BackendClient(properties.backend_url)
        current_ir = load_ir(context.scene)
        self._result = {}
        self._builder = None
        self._response = None
        self._count = 0
        self._built_items = []
        self._started = perf_counter()
        properties.code_preview = ""

        def request() -> None:
            try:
                self._result["response"] = client.prompt(prompt, current_ir)
            except Exception as exc:
                self._result["error"] = str(exc)

        self._thread = threading.Thread(target=request, daemon=True)
        self._thread.start()

        properties.is_generating = True
        properties.status = "Thinking..."
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type in {"RIGHTMOUSE", "ESC"} and self._builder is None:
            return self._fail(context, "Cancelled")
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        properties = context.scene.ido

        if self._builder is None:
            if self._thread is not None and self._thread.is_alive():
                elapsed = perf_counter() - self._started
                dots = "." * (int(elapsed * 2) % 4)
                properties.status = f"Thinking{dots} {elapsed:.0f}s"
                _redraw(context)
                return {"RUNNING_MODAL"}

            error = self._result.get("error")
            response = self._result.get("response")
            if error is None and response is not None:
                if response.get("status") != "ok" or not response.get("ir"):
                    error = (
                        response.get("error")
                        or "; ".join(response.get("validation_errors", []))
                        or "Backend did not return a model"
                    )
            if error is not None or response is None:
                return self._fail(context, error or "No response from backend")

            self._response = response
            self._builder = iter_execute(context, response["ir"])
            return {"RUNNING_MODAL"}

        try:
            index, total, label, item = next(self._builder)
        except StopIteration:
            return self._finish(context)
        except Exception as exc:
            return self._fail(context, str(exc))

        self._count = index
        self._built_items.append(item)
        properties.status = f"Building {index}/{total}: {label}"

        lines = properties.code_preview.split("\n") if properties.code_preview else []
        lines.append(_preview_line(item))
        properties.code_preview = "\n".join(lines[-PREVIEW_LINES:])

        partial_ir = dict(self._response["ir"])
        partial_ir["scene"] = {"objects": self._built_items}
        _write_code_text(partial_ir)
        _redraw(context)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        properties = context.scene.ido
        response = self._response or {}
        request_id = response.get("request_id")
        save_ir(context.scene, response["ir"], request_id)
        _write_code_text(response["ir"])
        duration_ms = (perf_counter() - self._started) * 1000
        self._report_execution(properties, request_id, "ok", duration_ms)
        properties.status = f"Done - {self._count} objects in {duration_ms / 1000:.0f}s"
        self._cleanup(context)
        return {"FINISHED"}

    def _fail(self, context, message: str):
        properties = context.scene.ido
        request_id = (self._result or {}).get("response", {}).get("request_id")
        duration_ms = (perf_counter() - self._started) * 1000
        self._report_execution(properties, request_id, "error", duration_ms, message)
        properties.status = f"Error: {message}"
        self.report({"ERROR"}, message)
        self._cleanup(context)
        return {"CANCELLED"}

    def _report_execution(
        self,
        properties,
        request_id: str | None,
        status: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        if not request_id:
            return
        try:
            BackendClient(properties.backend_url, timeout=10.0).report_execution(
                request_id=request_id,
                status=status,
                duration_ms=duration_ms,
                error=error,
            )
        except Exception:
            pass

    def _cleanup(self, context) -> None:
        context.scene.ido.is_generating = False
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._thread = None
        self._builder = None


class IDO_OT_view_code(Operator):
    bl_idname = "ido.view_code"
    bl_label = "View Code"
    bl_description = "Open the generated scene code in Blender's Text Editor"

    def execute(self, context):
        ir = load_ir(context.scene)
        if ir is None:
            self.report({"WARNING"}, "Nothing generated yet")
            return {"CANCELLED"}
        text = _write_code_text(ir)
        shown = False
        for area in context.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = text
                shown = True
                break
        context.window_manager.clipboard = text.as_string()
        if shown:
            self.report({"INFO"}, "Code opened in Text Editor (also copied)")
        else:
            self.report(
                {"INFO"},
                f"Code in text block '{CODE_TEXT_NAME}' - open a Text Editor to edit it",
            )
        return {"FINISHED"}


class IDO_OT_apply_code(Operator):
    bl_idname = "ido.apply_code"
    bl_label = "Apply Code"
    bl_description = "Rebuild the scene from the (edited) code in the Text Editor"

    def execute(self, context):
        properties = context.scene.ido
        text = bpy.data.texts.get(CODE_TEXT_NAME)
        if text is None:
            self.report({"WARNING"}, "No code to apply - generate or view code first")
            return {"CANCELLED"}
        try:
            ir = json.loads(text.as_string())
        except json.JSONDecodeError as exc:
            properties.status = f"Error: invalid JSON ({exc})"
            self.report({"ERROR"}, f"Invalid JSON: {exc}")
            return {"CANCELLED"}
        if not isinstance(ir, dict):
            self.report({"ERROR"}, "Code must be a JSON object")
            return {"CANCELLED"}
        try:
            count = execute_ir(context, ir)
        except Exception as exc:
            properties.status = f"Error: {exc}"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        save_ir(context.scene, ir)
        properties.status = f"Applied edited code - {count} objects"
        return {"FINISHED"}


class IDO_OT_reset(Operator):
    bl_idname = "ido.reset"
    bl_label = "Reset"
    bl_description = "Remove generated objects and clear the stored scene code"

    def execute(self, context):
        clear_generated(context.scene)
        clear_ir(context.scene)
        context.scene.ido.status = "Scene reset"
        context.scene.ido.code_preview = ""
        return {"FINISHED"}


class IDO_PT_sidebar(Panel):
    bl_label = "idō"
    bl_idname = "IDO_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ido"

    def draw(self, context):
        layout = self.layout
        properties = context.scene.ido

        prompt_box = layout.box()
        prompt_box.label(text="What do you want to build?", icon="OUTLINER_OB_LIGHT")
        prompt_box.prop(properties, "prompt", text="")
        button_row = prompt_box.row()
        button_row.scale_y = 1.5
        button_row.enabled = not properties.is_generating
        button_row.operator("ido.generate", icon="PLAY")

        status_box = layout.box()
        status = properties.status
        if status.startswith("Error"):
            icon = "ERROR"
        elif properties.is_generating:
            icon = "SORTTIME"
        elif status.startswith(("Done", "Applied", "Updated")):
            icon = "CHECKMARK"
        else:
            icon = "INFO"
        status_box.label(text=status, icon=icon)

        if properties.code_preview:
            live_box = layout.box()
            live_box.label(text="Live code", icon="SCRIPT")
            lines_col = live_box.column(align=True)
            lines_col.scale_y = 0.8
            for line in properties.code_preview.split("\n"):
                lines_col.label(text=line)

        code_box = layout.box()
        code_box.label(text="Code", icon="TEXT")
        code_row = code_box.row(align=True)
        code_row.operator("ido.view_code", text="View", icon="HIDE_OFF")
        code_row.operator("ido.apply_code", text="Apply", icon="FILE_REFRESH")

        layout.separator()
        layout.operator("ido.reset", icon="TRASH")

        layout.prop(
            properties,
            "show_settings",
            text="Connection Settings",
            icon="DISCLOSURE_TRI_DOWN"
            if properties.show_settings
            else "DISCLOSURE_TRI_RIGHT",
            emboss=False,
        )
        if properties.show_settings:
            layout.prop(properties, "backend_url")


def _write_code_text(ir: dict) -> bpy.types.Text:
    text = bpy.data.texts.get(CODE_TEXT_NAME) or bpy.data.texts.new(CODE_TEXT_NAME)
    text.clear()
    text.write(json.dumps(ir, indent=2))
    return text


def _preview_line(item: dict) -> str:
    kind = item.get("type")
    object_id = item.get("id", "?")
    if kind == "primitive":
        dims = item.get("dimensions") or {}
        if item.get("shape") in {"box", "prism"}:
            size = "x".join(
                f"{dims.get(key, 0):g}" for key in ("width", "depth", "height")
            )
        elif item.get("shape") == "sphere":
            size = f"r{dims.get('radius', 0):g}"
        else:
            size = f"r{dims.get('radius', 0):g} h{dims.get('height', 0):g}"
        return f"+ {item.get('shape', '?')} {object_id} [{size}]"
    children = len(item.get("children", []))
    if kind == "operation":
        return f"= {item.get('operation', '?')} {object_id} ({children})"
    return f"# group {object_id} ({children})"


def _redraw(context) -> None:
    for area in context.screen.areas:
        if area.type in {"VIEW_3D", "TEXT_EDITOR"}:
            area.tag_redraw()


CLASSES = (
    IdoProperties,
    IDO_OT_generate,
    IDO_OT_view_code,
    IDO_OT_apply_code,
    IDO_OT_reset,
    IDO_PT_sidebar,
)


def register_ui() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ido = PointerProperty(type=IdoProperties)


def unregister_ui() -> None:
    del bpy.types.Scene.ido
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
