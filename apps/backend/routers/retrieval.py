"""Retrieval endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from packages.idasdlc_core.retrieval import RetrievalResult, TraceRetrievalService

from ..api.dependencies import get_retrieval_service

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


class RetrievalResponse(BaseModel):
    results: list[RetrievalResult]


@router.get("/search", response_model=RetrievalResponse)
def search(query: str, limit: int = 5, service: TraceRetrievalService = Depends(get_retrieval_service)):
    return RetrievalResponse(results=service.search(query, limit=limit))
