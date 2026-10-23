"""
Autonomous promotion worker process for Aether server.

Runs promotion cycles in a separate process to avoid blocking the API event loop.
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
from packages.aether.services.gate_runner import GateRunner
from packages.aether.services.proposal_service import ProposalService
from packages.aether.services.promotion_engine import PromotionEngine
from packages.aether.services.repository_service import RepositoryService
from packages.aether.services.snapshot_service import SnapshotService

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
class AutoPromotionConfig:
    """Configuration for autonomous promotion worker."""

    enabled: bool = False
    interval_seconds: int = 15
    repo_timeout_seconds: int = 30
    batch_size: int = 10
    non_conflicting_batches: int = 4
    max_conflict_sets: int = 1
    parallel_repos: int = 2
    shutdown_grace_seconds: int = 8

    @classmethod
    def from_env(cls) -> "AutoPromotionConfig":
        """Load worker config from environment variables."""
        return cls(
            enabled=_parse_bool_env("AETHER_AUTOPROMOTE_ENABLED", False),
            interval_seconds=_parse_int_env("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", 15, 1),
            repo_timeout_seconds=_parse_int_env("AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS", 30, 1),
            batch_size=_parse_int_env("AETHER_AUTOPROMOTE_BATCH_SIZE", 10, 1),
            non_conflicting_batches=_parse_int_env("AETHER_AUTOPROMOTE_NON_CONFLICTING_BATCHES", 4, 1),
            max_conflict_sets=_parse_int_env("AETHER_AUTOPROMOTE_MAX_CONFLICT_SETS", 1, 0),
            parallel_repos=_parse_int_env("AETHER_AUTOPROMOTE_PARALLEL_REPOS", 2, 1),
            shutdown_grace_seconds=_parse_int_env("AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS", 8, 1),
        )


@dataclass
class WorkerHandle:
    """Handle used by the API process to control a worker process."""

    process: multiprocessing.Process
    stop_event: multiprocessing.synchronize.Event
    config: AutoPromotionConfig


def _build_promotion_engine(session: Session, storage_path: Path) -> tuple[RepositoryService, PromotionEngine]:
    """Build fresh service graph for a worker cycle."""
    event_bus = EventBus()
    repo_service = RepositoryService(session, storage_path)
    proposal_service = ProposalService(session, event_bus, repo_service)
    snapshot_service = SnapshotService(session, event_bus, storage_path)
    gate_runner = GateRunner(session, event_bus)
    promotion_engine = PromotionEngine(
        session,
        event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        storage_path,
    )
    return repo_service, promotion_engine


def _list_active_repo_ids(session: Session) -> list[str]:
    """Return repo ids that currently have promotable work."""
    from packages.aether.models.entities import ProposalContention
    from sqlmodel import exists
    deferred_subq = select(ProposalContention.proposal_id).where(
        ProposalContention.proposal_id == Proposal.id,
        ProposalContention.defer_evaluation == True,
    )
    statement = (
        select(Proposal.repo_id)
        .where(
            Proposal.status.in_([ProposalStatus.PENDING, ProposalStatus.EVALUATING]),
            ~exists(deferred_subq)
        )
        .distinct()
    )
    return list(session.exec(statement))


async def _run_cycle(
    engine,
    storage_path: Path,
    config: AutoPromotionConfig,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """
    Run a single autonomous promotion cycle.

    Each cycle recreates session/services to avoid sticky state across iterations.
    """
    with Session(engine) as session:
        total_repositories = session.exec(select(func.count()).select_from(Repository)).one()
        repo_ids = _list_active_repo_ids(session)
        logger.debug(
            "autopromote_cycle_started",
            repositories=total_repositories,
            active_repositories=len(repo_ids),
        )

    if not repo_ids:
        return

    semaphore = asyncio.Semaphore(max(1, config.parallel_repos))

    async def _process_repo(repo_id: str) -> None:
        if stop_event.is_set():
            return

        async with semaphore:
            if stop_event.is_set():
                return
            try:
                with Session(engine) as repo_session:
                    repo_service, promotion_engine = _build_promotion_engine(repo_session, storage_path)
                    document_store = repo_service.get_document_store(repo_id)
                    if not document_store:
                        return

                    promotions = await asyncio.wait_for(
                        promotion_engine.process_pending_proposals(
                            repo_id,
                            document_store,
                            batch_size=config.batch_size,
                            proposal_scan_limit=max(
                                config.batch_size * config.non_conflicting_batches * 8,
                                200,
                            ),
                            max_conflict_sets=config.max_conflict_sets,
                            max_non_conflicting_batches=config.non_conflicting_batches,
                            prioritize_non_conflicting=True,
                        ),
                        timeout=config.repo_timeout_seconds,
                    )
                    if promotions:
                        logger.info(
                            "autopromote_repo_promoted",
                            repo_id=repo_id,
                            count=len(promotions),
                        )
            except TimeoutError:
                logger.error(
                    "autopromote_repo_timeout",
                    repo_id=repo_id,
                    timeout_seconds=config.repo_timeout_seconds,
                )
            except Exception as exc:
                logger.error(
                    "autopromote_repo_failed",
                    repo_id=repo_id,
                    error=str(exc),
                )

    await asyncio.gather(*[_process_repo(repo_id) for repo_id in repo_ids])


def _worker_main(
    database_url: str,
    storage_path: str,
    config: AutoPromotionConfig,
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
        "autopromote_worker_started",
        interval_seconds=config.interval_seconds,
        repo_timeout_seconds=config.repo_timeout_seconds,
        batch_size=config.batch_size,
        non_conflicting_batches=config.non_conflicting_batches,
        max_conflict_sets=config.max_conflict_sets,
        parallel_repos=config.parallel_repos,
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
            logger.error("autopromote_cycle_failed", error=str(exc))

        if stop_event.wait(config.interval_seconds):
            break

    engine.dispose()
    logger.info("autopromote_worker_stopped")


def start_worker(
    *,
    database_url: str,
    storage_path: Path,
    config: AutoPromotionConfig,
) -> Optional[WorkerHandle]:
    """Start autonomous promotion worker if enabled by config."""
    if not config.enabled:
        logger.info("autopromote_disabled")
        return None

    ctx = multiprocessing.get_context("spawn")
    stop_event = ctx.Event()
    process = ctx.Process(
        target=_worker_main,
        args=(database_url, str(storage_path), config, stop_event),
        name="aether-autopromote-worker",
        daemon=False,
    )
    process.start()

    logger.info("autopromote_worker_process_started", pid=process.pid)
    return WorkerHandle(process=process, stop_event=stop_event, config=config)


def stop_worker(handle: Optional[WorkerHandle]) -> None:
    """Stop autonomous promotion worker and force terminate if it does not exit in time."""
    if handle is None:
        return

    if not handle.process.is_alive():
        return

    handle.stop_event.set()
    handle.process.join(handle.config.shutdown_grace_seconds)

    if handle.process.is_alive():
        logger.warning("autopromote_worker_forced_terminate", pid=handle.process.pid)
        handle.process.terminate()
        handle.process.join(2)
