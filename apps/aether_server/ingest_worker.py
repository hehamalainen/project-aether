"""
Asynchronous ingest materialization worker for Aether server.

Moves INGESTED -> PENDING materialization out of promotion iterations.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import func
from sqlmodel import Session, create_engine, select, text

from packages.aether.models.entities import Proposal, ProposalStatus, Repository
from packages.aether.services.event_bus import EventBus
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.repository_service import RepositoryService

logger = structlog.get_logger()


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(minimum, parsed)


@dataclass(frozen=True)
class IngestMaterializationConfig:
    """Configuration for ingest materialization worker."""

    enabled: bool = True
    interval_seconds: int = 1
    repo_timeout_seconds: int = 20
    batch_size: int = 500
    shutdown_grace_seconds: int = 8

    @classmethod
    def from_env(cls) -> "IngestMaterializationConfig":
        """Load ingest worker config from environment variables."""
        return cls(
            enabled=_parse_bool_env("AETHER_MATERIALIZE_ENABLED", True),
            interval_seconds=_parse_int_env("AETHER_MATERIALIZE_INTERVAL_SECONDS", 1, 1),
            repo_timeout_seconds=_parse_int_env("AETHER_MATERIALIZE_REPO_TIMEOUT_SECONDS", 20, 1),
            batch_size=_parse_int_env("AETHER_MATERIALIZE_BATCH_SIZE", 500, 1),
            shutdown_grace_seconds=_parse_int_env("AETHER_MATERIALIZE_SHUTDOWN_GRACE_SECONDS", 8, 1),
        )


@dataclass
class WorkerHandle:
    """Handle used by the API process to control a worker process."""

    process: multiprocessing.Process
    stop_event: multiprocessing.synchronize.Event
    config: IngestMaterializationConfig


def _build_services(session: Session, storage_path: Path) -> tuple[RepositoryService, ProposalService]:
    """Build a fresh service graph for one ingest cycle."""
    event_bus = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(
        session,
        event_bus,
        repo_service,
        defer_conflict_detection=True,
    )
    return repo_service, proposal_service


def _list_ingest_active_repo_ids(session: Session) -> list[str]:
    """Return repo ids that currently have INGESTED proposals to materialize."""
    statement = (
        select(Proposal.repo_id)
        .where(Proposal.status == ProposalStatus.INGESTED)
        .distinct()
    )
    return list(session.exec(statement))


async def _run_cycle(
    engine,
    storage_path: Path,
    config: IngestMaterializationConfig,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """Run one ingest materialization cycle across all repositories."""
    with Session(engine) as session:
        repo_service, proposal_service = _build_services(session, storage_path)
        total_repositories = session.exec(select(func.count()).select_from(Repository)).one()
        active_repo_ids = _list_ingest_active_repo_ids(session)
        logger.debug(
            "materialize_cycle_started",
            repositories=total_repositories,
            active_repositories=len(active_repo_ids),
        )

        for repo_id in active_repo_ids:
            if stop_event.is_set():
                logger.debug("materialize_cycle_interrupted", repo_id=repo_id)
                return

            try:
                materialized = await asyncio.wait_for(
                    proposal_service.materialize_ingested_queue(
                        repo_id,
                        limit=config.batch_size,
                    ),
                    timeout=config.repo_timeout_seconds,
                )
                if materialized:
                    logger.info(
                        "materialize_repo_completed",
                        repo_id=repo_id,
                        count=materialized,
                    )
            except TimeoutError:
                logger.error(
                    "materialize_repo_timeout",
                    repo_id=repo_id,
                    timeout_seconds=config.repo_timeout_seconds,
                )
            except Exception as exc:
                logger.error(
                    "materialize_repo_failed",
                    repo_id=repo_id,
                    error=str(exc),
                )


def _worker_main(
    database_url: str,
    storage_path: str,
    config: IngestMaterializationConfig,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """Worker process loop."""
    worker_storage = Path(storage_path)
    worker_storage.mkdir(parents=True, exist_ok=True)
    cargs = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    engine = create_engine(
        database_url,
        echo=False,
        connect_args=cargs,
    )
    with engine.connect() as conn:
        if database_url.startswith("sqlite"):
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
            conn.execute(text("PRAGMA synchronous=NORMAL;"))

    logger.info(
        "materialize_worker_started",
        interval_seconds=config.interval_seconds,
        repo_timeout_seconds=config.repo_timeout_seconds,
        batch_size=config.batch_size,
    )

    while not stop_event.is_set():
        try:
            asyncio.run(
                _run_cycle(
                    engine=engine,
                    storage_path=worker_storage,
                    config=config,
                    stop_event=stop_event,
                )
            )
        except Exception as exc:
            logger.error("materialize_cycle_failed", error=str(exc))

        if stop_event.wait(config.interval_seconds):
            break

    engine.dispose()
    logger.info("materialize_worker_stopped")


def start_worker(
    *,
    database_url: str,
    storage_path: Path,
    config: IngestMaterializationConfig,
) -> Optional[WorkerHandle]:
    """Start ingest materialization worker if enabled by config."""
    if not config.enabled:
        logger.info("materialize_worker_disabled")
        return None

    ctx = multiprocessing.get_context("spawn")
    stop_event = ctx.Event()
    process = ctx.Process(
        target=_worker_main,
        args=(database_url, str(storage_path), config, stop_event),
        name="aether-materialize-worker",
        daemon=False,
    )
    process.start()

    logger.info("materialize_worker_process_started", pid=process.pid)
    return WorkerHandle(process=process, stop_event=stop_event, config=config)


def stop_worker(handle: Optional[WorkerHandle]) -> None:
    """Stop ingest materialization worker and force terminate if needed."""
    if handle is None:
        return

    if not handle.process.is_alive():
        return

    handle.stop_event.set()
    handle.process.join(handle.config.shutdown_grace_seconds)

    if handle.process.is_alive():
        logger.warning("materialize_worker_forced_terminate", pid=handle.process.pid)
        handle.process.terminate()
        handle.process.join(2)
