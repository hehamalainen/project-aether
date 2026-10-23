"""
Aether Server - Agent-Native Version Control System.

Main application entry point.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
import hmac
import os
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import SQLModel, create_engine, text

from packages.aether.api.routes import router, set_services
from packages.aether.api.websocket import (
    websocket_endpoint,
    ConnectionManager,
    set_connection_manager,
)
from packages.aether.services.event_bus import EventBus
from apps.aether_server.ingest_worker import (
    IngestMaterializationConfig,
    start_worker as start_ingest_worker,
    stop_worker as stop_ingest_worker,
)
from apps.aether_server.promotion_worker import (
    AutoPromotionConfig,
    start_worker as start_promotion_worker,
    stop_worker as stop_promotion_worker,
)

logger = structlog.get_logger()

# Configuration
AETHER_HOME = Path.home() / ".aether"
AETHER_HOME.mkdir(parents=True, exist_ok=True)

import os

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{AETHER_HOME}/aether.db")
STORAGE_PATH = AETHER_HOME / "aether_storage"
STORAGE_PATH.mkdir(parents=True, exist_ok=True)


def setup_logging():
    """Configure structured logging."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# Global services
_engine = None
_event_bus = None


def _normalized_server_profile() -> str:
    raw = os.getenv("AETHER_SERVER_PROFILE", "default").strip().lower()
    if not raw:
        return "default"
    return raw


def _resolve_worker_configs() -> tuple[str, IngestMaterializationConfig, AutoPromotionConfig]:
    """Resolve worker configs, allowing a named server profile to supply defaults."""
    profile = _normalized_server_profile()
    materialization_config = IngestMaterializationConfig.from_env()
    auto_promotion_config = AutoPromotionConfig.from_env()

    if profile == "mx_proof":
        if os.getenv("AETHER_AUTOPROMOTE_ENABLED") is None:
            auto_promotion_config = replace(auto_promotion_config, enabled=True)
        if os.getenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS") is None:
            auto_promotion_config = replace(auto_promotion_config, interval_seconds=5)

    logger.info(
        "worker_configs_resolved",
        server_profile=profile,
        materialize_enabled=materialization_config.enabled,
        materialize_interval_seconds=materialization_config.interval_seconds,
        autopromote_enabled=auto_promotion_config.enabled,
        autopromote_interval_seconds=auto_promotion_config.interval_seconds,
    )
    return profile, materialization_config, auto_promotion_config


def _ensure_proposal_source_columns(engine) -> None:
    """Backfill proposal source columns for existing SQLite databases."""
    required_columns = {
        "source": "TEXT DEFAULT 'manual'",
        "source_run_id": "TEXT",
        "source_task_id": "TEXT",
        "source_agent_id": "TEXT",
        "source_attempt_id": "TEXT",
        "intent_type": "TEXT",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info('aether_proposals')")).fetchall()
        existing = {str(row[1]) for row in rows}
        for column, definition in required_columns.items():
            if column in existing:
                continue
            conn.execute(text(f"ALTER TABLE aether_proposals ADD COLUMN {column} {definition}"))
        conn.execute(
            text(
                "UPDATE aether_proposals SET source = 'manual' "
                "WHERE source IS NULL OR source = ''"
            )
        )


def _ensure_proposal_contention_columns(engine) -> None:
    """Backfill proposal contention columns for existing databases."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("PRAGMA table_info('aether_proposal_contentions')")).fetchall()
            existing = {str(row[1]) for row in rows}
            if "defer_evaluation" not in existing:
                conn.execute(text("ALTER TABLE aether_proposal_contentions ADD COLUMN defer_evaluation BOOLEAN DEFAULT 0"))
        elif engine.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE aether_proposal_contentions ADD COLUMN IF NOT EXISTS defer_evaluation BOOLEAN DEFAULT FALSE"
            ))


def _ensure_rebase_columns(engine) -> None:
    """Backfill rebase columns for proposals and repositories."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            # proposals
            rows = conn.execute(text("PRAGMA table_info('aether_proposals')")).fetchall()
            existing = {str(row[1]) for row in rows}
            if "rebase_attempts" not in existing:
                conn.execute(text("ALTER TABLE aether_proposals ADD COLUMN rebase_attempts INTEGER DEFAULT 0"))
            # repositories
            rows_repo = conn.execute(text("PRAGMA table_info('aether_repositories')")).fetchall()
            existing_repo = {str(row[1]) for row in rows_repo}
            if "max_rebase_attempts" not in existing_repo:
                conn.execute(text("ALTER TABLE aether_repositories ADD COLUMN max_rebase_attempts INTEGER DEFAULT 2"))
        elif engine.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE aether_proposals ADD COLUMN IF NOT EXISTS rebase_attempts INTEGER DEFAULT 0"
            ))
            conn.execute(text(
                "ALTER TABLE aether_repositories ADD COLUMN IF NOT EXISTS max_rebase_attempts INTEGER DEFAULT 2"
            ))


def _ensure_metric_event_columns_and_indices(engine) -> None:
    """Ensure system metric events have the required columns and indices idempotently."""
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("PRAGMA table_info('aether_system_metric_events')")).fetchall()
            existing = {str(row[1]) for row in rows}
            if "synthesis_engine" not in existing:
                conn.execute(text("ALTER TABLE aether_system_metric_events ADD COLUMN synthesis_engine TEXT DEFAULT NULL"))
            if "source_run_id" not in existing:
                conn.execute(text("ALTER TABLE aether_system_metric_events ADD COLUMN source_run_id TEXT DEFAULT NULL"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_metric_events_source_run_id ON aether_system_metric_events (source_run_id)"
            ))
        elif engine.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE aether_system_metric_events ADD COLUMN IF NOT EXISTS synthesis_engine TEXT DEFAULT NULL"
            ))
            conn.execute(text(
                "ALTER TABLE aether_system_metric_events ADD COLUMN IF NOT EXISTS source_run_id TEXT DEFAULT NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_metric_events_source_run_id ON aether_system_metric_events (source_run_id)"
            ))


def init_database():
    """Initialize database and create tables."""
    global _engine

    # Import all models to register them
    from packages.aether.models.entities import (
        Repository,
        Proposal,
        ConflictSet,
        Snapshot,
        GateResult,
        Promotion,
        OperationGraphNode,
        SystemMetricEvent,
        AetherHead,
    )

    connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
    
    _engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args=connect_args,
    )
    
    # Enable WAL mode for better concurrency on SQLite
    if _engine.dialect.name == "sqlite":
        with _engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
            conn.execute(text("PRAGMA synchronous=NORMAL;"))
        
    SQLModel.metadata.create_all(_engine)
    if _engine.dialect.name == "sqlite":
        _ensure_proposal_source_columns(_engine)
    _ensure_proposal_contention_columns(_engine)
    _ensure_rebase_columns(_engine)
    _ensure_metric_event_columns_and_indices(_engine)

    logger.info("database_initialized", url=DATABASE_URL)


def init_services():
    """Initialize shared global resources."""
    global _event_bus

    # Ensure storage path exists
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)

    # Initialize event bus
    if _event_bus is None:
        _event_bus = EventBus()

    # Set up API dependency injection resources
    set_services(_engine, _event_bus, STORAGE_PATH)

    # Set up WebSocket connection manager
    connection_manager = ConnectionManager(_event_bus)
    set_connection_manager(connection_manager)

    logger.info("services_initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger.info("aether_server_starting", version="0.1.0")

    init_database()
    init_services()

    profile, materialization_config, auto_promotion_config = _resolve_worker_configs()

    # Start ingest materialization worker in a separate process (enabled by default).
    ingest_worker_handle = start_ingest_worker(
        database_url=DATABASE_URL,
        storage_path=STORAGE_PATH,
        config=materialization_config,
    )

    # Start autonomous promotion worker in a separate process.
    promotion_worker_handle = start_promotion_worker(
        database_url=DATABASE_URL,
        storage_path=STORAGE_PATH,
        config=auto_promotion_config,
    )

    logger.info("aether_server_ready", server_profile=profile)

    yield

    # Shutdown
    logger.info("aether_server_shutting_down")

    stop_promotion_worker(promotion_worker_handle)
    stop_ingest_worker(ingest_worker_handle)


DEFAULT_SERVER_HOST = "127.0.0.1"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8100",
    "http://127.0.0.1:8100",
]


def _is_truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_cors_origins() -> list[str]:
    """Resolve allowed browser CORS origins, defaulting to local dev origins only."""
    raw = os.getenv("AETHER_CORS_ORIGINS", "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if "*" in origins and not _is_truthy_env("AETHER_ALLOW_UNSAFE_CORS_WILDCARD", False):
        raise RuntimeError(
            "Wildcard AETHER_CORS_ORIGINS='*' is refused by default. "
            "Specify explicit origins or set AETHER_ALLOW_UNSAFE_CORS_WILDCARD=true."
        )
    return origins or list(DEFAULT_CORS_ORIGINS)


def _resolve_server_host(host: str | None = None) -> str:
    """Resolve bind host, enforcing loopback default unless unsafe network bind + auth are configured."""
    resolved = (host if host is not None else os.getenv("AETHER_HOST", DEFAULT_SERVER_HOST)).strip()
    if not resolved:
        resolved = DEFAULT_SERVER_HOST
    if resolved not in LOOPBACK_HOSTS:
        if not _is_truthy_env("AETHER_ALLOW_UNSAFE_NETWORK_BIND", False):
            raise RuntimeError(
                f"Refusing to bind Aether server to non-loopback host '{resolved}' by default. "
                "Network deployment requires authentication (AETHER_API_TOKEN) and execution isolation, "
                "plus explicit opt-in via AETHER_ALLOW_UNSAFE_NETWORK_BIND=true."
            )
        if not os.getenv("AETHER_API_TOKEN", "").strip():
            raise RuntimeError(
                f"Refusing to bind Aether server to non-loopback host '{resolved}' without AETHER_API_TOKEN set."
            )
    return resolved


# Create FastAPI app
app = FastAPI(
    title="Project Aether",
    description="Probabilistic, CRDT-Powered Version Control for Agent Swarms",
    version="0.1.0",
    lifespan=lifespan,
)

# Add CORS middleware with restricted local origins by default
_cors_origins = _resolve_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Aether-Token"],
)


@app.middleware("http")
async def api_token_auth_middleware(request: Request, call_next):
    """Enforce bearer/header token authentication when AETHER_API_TOKEN is configured."""
    configured_token = os.getenv("AETHER_API_TOKEN", "").strip()
    if configured_token and request.method != "OPTIONS" and request.url.path not in {"/", "/v1/health"}:
        auth_header = request.headers.get("Authorization", "")
        bearer_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        header_token = request.headers.get("X-Aether-Token", "").strip()
        candidate = bearer_token or header_token
        if not candidate or not hmac.compare_digest(candidate, configured_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: valid AETHER_API_TOKEN required"},
            )
    return await call_next(request)

# Include API router
app.include_router(router)


# WebSocket endpoint
@app.websocket("/v1/repos/{repo_id}/events")
async def repo_events_ws(websocket: WebSocket, repo_id: str):
    """WebSocket endpoint for repository events."""
    await websocket_endpoint(websocket, repo_id=repo_id)


@app.websocket("/v1/events")
async def global_events_ws(websocket: WebSocket):
    """WebSocket endpoint for all events."""
    await websocket_endpoint(websocket, repo_id=None)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "service": "Project Aether",
        "version": "0.1.0",
        "description": "Agent-Native Version Control System",
        "server_profile": _normalized_server_profile(),
        "docs": "/docs",
        "health": "/v1/health",
    }


def run(host: str | None = None, port: int = 8100, reload: bool = False):
    """Run the Aether server (binds to 127.0.0.1 by default)."""
    resolved_host = _resolve_server_host(host)
    uvicorn.run(
        "apps.aether_server.main:app",
        host=resolved_host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    run()
