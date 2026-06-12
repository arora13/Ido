from __future__ import annotations

import json
import threading
from queue import Empty, Queue
from time import perf_counter

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .harness import _persist_sponsor_metadata
from .client import BackendClient
from .executor import clear_generated, execute_ir, iter_execute
from .state import (
    clear_ir,
    load_airbyte_context_exported,
    load_clickhouse_exported,
    load_composio_status,
    load_guild_trace_url,
    load_ir,
    load_openui_lang,
    load_trace,
    save_ir,
    save_trace,
)
from .trace_timeline import format_trace_timeline, summarize_trace

CODE_TEXT_NAME = "ido_code.json"
PREVIEW_LINES = 14


class IdoProperties(PropertyGroup):
    prompt: StringProperty(name="Prompt", default="make a house")
    backend_url: StringProperty(name="Backend", default="http://127.0.0.1:8010")
    status: StringProperty(name="Status", default="Ready")
    is_generating: BoolProperty(name="Generating", default=False)
    show_settings: BoolProperty(name="Settings", default=False)
    code_preview: StringProperty(name="Code Preview", default="")
    auto_open_guild: BoolProperty(
        name="Auto-open Guild after generate",
        default=True,
        description="Open the Guild trace view in your browser after a successful generate",
    )


def _open_guild_url(context, url: str) -> None:
    bpy.ops.wm.url_open(url=url)
    context.scene.ido.status = "Opened Guild trace view"


def _save_response_metadata(scene, response: dict[str, object], trace: list[dict[str, object]]) -> None:
    save_trace(
        scene,
        trace,
        guild_trace_url=response.get("guild_trace_url"),  # type: ignore[arg-type]
        openui_lang=response.get("openui_lang"),  # type: ignore[arg-type]
        composio_status=response.get("composio_status"),  # type: ignore[arg-type]
        clickhouse_exported=response.get("clickhouse_exported"),  # type: ignore[arg-type]
        airbyte_context_exported=response.get("airbyte_context_exported"),  # type: ignore[arg-type]
    )
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
    _events: Queue | None = None

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
        self._events = Queue()
        self._started = perf_counter()
        properties.code_preview = ""

        def request() -> None:
            try:
                self._result["response"] = client.prompt_stream(
                    prompt,
                    current_ir,
                    self._events.put,
                )
            except Exception as exc:
                self._result["error"] = str(exc)

        self._thread = threading.Thread(target=request, daemon=True)
        self._thread.start()

        properties.is_generating = True
        properties.status = "Parsing prompt..."
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
            self._consume_stream_events(properties)
            response = self._response or self._result.get("response")
            if response is not None:
                if response.get("status") != "ok" or not response.get("ir"):
                    error = (
                        response.get("error")
                        or "; ".join(response.get("validation_errors", []))
                        or "Backend did not return a model"
                    )
                    return self._fail(context, error)
                self._response = response
                self._builder = iter_execute(context, response["ir"])
                _redraw(context)
                return {"RUNNING_MODAL"}

            if self._thread is not None and self._thread.is_alive():
                _redraw(context)
                return {"RUNNING_MODAL"}

            error = self._result.get("error")
            if error is not None or response is None:
                return self._fail(context, error or "No response from backend")

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

    def _consume_stream_events(self, properties) -> None:
        if self._events is None:
            return
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                return

            event_type = event.get("type")
            if event_type == "status":
                message = str(event.get("message") or "Generating scene...")
                elapsed = event.get("elapsed_seconds")
                properties.status = (
                    f"{message} {elapsed}s"
                    if isinstance(elapsed, int) and elapsed > 0
                    else message
                )
            elif event_type == "object":
                lines = properties.code_preview.split("\n") if properties.code_preview else []
                lines.append(_stream_object_line(event))
                properties.code_preview = "\n".join(lines[-PREVIEW_LINES:])
            elif event_type == "code":
                if not properties.code_preview:
                    properties.status = "Streaming generated scene..."
            elif event_type == "code_reset":
                properties.code_preview = ""
            elif event_type in {"done", "error"} and isinstance(event.get("result"), dict):
                self._response = event["result"]
            elif event_type == "error":
                self._result["error"] = str(event.get("message") or "Generation failed")

    def _finish(self, context):
        properties = context.scene.ido
        response = self._response or {}
        request_id = response.get("request_id")
        save_ir(context.scene, response["ir"], request_id)
        _write_code_text(response["ir"])
        duration_ms = (perf_counter() - self._started) * 1000
        execution = self._report_execution(properties, request_id, "ok", duration_ms)
        trace = list(response.get("trace", []))
        guild_trace_url = response.get("guild_trace_url")
        if execution:
            trace = execution.get("trace", trace)
            guild_trace_url = execution.get("guild_trace_url") or guild_trace_url
            response = execution
        _persist_sponsor_metadata(context.scene, self._response or {}, execution)
        properties.status = f"Done - {self._count} objects in {duration_ms / 1000:.0f}s"
        if guild_trace_url and properties.auto_open_guild:
            _open_guild_url(context, guild_trace_url)
        self._cleanup(context)
        return {"FINISHED"}

    def _fail(self, context, message: str):
        properties = context.scene.ido
        response = self._response or (self._result or {}).get("response", {})
        request_id = response.get("request_id")
        duration_ms = (perf_counter() - self._started) * 1000
        execution = self._report_execution(properties, request_id, "error", duration_ms, message)
        if execution:
            _persist_sponsor_metadata(
                context.scene,
                (self._result or {}).get("response", {}),
                execution,
            )
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
    ) -> dict | None:
        if not request_id:
            return None
        try:
            return BackendClient(properties.backend_url, timeout=10.0).report_execution(
                request_id=request_id,
                status=status,
                duration_ms=duration_ms,
                error=error,
            )
        except Exception:
            return None

    def _cleanup(self, context) -> None:
        context.scene.ido.is_generating = False
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._thread = None
        self._builder = None
        self._events = None


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


class IDO_OT_show_trace(Operator):
    bl_idname = "ido.show_trace"
    bl_label = "Show Request Timeline"
    bl_description = "Show parse, validate, route, and execute timings for the last request"

    def execute(self, context):
        trace = load_trace(context.scene)
        if not trace:
            self.report({"WARNING"}, "No trace is available yet")
            return {"CANCELLED"}

        request_id = context.scene.get("cad_agent_last_request_id")
        text = bpy.data.texts.get("idō Trace") or bpy.data.texts.new("idō Trace")
        text.clear()
        text.write(format_trace_timeline(trace, request_id=request_id))
        for area in context.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = text
                break
        self.report({"INFO"}, "Request timeline loaded in Text Editor")
        return {"FINISHED"}


class IDO_OT_show_openui(Operator):
    bl_idname = "ido.show_openui"
    bl_label = "Show OpenUI Lang"
    bl_description = "Open the OpenUI Lang generative UI description for the last request"

    def execute(self, context):
        openui_lang = load_openui_lang(context.scene)
        if not openui_lang:
            self.report({"WARNING"}, "No OpenUI Lang is available yet")
            return {"CANCELLED"}
        text = bpy.data.texts.get("idō OpenUI") or bpy.data.texts.new("idō OpenUI")
        text.clear()
        text.write(openui_lang)
        for area in context.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = text
                break
        self.report({"INFO"}, "OpenUI Lang loaded in Text Editor")
        return {"FINISHED"}


class IDO_OT_open_guild(Operator):
    bl_idname = "ido.open_guild"
    bl_label = "Open in Guild"
    bl_description = "Open the last request trace in Guild"

    def execute(self, context):
        guild_trace_url = load_guild_trace_url(context.scene)
        if not guild_trace_url:
            self.report({"WARNING"}, "No Guild trace URL is available for this request")
            return {"CANCELLED"}
        _open_guild_url(context, guild_trace_url)
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
            live_box.label(text="Live generation", icon="SCRIPT")
            lines_col = live_box.column(align=True)
            lines_col.scale_y = 0.8
            for line in properties.code_preview.split("\n"):
                lines_col.label(text=line)

        code_box = layout.box()
        code_box.label(text="Code", icon="TEXT")
        code_row = code_box.row(align=True)
        code_row.operator("ido.view_code", text="View", icon="HIDE_OFF")
        code_row.operator("ido.apply_code", text="Apply", icon="FILE_REFRESH")

        trace_row = layout.row(align=True)
        trace_row.operator("ido.show_trace", text="Timeline", icon="TIME")
        trace_row.operator("ido.show_openui", text="OpenUI", icon="UI")
        trace_row.operator("ido.open_guild", text="Guild", icon="URL")

        trace = load_trace(context.scene)
        if trace:
            box = layout.box()
            box.label(text="Last Request Timeline", icon="TIME")
            request_id = context.scene.get("cad_agent_last_request_id")
            if request_id:
                box.label(text=f"Request: {request_id[:12]}...")
            for item in summarize_trace(trace):
                duration = item["duration_ms"]
                duration_text = f"{duration:.1f} ms" if duration is not None else "pending"
                row = box.row()
                row.label(text=f"{item['step']}: {item['status']} ({duration_text})")

            sponsors = layout.box()
            sponsors.label(text="Sponsor Integrations", icon="LINKED")
            if load_guild_trace_url(context.scene):
                sponsors.label(text="Guild: trace exported")
            if load_openui_lang(context.scene):
                sponsors.label(text="OpenUI: lang ready")
            if load_clickhouse_exported(context.scene):
                sponsors.label(text="ClickHouse: trace stored")
            if load_airbyte_context_exported(context.scene):
                sponsors.label(text="Airbyte: context synced")
            composio_status = load_composio_status(context.scene)
            if composio_status:
                sponsors.label(text=f"Composio: {composio_status}")

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
            layout.prop(properties, "auto_open_guild")


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


def _stream_object_line(event: dict) -> str:
    item = event.get("item")
    if isinstance(item, dict):
        return _preview_line(item)
    object_type = event.get("object_type", "object")
    return f"+ ready {object_type} {event.get('name') or event.get('id', '?')}"


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
    IDO_OT_show_trace,
    IDO_OT_show_openui,
    IDO_OT_open_guild,
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
