"""Export endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from packages.idasdlc_core.services import ExportService

from ..api.dependencies import get_export_service

router = APIRouter(prefix="/export", tags=["export"])


class ExportRequest(BaseModel):
    path: Path = Path(".idasdlc/datasets/curated.jsonl")
    include_trace: bool = True


class ExportResponse(BaseModel):
    path: Path


@router.post("", response_model=ExportResponse)
def export_dataset(
    request: ExportRequest,
    exporter: ExportService = Depends(get_export_service),
):
    path = exporter.export_curated(request.path, include_trace=request.include_trace)
    return ExportResponse(path=path)
