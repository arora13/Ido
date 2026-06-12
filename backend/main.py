from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.config import Settings
from backend.openscad_service import OpenScadService
from backend.providers.base import IRGenerationError, IRProvider
from backend.providers.factory import create_provider
from backend.status import StatusStore
from backend.tracing import StepTimer, TraceStore
from shared.contracts import (
    ExecutionReport,
    HealthResponse,
    OpenScadPromptRequest,
    OpenScadPromptResponse,
    PetVisibilityRequest,
    PromptRequest,
    PromptResponse,
    RuntimeStatus,
    TraceEvent,
    new_request_id,
)
from shared.validation import IRValidationError, parse_and_validate_ir


def create_app(
    *,
    settings: Settings | None = None,
    provider: IRProvider | None = None,
    trace_store: TraceStore | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    logging.basicConfig(level=resolved_settings.log_level)
    resolved_provider = provider or create_provider(resolved_settings)
    traces = trace_store or TraceStore()
    statuses = StatusStore()
    openscad = OpenScadService(resolved_provider, statuses)

    app = FastAPI(
        title="CAD-Agent API",
        version=__version__,
        description="Engineering IR generation for Blender and OpenSCAD adapters.",
    )
    app.state.provider = resolved_provider
    app.state.traces = traces
    app.state.statuses = statuses
    app.state.openscad = openscad
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8010",
            "http://localhost:8010",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "https://v1shay.github.io",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    site_dir = Path(__file__).resolve().parents[1] / "site"
    if (web_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")
    if site_dir.is_dir():
        app.mount("/info", StaticFiles(directory=site_dir, html=True), name="site")

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(provider=resolved_provider.name)

    @app.post("/api/prompt", response_model=PromptResponse)
    async def prompt(request: PromptRequest) -> PromptResponse:
        request_id = new_request_id()
        statuses.update(
            tool="blender",
            phase="generating",
            message="Generating Blender scene",
            request_id=request_id,
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
                metadata={"provider": resolved_provider.name},
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
            return PromptResponse(
                status="error",
                error=str(exc),
                request_id=request_id,
                provider=resolved_provider.name,
                trace=await traces.get(request_id),
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
            return PromptResponse(
                status="validation_failed",
                error="Generated IR failed validation",
                validation_errors=exc.errors,
                request_id=request_id,
                provider=resolved_provider.name,
                trace=await traces.get(request_id),
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
            metadata={"target_tool": request.target_tool},
        )
        await traces.record(
            request_id,
            "route",
            "completed",
            duration_ms=route_timer.elapsed_ms,
            metadata={"target_tool": request.target_tool},
        )
        statuses.update(
            phase="completed",
            message="Blender scene is ready",
            request_id=request_id,
        )
        return PromptResponse(
            ir=validated,
            status="ok",
            request_id=request_id,
            provider=resolved_provider.name,
            trace=await traces.get(request_id),
        )

    @app.post("/api/execution", response_model=TraceEvent)
    async def execution(report: ExecutionReport) -> TraceEvent:
        statuses.update(
            tool=report.target_tool,
            phase="completed" if report.status == "ok" else "failed",
            message=report.error or f"{report.target_tool.title()} execution completed",
            request_id=report.request_id,
        )
        return await traces.record(
            report.request_id,
            "execute",
            "completed" if report.status == "ok" else "failed",
            duration_ms=report.duration_ms,
            metadata={
                "target_tool": report.target_tool,
                "error": report.error,
            },
        )

    @app.get("/api/traces/{request_id}", response_model=list[TraceEvent])
    async def get_trace(request_id: str) -> list[TraceEvent]:
        return await traces.get(request_id)

    @app.post("/api/openscad/prompt", response_model=OpenScadPromptResponse)
    async def openscad_prompt(request: OpenScadPromptRequest) -> OpenScadPromptResponse:
        return await openscad.prompt(request)

    @app.get("/api/status", response_model=RuntimeStatus)
    async def runtime_status() -> RuntimeStatus:
        return statuses.get()

    @app.post("/api/pet/visibility", response_model=RuntimeStatus)
    async def pet_visibility(request: PetVisibilityRequest) -> RuntimeStatus:
        return statuses.update(pet_visible=request.visible)

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

    @app.get("/ido-pet.svg")
    async def pet_asset() -> FileResponse:
        asset = web_dist / "ido-pet.svg"
        if not asset.is_file():
            raise HTTPException(status_code=404, detail="Pet asset not built")
        return FileResponse(asset)

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
