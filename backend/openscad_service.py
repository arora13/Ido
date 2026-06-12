from __future__ import annotations

import os
from pathlib import Path

from adapters.openscad.adapter import compile_ir_to_scad, export_with_openscad, write_scad
from backend.providers.base import IRProvider
from backend.status import StatusStore
from shared.contracts import (
    OpenScadExecution,
    OpenScadPromptRequest,
    OpenScadPromptResponse,
    new_request_id,
)
from shared.validation import IRValidationError, parse_and_validate_ir


class OpenScadService:
    def __init__(self, provider: IRProvider, status_store: StatusStore) -> None:
        self.provider = provider
        self.status_store = status_store
        self.output_dir = Path(
            os.getenv("IDO_OUTPUT_DIR", str(Path.home() / ".ido" / "projects" / "default"))
        )

    async def prompt(self, request: OpenScadPromptRequest) -> OpenScadPromptResponse:
        request_id = new_request_id()
        self.status_store.update(
            tool="openscad",
            phase="generating",
            message="Generating engineering model",
            request_id=request_id,
            active_project=str(self.output_dir),
        )
        try:
            generated = await self.provider.generate(request.prompt, request.current_ir)
            self.status_store.update(phase="validating", message="Validating OpenSCAD source")
            validated = parse_and_validate_ir(generated)
            source = compile_ir_to_scad(validated)
            scad_path = write_scad(source, self.output_dir)
            self.status_store.update(phase="rendering", message="Exporting OpenSCAD artifacts")
            artifacts, export_errors = export_with_openscad(
                scad_path,
                list(request.export_formats),
            )
            execution = OpenScadExecution(
                scad_path=str(scad_path),
                scad_source=source,
                artifacts=artifacts,
                export_errors=export_errors,
            )
            self.status_store.update(
                phase="completed",
                message="OpenSCAD project updated",
                artifacts=artifacts,
            )
            return OpenScadPromptResponse(
                ir=validated,
                status="ok",
                request_id=request_id,
                provider=self.provider.name,
                execution=execution,
            )
        except IRValidationError as exc:
            self.status_store.fail(
                tool="openscad",
                message="Generated model failed validation",
                request_id=request_id,
            )
            return OpenScadPromptResponse(
                status="validation_failed",
                error="Generated IR failed validation",
                validation_errors=exc.errors,
                request_id=request_id,
                provider=self.provider.name,
            )
        except Exception as exc:
            self.status_store.fail(
                tool="openscad",
                message=str(exc),
                request_id=request_id,
            )
            return OpenScadPromptResponse(
                status="error",
                error=str(exc),
                request_id=request_id,
                provider=self.provider.name,
            )
