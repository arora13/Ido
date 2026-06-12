from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.airbyte_exporter import AirbyteContextExporter, create_airbyte_exporter
from backend.clickhouse_exporter import ClickHouseTraceExporter, create_clickhouse_exporter
from backend.composio_exporter import ComposioActionExporter, create_composio_exporter
from backend.config import Settings
from backend.guild_exporter import GuildTraceExporter, create_guild_exporter
from backend.openui_exporter import OpenUIExporter
from backend.openscad_service import OpenScadService
from backend.providers.base import IRGenerationError, IRProvider
from backend.providers.factory import create_provider, inference_provider_name
from backend.integrations import build_integrations_status
from backend.request_context import RequestContextStore
from backend.sponsor_exports import SponsorExportBundle, export_sponsors
from backend.status import StatusStore
from backend.tracing import StepTimer, TraceStore
from shared.contracts import (
    ExecutionReport,
    ExecutionResponse,
    HealthResponse,
    IntegrationsStatus,
    OpenScadPromptRequest,
    OpenScadPromptResponse,
    PromptRequest,
    PromptResponse,
    RuntimeStatus,
    TraceAnalyticsResponse,
    TraceAnalyticsRow,
    TraceEvent,
    new_request_id,
)
from shared.ir import EngineeringIR
from shared.validation import IRValidationError, parse_and_validate_ir

logger = logging.getLogger("cad_agent.api")
ProgressCallback = Callable[[dict[str, object]], Awaitable[None]]


def _response_from_bundle(
    bundle: SponsorExportBundle,
    *,
    trace: list[TraceEvent],
    **fields,
) -> dict:
    return {
        **fields,
        "trace": trace,
        "guild_trace_url": bundle.guild_trace_url,
        "openui_lang": bundle.openui_lang,
        "openui_elements": list(bundle.openui_elements),
        "scene_headline": bundle.scene_headline,
        "clickhouse_exported": bundle.clickhouse_exported,
        "composio_status": bundle.composio_status,
        "airbyte_context_exported": bundle.airbyte_context_exported,
    }


def _provider_metadata(provider: IRProvider, **extra: object) -> dict[str, object]:
    metadata: dict[str, object] = {"provider": provider.name}
    inference = inference_provider_name(provider)
    if inference:
        metadata["inference_provider"] = inference
    metadata.update(extra)
    return metadata


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


class StreamedObjectParser:
    """Extract complete top-level objects from a streamed scene.objects array."""

    def __init__(self) -> None:
        self._text = ""
        self._cursor = 0
        self._in_objects = False
        self._start: int | None = None
        self._depth = 0
        self._in_string = False
        self._escaped = False

    def reset(self) -> None:
        self.__init__()

    def feed(self, delta: str) -> list[dict[str, object]]:
        self._text += delta
        if not self._in_objects:
            key_index = self._text.find('"objects"')
            if key_index < 0:
                return []
            array_index = self._text.find("[", key_index + len('"objects"'))
            if array_index < 0:
                return []
            self._in_objects = True
            self._cursor = array_index + 1

        objects: list[dict[str, object]] = []
        while self._cursor < len(self._text):
            char = self._text[self._cursor]
            if self._start is None:
                if char == "]":
                    self._cursor += 1
                    break
                if char != "{":
                    self._cursor += 1
                    continue
                self._start = self._cursor
                self._depth = 1
                self._in_string = False
                self._escaped = False
                self._cursor += 1
                continue

            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
            elif char == '"':
                self._in_string = True
            elif char == "{":
                self._depth += 1
            elif char == "}":
                self._depth -= 1
                if self._depth == 0:
                    raw = self._text[self._start : self._cursor + 1]
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        objects.append(parsed)
                    self._start = None
            self._cursor += 1
        return objects


def create_app(
    *,
    settings: Settings | None = None,
    provider: IRProvider | None = None,
    trace_store: TraceStore | None = None,
    guild_exporter: GuildTraceExporter | None = None,
    clickhouse_exporter: ClickHouseTraceExporter | None = None,
    composio_exporter: ComposioActionExporter | None = None,
    openui_exporter: OpenUIExporter | None = None,
    airbyte_exporter: AirbyteContextExporter | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    logging.basicConfig(level=resolved_settings.log_level)
    resolved_provider = provider or create_provider(resolved_settings)
    traces = trace_store or TraceStore()
    request_contexts = RequestContextStore()
    statuses = StatusStore()
    openscad = OpenScadService(statuses)
    resolved_guild_exporter = guild_exporter or create_guild_exporter(resolved_settings)
    resolved_clickhouse_exporter = (
        clickhouse_exporter or create_clickhouse_exporter(resolved_settings)
    )
    resolved_composio_exporter = composio_exporter or create_composio_exporter(resolved_settings)
    resolved_openui_exporter = openui_exporter or OpenUIExporter()
    resolved_airbyte_exporter = airbyte_exporter or create_airbyte_exporter(resolved_settings)

    app = FastAPI(
        title="CAD-Agent API",
        version=__version__,
        description="Engineering IR generation for Blender and OpenSCAD adapters.",
    )
    app.state.provider = resolved_provider
    app.state.traces = traces
    app.state.statuses = statuses
    app.state.openscad = openscad
    app.state.guild_exporter = resolved_guild_exporter
    app.state.clickhouse_exporter = resolved_clickhouse_exporter
    app.state.composio_exporter = resolved_composio_exporter
    app.state.openui_exporter = resolved_openui_exporter
    app.state.airbyte_exporter = resolved_airbyte_exporter
    statuses.update(
        provider=resolved_provider.name,
        clickhouse_enabled=resolved_clickhouse_exporter.enabled,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8010",
            "http://localhost:8010",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if (web_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

    async def finalize_exports(
        *,
        request_id: str,
        events: list[TraceEvent],
        prompt: str | None = None,
        target_tool: str | None = None,
        ir: EngineeringIR | None = None,
        notify_composio: bool = False,
        execution_status: str = "ok",
        airbyte_event_type: str = "prompt_completed",
    ) -> SponsorExportBundle:
        return await export_sponsors(
            guild_exporter=resolved_guild_exporter,
            clickhouse_exporter=resolved_clickhouse_exporter,
            composio_exporter=resolved_composio_exporter,
            openui_exporter=resolved_openui_exporter,
            airbyte_exporter=resolved_airbyte_exporter,
            request_id=request_id,
            events=events,
            prompt=prompt,
            target_tool=target_tool,
            ir=ir,
            notify_composio=notify_composio,
            execution_status=execution_status,
            airbyte_event_type=airbyte_event_type,
        )

    async def _publish_integration_status(
        *,
        bundle: SponsorExportBundle,
        inference_provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        statuses.update(
            provider=resolved_provider.name,
            inference_provider=inference_provider,
            clickhouse_enabled=resolved_clickhouse_exporter.enabled,
            clickhouse_exported=bundle.clickhouse_exported,
            request_id=request_id,
        )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(provider=resolved_provider.name)

    async def generate_prompt(
        request: PromptRequest,
        *,
        request_id: str,
        progress: ProgressCallback | None = None,
    ) -> PromptResponse:
        async def emit(event: dict[str, object]) -> None:
            if progress is not None:
                await progress(event)

        statuses.update(
            tool="blender",
            phase="generating",
            message="Generating Blender scene",
            request_id=request_id,
        )
        await emit(
            {
                "type": "status",
                "message": "Generating scene...",
                "phase": "generating",
                "request_id": request_id,
            }
        )
        parse_timer = StepTimer()
        await traces.record(request_id, "parse", "started")
        streamed_objects = StreamedObjectParser()
        emitted_object_ids: set[str] = set()

        async def provider_progress(event: dict[str, object]) -> None:
            event_type = event.get("type")
            if event_type == "code_reset":
                streamed_objects.reset()
                emitted_object_ids.clear()
                await emit(event)
                return
            if event_type != "code":
                await emit(event)
                return

            await emit(event)
            content = event.get("content")
            if not isinstance(content, str):
                return
            for item in streamed_objects.feed(content):
                object_id = item.get("id")
                if not isinstance(object_id, str) or object_id in emitted_object_ids:
                    continue
                emitted_object_ids.add(object_id)
                await emit(
                    {
                        "type": "object",
                        "id": object_id,
                        "name": item.get("label") or object_id,
                        "object_type": item.get("type") or "object",
                        "shape": item.get("shape"),
                        "item": item,
                        "status": "generated",
                    }
                )

        try:
            stream_generate = getattr(resolved_provider, "generate_stream", None)
            if progress is not None and stream_generate is not None:
                generated = await stream_generate(
                    request.prompt,
                    request.current_ir,
                    provider_progress,
                )
            else:
                generated = await resolved_provider.generate(
                    request.prompt,
                    request.current_ir,
                )
            await traces.record(
                request_id,
                "parse",
                "completed",
                duration_ms=parse_timer.elapsed_ms,
                metadata=_provider_metadata(resolved_provider),
            )
        except IRGenerationError as exc:
            statuses.fail(tool="blender", message=str(exc), request_id=request_id)
            await traces.record(
                request_id,
                "parse",
                "failed",
                duration_ms=parse_timer.elapsed_ms,
                metadata={"error": str(exc)},
            )
            trace = await traces.get(request_id)
            bundle = await finalize_exports(
                request_id=request_id,
                events=trace,
                prompt=request.prompt,
                target_tool=request.target_tool,
            )
            response = PromptResponse(
                **_response_from_bundle(
                    bundle,
                    trace=trace,
                    status="error",
                    error=str(exc),
                    request_id=request_id,
                    provider=resolved_provider.name,
                )
            )
            await emit(
                {
                    "type": "error",
                    "message": str(exc),
                    "result": response.model_dump(mode="json"),
                }
            )
            return response

        statuses.update(
            phase="validating",
            message="Validating generated scene",
            request_id=request_id,
        )
        await emit(
            {
                "type": "status",
                "message": "Validating generated scene...",
                "phase": "validating",
                "request_id": request_id,
            }
        )
        validation_timer = StepTimer()
        await traces.record(request_id, "validate", "started")
        try:
            validated = parse_and_validate_ir(generated)
        except IRValidationError as exc:
            statuses.fail(
                tool="blender",
                message="Generated model failed validation",
                request_id=request_id,
            )
            await traces.record(
                request_id,
                "validate",
                "failed",
                duration_ms=validation_timer.elapsed_ms,
                metadata={"errors": exc.errors},
            )
            trace = await traces.get(request_id)
            bundle = await finalize_exports(
                request_id=request_id,
                events=trace,
                prompt=request.prompt,
                target_tool=request.target_tool,
            )
            response = PromptResponse(
                **_response_from_bundle(
                    bundle,
                    trace=trace,
                    status="validation_failed",
                    error="Generated IR failed validation",
                    validation_errors=exc.errors,
                    request_id=request_id,
                    provider=resolved_provider.name,
                )
            )
            await emit(
                {
                    "type": "error",
                    "message": "Generated IR failed validation",
                    "result": response.model_dump(mode="json"),
                }
            )
            return response

        await traces.record(
            request_id,
            "validate",
            "completed",
            duration_ms=validation_timer.elapsed_ms,
        )
        for item in validated.scene.objects:
            if item.id in emitted_object_ids:
                continue
            await emit(
                {
                    "type": "object",
                    "id": item.id,
                    "name": item.label,
                    "object_type": item.type,
                    "shape": getattr(item, "shape", None),
                    "item": item.model_dump(mode="json"),
                    "status": "generated",
                }
            )
        statuses.update(
            phase="generating",
            message="Preparing Blender scene",
            request_id=request_id,
        )
        await emit(
            {
                "type": "status",
                "message": "Preparing Blender scene...",
                "phase": "routing",
                "request_id": request_id,
            }
        )
        route_timer = StepTimer()
        await traces.record(
            request_id,
            "route",
            "started",
            metadata={"target_tool": request.target_tool},
        )
        await traces.record(
            request_id,
            "route",
            "completed",
            duration_ms=route_timer.elapsed_ms,
            metadata={"target_tool": request.target_tool},
        )
        trace = await traces.get(request_id)
        bundle = await finalize_exports(
            request_id=request_id,
            events=trace,
            prompt=request.prompt,
            target_tool=request.target_tool,
            ir=validated,
        )
        inference = inference_provider_name(resolved_provider)
        await request_contexts.set(
            request_id,
            prompt=request.prompt,
            target_tool=request.target_tool,
            ir=validated,
            inference_provider=inference,
        )
        await _publish_integration_status(
            bundle=bundle,
            inference_provider=inference,
            request_id=request_id,
        )
        statuses.update(
            phase="completed",
            message="Blender scene is ready",
            request_id=request_id,
        )
        response = PromptResponse(
            **_response_from_bundle(
                bundle,
                trace=trace,
                ir=validated,
                status="ok",
                request_id=request_id,
                provider=resolved_provider.name,
            )
        )
        await emit(
            {
                "type": "status",
                "message": "Blender scene is ready",
                "phase": "completed",
                "request_id": request_id,
            }
        )
        return response

    @app.post("/api/prompt", response_model=PromptResponse)
    async def prompt(request: PromptRequest) -> PromptResponse:
        return await generate_prompt(request, request_id=new_request_id())

    @app.post("/api/prompt/stream")
    async def prompt_stream(
        request: PromptRequest,
        http_request: FastAPIRequest,
    ) -> StreamingResponse:
        request_id = new_request_id()

        async def event_generator():
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            started = monotonic()
            latest_message = "Parsing prompt..."

            async def publish(event: dict[str, object]) -> None:
                await queue.put(event)

            yield _sse(
                {
                    "type": "status",
                    "message": latest_message,
                    "phase": "parsing",
                    "request_id": request_id,
                    "elapsed_seconds": 0,
                }
            )
            task = asyncio.create_task(
                generate_prompt(
                    request,
                    request_id=request_id,
                    progress=publish,
                )
            )
            try:
                while True:
                    if await http_request.is_disconnected():
                        task.cancel()
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=1.0)
                        if event.get("type") == "status":
                            latest_message = str(event.get("message") or latest_message)
                        yield _sse(event)
                        if event.get("type") == "error":
                            await task
                            break
                    except TimeoutError:
                        if task.done():
                            response = await task
                            yield _sse(
                                {
                                    "type": "done",
                                    "result": response.model_dump(mode="json"),
                                }
                            )
                            break
                        yield _sse(
                            {
                                "type": "status",
                                "message": latest_message,
                                "phase": "generating",
                                "request_id": request_id,
                                "elapsed_seconds": int(monotonic() - started),
                            }
                        )

                    if task.done() and queue.empty():
                        response = await task
                        yield _sse(
                            {
                                "type": "done",
                                "result": response.model_dump(mode="json"),
                            }
                        )
                        break
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/execution", response_model=ExecutionResponse)
    async def execution(report: ExecutionReport) -> ExecutionResponse:
        statuses.update(
            tool=report.target_tool,
            phase="completed" if report.status == "ok" else "failed",
            message=report.error or f"{report.target_tool.title()} execution completed",
            request_id=report.request_id,
        )
        event = await traces.record(
            report.request_id,
            "execute",
            "completed" if report.status == "ok" else "failed",
            duration_ms=report.duration_ms,
            metadata={
                "target_tool": report.target_tool,
                "error": report.error,
            },
        )
        trace = await traces.get(report.request_id)
        context = await request_contexts.get(report.request_id)
        bundle = await finalize_exports(
            request_id=report.request_id,
            events=trace,
            prompt=context.prompt if context else None,
            target_tool=report.target_tool,
            ir=context.ir if context else None,
            notify_composio=True,
            execution_status=report.status,
            airbyte_event_type="execution_completed",
        )
        await _publish_integration_status(
            bundle=bundle,
            inference_provider=context.inference_provider if context else None,
            request_id=report.request_id,
        )
        return ExecutionResponse(
            event=event,
            **_response_from_bundle(bundle, trace=trace),
        )

    @app.get("/api/traces/{request_id}", response_model=list[TraceEvent])
    async def get_trace(request_id: str) -> list[TraceEvent]:
        return await traces.get(request_id)

    @app.get("/api/integrations", response_model=IntegrationsStatus)
    async def integrations() -> IntegrationsStatus:
        reachable = None
        if resolved_clickhouse_exporter.enabled:
            reachable = await resolved_clickhouse_exporter.ping()
        return build_integrations_status(
            settings=resolved_settings,
            provider=resolved_provider,
            guild_exporter=resolved_guild_exporter,
            clickhouse_exporter=resolved_clickhouse_exporter,
            composio_exporter=resolved_composio_exporter,
            airbyte_exporter=resolved_airbyte_exporter,
            clickhouse_reachable=reachable,
        )

    @app.get("/api/analytics/traces", response_model=TraceAnalyticsResponse)
    async def trace_analytics(limit: int = 20) -> TraceAnalyticsResponse:
        if not resolved_clickhouse_exporter.enabled:
            return TraceAnalyticsResponse(enabled=False, rows=[])
        rows = await resolved_clickhouse_exporter.query_recent(limit=min(limit, 100))
        return TraceAnalyticsResponse(
            enabled=True,
            rows=[TraceAnalyticsRow.model_validate(row) for row in rows],
        )

    @app.post("/api/openscad/prompt", response_model=OpenScadPromptResponse)
    async def openscad_prompt(request: OpenScadPromptRequest) -> OpenScadPromptResponse:
        request_id = new_request_id()
        target_tool = "openscad"
        statuses.update(
            tool=target_tool,
            phase="generating",
            message="Generating engineering model",
            request_id=request_id,
            active_project=str(openscad.output_dir),
        )
        parse_timer = StepTimer()
        await traces.record(request_id, "parse", "started")
        try:
            generated = await resolved_provider.generate(
                request.prompt,
                request.current_ir,
            )
            await traces.record(
                request_id,
                "parse",
                "completed",
                duration_ms=parse_timer.elapsed_ms,
                metadata=_provider_metadata(resolved_provider),
            )
        except IRGenerationError as exc:
            statuses.fail(tool=target_tool, message=str(exc), request_id=request_id)
            await traces.record(
                request_id,
                "parse",
                "failed",
                duration_ms=parse_timer.elapsed_ms,
                metadata={"error": str(exc)},
            )
            trace = await traces.get(request_id)
            bundle = await finalize_exports(
                request_id=request_id,
                events=trace,
                prompt=request.prompt,
                target_tool=target_tool,
            )
            return OpenScadPromptResponse(
                **_response_from_bundle(
                    bundle,
                    trace=trace,
                    status="error",
                    error=str(exc),
                    request_id=request_id,
                    provider=resolved_provider.name,
                )
            )

        validation_timer = StepTimer()
        await traces.record(request_id, "validate", "started")
        statuses.update(phase="validating", message="Validating OpenSCAD source")
        try:
            validated = parse_and_validate_ir(generated)
        except IRValidationError as exc:
            statuses.fail(
                tool=target_tool,
                message="Generated model failed validation",
                request_id=request_id,
            )
            await traces.record(
                request_id,
                "validate",
                "failed",
                duration_ms=validation_timer.elapsed_ms,
                metadata={"errors": exc.errors},
            )
            trace = await traces.get(request_id)
            bundle = await finalize_exports(
                request_id=request_id,
                events=trace,
                prompt=request.prompt,
                target_tool=target_tool,
            )
            return OpenScadPromptResponse(
                **_response_from_bundle(
                    bundle,
                    trace=trace,
                    status="validation_failed",
                    error="Generated IR failed validation",
                    validation_errors=exc.errors,
                    request_id=request_id,
                    provider=resolved_provider.name,
                )
            )

        await traces.record(
            request_id,
            "validate",
            "completed",
            duration_ms=validation_timer.elapsed_ms,
        )
        route_timer = StepTimer()
        await traces.record(
            request_id,
            "route",
            "started",
            metadata={"target_tool": target_tool},
        )
        await traces.record(
            request_id,
            "route",
            "completed",
            duration_ms=route_timer.elapsed_ms,
            metadata={"target_tool": target_tool},
        )

        execute_timer = StepTimer()
        await traces.record(request_id, "execute", "started", metadata={"target_tool": target_tool})
        execution_status = "ok"
        execution = None
        try:
            execution = openscad.render(validated, list(request.export_formats))
        except Exception as exc:
            execution_status = "error"
            statuses.fail(tool=target_tool, message=str(exc), request_id=request_id)
            await traces.record(
                request_id,
                "execute",
                "failed",
                duration_ms=execute_timer.elapsed_ms,
                metadata={"target_tool": target_tool, "error": str(exc)},
            )
            trace = await traces.get(request_id)
            bundle = await finalize_exports(
                request_id=request_id,
                events=trace,
                prompt=request.prompt,
                target_tool=target_tool,
                ir=validated,
                notify_composio=True,
                execution_status=execution_status,
                airbyte_event_type="execution_completed",
            )
            return OpenScadPromptResponse(
                **_response_from_bundle(
                    bundle,
                    trace=trace,
                    status="error",
                    error=str(exc),
                    request_id=request_id,
                    provider=resolved_provider.name,
                )
            )

        await traces.record(
            request_id,
            "execute",
            "completed",
            duration_ms=execute_timer.elapsed_ms,
            metadata={
                "target_tool": target_tool,
                "export_errors": execution.export_errors,
            },
        )
        trace = await traces.get(request_id)
        bundle = await finalize_exports(
            request_id=request_id,
            events=trace,
            prompt=request.prompt,
            target_tool=target_tool,
            ir=validated,
            notify_composio=True,
            execution_status=execution_status,
            airbyte_event_type="execution_completed",
        )
        inference = inference_provider_name(resolved_provider)
        await _publish_integration_status(
            bundle=bundle,
            inference_provider=inference,
            request_id=request_id,
        )
        return OpenScadPromptResponse(
            **_response_from_bundle(
                bundle,
                trace=trace,
                ir=validated,
                status="ok",
                request_id=request_id,
                provider=resolved_provider.name,
                execution=execution,
            )
        )

    @app.get("/api/status", response_model=RuntimeStatus)
    async def runtime_status() -> RuntimeStatus:
        return statuses.get()


    @app.get("/api/artifacts/{filename}")
    async def artifact(filename: str) -> FileResponse:
        if filename != Path(filename).name:
            raise HTTPException(status_code=400, detail="Invalid artifact name")
        artifact_path = (openscad.output_dir / filename).resolve()
        if openscad.output_dir.resolve() not in artifact_path.parents:
            raise HTTPException(status_code=400, detail="Invalid artifact path")
        if not artifact_path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(artifact_path)

    @app.get("/downloads/ido_blender.zip")
    async def blender_addon_download() -> FileResponse:
        addon = Path(__file__).resolve().parents[1] / "ido_blender.zip"
        if not addon.is_file():
            raise HTTPException(
                status_code=404,
                detail="Build the add-on with: cd adapters/blender && zip -r ../../ido_blender.zip ido_blender",
            )
        return FileResponse(
            addon,
            media_type="application/zip",
            filename="ido_blender.zip",
        )


    @app.get("/", response_model=None)
    async def control_panel() -> FileResponse | HTMLResponse:
        web_index = web_dist / "index.html"
        if web_index.is_file():
            return FileResponse(web_index)
        return HTMLResponse(
            """
            <!doctype html>
            <html lang="en"><meta charset="utf-8"><title>idō</title>
            <style>body{font:16px system-ui;background:#050505;color:#f5f5f5;
            max-width:720px;margin:12vh auto;padding:24px}a{color:white}</style>
            <h1>idō companion is running</h1>
            <p>Build the website with <code>cd web && npm run build</code> to use
            the local control panel.</p><p><a href="/docs">Open API docs</a></p>
            </html>
            """
        )

    return app


app = create_app()
