"""FastAPI entrypoint for ID-ASDLC backend."""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from packages.idasdlc_core import __version__, get_settings, init_db

from .routers import curation, evals, export, retrieval, runs, tasks


def create_app() -> FastAPI:
    get_settings()
    init_db()
    app = FastAPI(
        title="ID-ASDLC Backend",
        version=__version__,
        description="APIs for iterative deployment loops",
    )
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(curation.router)
    app.include_router(evals.router)
    app.include_router(retrieval.router)
    app.include_router(export.router)

    @app.get("/health")
    def health() -> dict[str, str]:  # pragma: no cover - trivial
        return {"status": "ok"}

    return app


app = create_app()


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:  # pragma: no cover - uvicorn hook
    uvicorn.run("apps.backend.main:app", host=host, port=port, reload=reload)
