"""
Aether API routes.

Implements the API contract from the PRD:
- POST /v1/repos - Create repo
- POST /v1/repos/{repo_id}/proposals - Submit proposal
- GET /v1/repos/{repo_id}/head - Get canonical head
- POST /v1/repos/{repo_id}/snapshots - Create snapshot
- GET /v1/repos/{repo_id}/snapshots/{snapshot_id}/download - Download snapshot
- POST /v1/repos/{repo_id}/promotions - Request promotion
"""

from datetime import datetime
from typing import Optional
from pathlib import Path
import os
import random
import time

import subprocess
import hashlib

_AETHER_ROOT = Path(__file__).resolve().parents[3]

try:
    BOOT_BRANCH = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=_AETHER_ROOT, stderr=subprocess.DEVNULL).decode("utf-8").strip()
except Exception:
    BOOT_BRANCH = "unknown"

try:
    BOOT_COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_AETHER_ROOT, stderr=subprocess.DEVNULL).decode("utf-8").strip()
except Exception:
    BOOT_COMMIT = "unknown"

BOOT_SHA = BOOT_COMMIT

try:
    status_output = subprocess.check_output(["git", "status", "--porcelain"], cwd=_AETHER_ROOT, stderr=subprocess.DEVNULL).decode("utf-8").strip()
    BOOT_DIRTY = len(status_output) > 0
except Exception:
    BOOT_DIRTY = False

try:
    diff_output = subprocess.check_output(["git", "diff"], cwd=_AETHER_ROOT, stderr=subprocess.DEVNULL)
    BOOT_DIFF_FINGERPRINT = hashlib.sha256(diff_output).hexdigest() if diff_output else None
except Exception:
    BOOT_DIFF_FINGERPRINT = None

from fastapi import APIRouter, Depends, HTTPException, Response, BackgroundTasks
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..models.entities import (
    Repository,
    Proposal,
    ProposalStatus,
    ProposalCreate,
    ProposalResponse,
    SnapshotCreate,
    SnapshotResponse,
    PromotionRequest,
    PromotionResponse,
    HeadResponse,
    RepoConfig,
    GateConfig,
    generate_id,
)
from ..services.repository_service import RepositoryService
from ..services.proposal_service import ProposalService
from ..services.snapshot_service import SnapshotService
from ..services.gate_runner import GateRunner, is_safe_simulation_command
from ..services.promotion_engine import PromotionEngine
import subprocess

REPO_ROOT = str(Path(__file__).resolve().parents[3])

BOOT_BRANCH = "unknown"
BOOT_SHA = "unknown"

try:
    BOOT_SHA = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stderr=subprocess.DEVNULL
    ).strip()
    BOOT_BRANCH = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        stderr=subprocess.DEVNULL
    ).strip()
except Exception:
    pass

router = APIRouter(prefix="/v1", tags=["aether"])


# ============================================================================
# Request/Response models
# ============================================================================

class RepoCreateRequest(BaseModel):
    """Request to create a repository."""
    name: str
    description: Optional[str] = None
    config: Optional[RepoConfig] = None


class RepoResponse(BaseModel):
    """Repository response."""
    id: str
    name: str
    description: Optional[str]
    head_id: Optional[str]
    head_sequence: int
    created_at: datetime
    updated_at: datetime


class ProposalListResponse(BaseModel):
    """List of proposals."""
    proposals: list[dict]
    total: int


class ConflictSetResponse(BaseModel):
    """Conflict set information."""
    id: str
    repo_id: str
    path: str
    proposal_ids: list[str]
    active_choice_id: Optional[str]
    resolved: bool


class GateResultResponse(BaseModel):
    """Gate result information."""
    id: str
    gate_name: str
    status: str
    exit_code: Optional[int]
    duration_ms: Optional[int]
    tests_passed: Optional[int]
    tests_failed: Optional[int]


class PromotionListResponse(BaseModel):
    """List of promotions."""
    promotions: list[dict]
    total: int


class CohortEvaluateRequest(BaseModel):
    """Request to evaluate a cohort of proposals."""
    proposal_ids: Optional[list[str]] = None
    contention_group_id: Optional[str] = None


class CohortEvaluateResponse(BaseModel):
    """Response from evaluating a cohort."""
    promotion_id: Optional[str] = None
    success: bool
    proposal_outcomes: list[dict]


# ============================================================================
# Dependency injection
# ============================================================================

# These will be set by the server at startup
_engine = None
_event_bus = None
_storage_path = None
_rate_limit_hold_until_by_repo: dict[str, float] = {}


def _parse_bool_env(name: str, default: bool) -> bool:
    """Parse boolean environment flags for API runtime behavior."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int_env(name: str, default: int, minimum: int = 0) -> int:
    """Parse integer environment values used for runtime guardrails."""
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, default)
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, value)


def _parse_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """Parse float environment values used for runtime guardrails."""
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, default)
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(minimum, value)


def set_services(engine, event_bus, storage_path):
    """Set global resources for dependency injection."""
    global _engine, _event_bus, _storage_path, _rate_limit_hold_until_by_repo
    _engine = engine
    _event_bus = event_bus
    _storage_path = storage_path
    _rate_limit_hold_until_by_repo = {}


def get_session():
    """Get fresh database session per request."""
    with Session(_engine) as session:
        yield session


def get_repo_service(session: Session = Depends(get_session)):
    """Get repository service."""
    return RepositoryService(session, _storage_path)


def get_proposal_service(session: Session = Depends(get_session)):
    """Get proposal service."""
    repo_service = get_repo_service(session)
    return ProposalService(
        session,
        _event_bus,
        repo_service,
        submit_in_worker=True,
        session_factory=lambda: Session(_engine),
        worker_threads=4,
        defer_conflict_detection=_parse_bool_env("AETHER_DEFER_CONFLICT_DETECTION", True),
    )


def get_snapshot_service(session: Session = Depends(get_session)):
    """Get snapshot service."""
    return SnapshotService(session, _event_bus, _storage_path)


def get_gate_runner(session: Session = Depends(get_session)):
    """Get gate runner."""
    return GateRunner(session, _event_bus)


def get_promotion_engine(session: Session = Depends(get_session)):
    """Get promotion engine."""
    repo_service = get_repo_service(session)
    proposal_service = ProposalService(session, _event_bus, repo_service)
    snapshot_service = SnapshotService(session, _event_bus, _storage_path)
    gate_runner = GateRunner(session, _event_bus)
    return PromotionEngine(
        session,
        _event_bus,
        proposal_service,
        snapshot_service,
        gate_runner,
        _storage_path,
    )


def get_document_store(repo_id: str, repo_service: RepositoryService = Depends(get_repo_service)):
    """Get document store for a repository."""
    return repo_service.get_document_store(repo_id)


# ============================================================================
# Repository endpoints
# ============================================================================

@router.post("/repos", response_model=RepoResponse)
async def create_repo(
    request: RepoCreateRequest,
    repo_service: RepositoryService = Depends(get_repo_service),
):
    """Create a new repository."""
    if request.config and request.config.gates:
        has_custom_gate = any(
            not is_safe_simulation_command(gate.command)
            for gate in request.config.gates
        )
        if has_custom_gate and not _parse_bool_env("AETHER_ALLOW_CUSTOM_GATE_COMMANDS", False):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Custom gate commands are disabled by default. "
                    "Set AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true in a trusted, isolated environment."
                ),
            )

    repo = repo_service.create_repository(
        name=request.name,
        description=request.description,
        config=request.config,
    )

    return RepoResponse(
        id=repo.id,
        name=repo.name,
        description=repo.description,
        head_id=repo.head_id,
        head_sequence=repo.head_sequence,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.get("/repos", response_model=list[RepoResponse])
async def list_repos(repo_service: RepositoryService = Depends(get_repo_service)):
    """List all repositories."""
    repos = repo_service.list_repositories()

    return [
        RepoResponse(
            id=r.id,
            name=r.name,
            description=r.description,
            head_id=r.head_id,
            head_sequence=r.head_sequence,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in repos
    ]


@router.get("/repos/{repo_id}", response_model=RepoResponse)
async def get_repo(repo_id: str, repo_service: RepositoryService = Depends(get_repo_service)):
    """Get repository by ID."""
    repo = repo_service.get_repository(repo_id)

    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    return RepoResponse(
        id=repo.id,
        name=repo.name,
        description=repo.description,
        head_id=repo.head_id,
        head_sequence=repo.head_sequence,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.delete("/repos/{repo_id}")
async def delete_repo(repo_id: str, repo_service: RepositoryService = Depends(get_repo_service)):
    """Delete a repository."""

    if not repo_service.delete_repository(repo_id):
        raise HTTPException(status_code=404, detail="Repository not found")

    return {"status": "deleted", "repo_id": repo_id}


# ============================================================================
# Head endpoint
# ============================================================================

@router.get("/repos/{repo_id}/head", response_model=HeadResponse)
async def get_head(
    repo_id: str,
    repo_service: RepositoryService = Depends(get_repo_service),
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """Get canonical head for a repository."""
    repo = repo_service.get_repository(repo_id)

    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    head = repo_service.get_head(repo_id)

    if not head:
        # No head yet - return empty state
        return HeadResponse(
            head_id="",
            sequence=0,
            snapshot_id="",
            content_hash="",
            last_promotion_id=None,
            last_promotion_at=None,
        )

    snapshot = snapshot_service.get_snapshot(head.snapshot_id)

    return HeadResponse(
        head_id=head.id,
        sequence=head.sequence,
        snapshot_id=head.snapshot_id,
        content_hash=snapshot.content_hash if snapshot else "",
        last_promotion_id=head.promotion_id,
        last_promotion_at=head.created_at,
    )


# ============================================================================
# Proposal endpoints
# ============================================================================

@router.post("/repos/{repo_id}/proposals", response_model=ProposalResponse)
async def submit_proposal(
    repo_id: str,
    proposal: ProposalCreate,
    response: Response,
    repo_service: RepositoryService = Depends(get_repo_service),
    proposal_service: ProposalService = Depends(get_proposal_service),
):
    """Submit a new proposal."""
    route_started = time.perf_counter()
    repo = repo_service.get_repository(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    now = time.monotonic()
    hold_until = _rate_limit_hold_until_by_repo.get(repo_id, 0.0)
    if now < hold_until:
        retry_after = max(1, int(hold_until - now))
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Swarm intake cooling down after capacity breach. Yield and sync HEAD.",
                "throttle_reason": "cooldown_hold",
            },
            headers={"Retry-After": str(retry_after)},
        )
    if hold_until and now >= hold_until:
        _rate_limit_hold_until_by_repo.pop(repo_id, None)

    max_backlog = _parse_int_env("AETHER_MAX_COHERENCE_BACKLOG", 1000, minimum=1)
    resume_backlog = _parse_int_env(
        "AETHER_BACKLOG_RESUME_LEVEL",
        int(max_backlog * 0.8),
        minimum=0,
    )
    if resume_backlog >= max_backlog:
        resume_backlog = max_backlog - 1

    physics_window_seconds = _parse_int_env("AETHER_COHERENCE_WINDOW_SECONDS", 60, minimum=1)
    physics = ProposalService.get_system_physics_snapshot(
        proposal_service.session,
        window_seconds=physics_window_seconds,
    )
    lambda_tps = float(physics.get("lambda_tps", 0.0) or 0.0)
    mu_tps = float(physics.get("mu_tps", 0.0) or 0.0)
    active_backlog = int(physics.get("queue_depth", {}).get("active", 0))
    if active_backlog > max_backlog:
        drain_target = max(1, active_backlog - resume_backlog)
        estimated_clear_seconds = drain_target / max(mu_tps, 0.1)
        jitter_seconds = random.randint(1, 10)
        retry_after = int(estimated_clear_seconds) + jitter_seconds
        retry_after_max = _parse_int_env("AETHER_RETRY_AFTER_MAX_SECONDS", 300, minimum=1)
        retry_after = max(1, min(retry_after, retry_after_max))

        raise HTTPException(
            status_code=429,
            detail={
                "error": "Swarm integration capacity exceeded. Canonicalization in progress. Yield and sync HEAD.",
                "throttle_reason": "backlog_limit_exceeded",
                "queue_depth_active": active_backlog,
                "queue_depth_limit": max_backlog,
                "queue_depth_resume": resume_backlog,
                "mu_tps": mu_tps,
                "lambda_tps": lambda_tps,
                "window_seconds": physics_window_seconds,
            },
            headers={"Retry-After": str(retry_after)},
        )

    max_path_active = _parse_int_env("AETHER_MAX_PATH_ACTIVE_PROPOSALS", 0, minimum=0)
    if max_path_active > 0 and proposal.affected_paths:
        path_counts = ProposalService.get_active_path_counts(
            proposal_service.session,
            repo_id=repo_id,
            paths=proposal.affected_paths,
        )
        hot_paths = {
            path: count
            for path, count in path_counts.items()
            if count >= max_path_active
        }
        if hot_paths:
            hottest_path = max(hot_paths, key=hot_paths.get)
            hottest_count = int(hot_paths[hottest_path])
            retry_after_max = _parse_int_env("AETHER_RETRY_AFTER_MAX_SECONDS", 300, minimum=1)
            retry_after = min(
                retry_after_max,
                max(1, int((hottest_count - max_path_active + 1) / max(mu_tps, 0.1))),
            )
            retry_after = max(1, retry_after + random.randint(1, 3))
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Path hotspot detected. Yield and sync HEAD before retrying this region.",
                    "throttle_reason": "path_hotspot_exceeded",
                    "max_path_active_proposals": max_path_active,
                    "hot_paths": hot_paths,
                    "hottest_path": hottest_path,
                    "hottest_count": hottest_count,
                },
                headers={"Retry-After": str(retry_after)},
            )

    route_ratio_limit = _parse_float_env(
        "AETHER_ROUTE_ACCEPTED_TO_PROMOTION_RATIO_LIMIT",
        -1.0,
        minimum=-1.0,
    )
    ratio_limit = (
        _parse_float_env("AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT", 0.0, minimum=0.0)
        if route_ratio_limit < 0.0
        else route_ratio_limit
    )
    ratio_min_mu_tps = _parse_float_env("AETHER_RATIO_CONTROL_MIN_MU_TPS", 0.2, minimum=0.0)
    ratio_min_mu_events = _parse_int_env("AETHER_RATIO_CONTROL_MIN_MU_EVENTS", 5, minimum=1)
    mu_events = int(physics.get("events", {}).get("status_promoted", 0)) + int(
        physics.get("events", {}).get("status_rejected", 0)
    ) + int(physics.get("events", {}).get("status_superseded", 0))
    ratio_limit = min(ratio_limit, 1.0) if ratio_limit > 0 else 0.0

    if (
        ratio_limit > 0.0
        and mu_tps >= ratio_min_mu_tps
        and mu_events >= ratio_min_mu_events
    ):
        lambda_limit = mu_tps * ratio_limit
        if lambda_tps > lambda_limit:
            overload_ratio = lambda_tps / max(lambda_limit, 1e-9)
            retry_after_max = _parse_int_env("AETHER_RETRY_AFTER_MAX_SECONDS", 300, minimum=1)
            base_retry = max(1, int(max(0.0, overload_ratio - 1.0) * 6.0))
            retry_after = min(retry_after_max, base_retry + random.randint(1, 3))
            _rate_limit_hold_until_by_repo[repo_id] = max(
                _rate_limit_hold_until_by_repo.get(repo_id, 0.0),
                time.monotonic() + retry_after,
            )

            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Swarm submit rate exceeds promotion capacity window. Yield and sync HEAD.",
                    "throttle_reason": "accepted_rate_ratio_exceeded",
                    "lambda_tps": lambda_tps,
                    "mu_tps": mu_tps,
                    "ratio_limit": ratio_limit,
                    "lambda_limit_tps": lambda_limit,
                    "mu_events": mu_events,
                    "window_seconds": physics_window_seconds,
                },
                headers={"Retry-After": str(retry_after)},
            )

    try:
        proposal_response = await proposal_service.submit_proposal(repo_id, proposal)
        submit_timings = proposal_service.get_last_submit_timings()
        response.headers["X-Aether-Route-Handler-Ms"] = (
            f"{(time.perf_counter() - route_started) * 1000.0:.3f}"
        )
        if submit_timings:
            response.headers["X-Aether-Submit-Service-Ms"] = (
                f"{submit_timings.get('service_total_ms', 0.0):.3f}"
            )
            response.headers["X-Aether-Submit-DB-Write-Ms"] = (
                f"{submit_timings.get('db_write_ms', 0.0):.3f}"
            )
            response.headers["X-Aether-Submit-Conflict-Enqueue-Ms"] = (
                f"{submit_timings.get('conflict_or_enqueue_ms', 0.0):.3f}"
            )
        return proposal_response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_id}/proposals", response_model=ProposalListResponse)
async def list_proposals(
    repo_id: str,
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 100,
    proposal_service: ProposalService = Depends(get_proposal_service),
):
    """List proposals for a repository."""

    status_enum = ProposalStatus(status) if status else None
    proposals = proposal_service.list_proposals(
        repo_id,
        status=status_enum,
        agent_id=agent_id,
        limit=limit,
    )

    return ProposalListResponse(
        proposals=[
            {
                "id": p.id,
                "agent_id": p.agent_id,
                "task_id": p.task_id,
                "source": p.source,
                "source_run_id": p.source_run_id,
                "source_task_id": p.source_task_id,
                "source_agent_id": p.source_agent_id,
                "source_attempt_id": p.source_attempt_id,
                "intent_type": p.intent_type,
                "status": p.status.value,
                "confidence_score": p.confidence_score,
                "change_type": p.change_type,
                "change_content": p.change_content,
                "conflict_set_id": p.conflict_set_id,
                "submitted_at": p.submitted_at.isoformat(),
            }
            for p in proposals
        ],
        total=len(proposals),
    )


@router.get("/repos/{repo_id}/proposals/{proposal_id}")
async def get_proposal(
    repo_id: str,
    proposal_id: str,
    proposal_service: ProposalService = Depends(get_proposal_service),
):
    """Get proposal details."""
    proposal = proposal_service.get_proposal(proposal_id)

    if not proposal or proposal.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Proposal not found")

    return {
        "id": proposal.id,
        "repo_id": proposal.repo_id,
        "agent_id": proposal.agent_id,
        "task_id": proposal.task_id,
        "source": proposal.source,
        "source_run_id": proposal.source_run_id,
        "source_task_id": proposal.source_task_id,
        "source_agent_id": proposal.source_agent_id,
        "source_attempt_id": proposal.source_attempt_id,
        "intent_type": proposal.intent_type,
        "status": proposal.status.value,
        "base_head_id": proposal.base_head_id,
        "base_head_sequence": proposal.base_head_sequence,
        "change_type": proposal.change_type,
        "change_content": proposal.change_content,
        "affected_paths": proposal.affected_paths,
        "confidence_score": proposal.confidence_score,
        "model_id": proposal.model_id,
        "rationale": proposal.rationale,
        "conflict_set_id": proposal.conflict_set_id,
        "submitted_at": proposal.submitted_at.isoformat(),
        "evaluated_at": proposal.evaluated_at.isoformat() if proposal.evaluated_at else None,
        "promoted_at": proposal.promoted_at.isoformat() if proposal.promoted_at else None,
    }


@router.get("/repos/{repo_id}/proposals/{proposal_id}/provenance")
async def get_proposal_provenance(
    repo_id: str,
    proposal_id: str,
    proposal_service: ProposalService = Depends(get_proposal_service),
):
    """Get proposal status provenance (reasons, event type, timestamp)."""
    proposal = proposal_service.get_proposal(proposal_id)
    if not proposal or proposal.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Proposal not found")
        
    provenance = proposal_service.get_proposal_provenance(proposal_id)
    if not provenance:
        return {
            "proposal_id": proposal_id,
            "event_type": None,
            "event_reason": None,
            "created_at": None,
            "contention_group_id": None,
            "contention_policy": None,
            "winner_proposal_id": None,
            "target_paths": None,
            "synthesis_engine": None,
        }
        
    return {
        "proposal_id": provenance.proposal_id,
        "event_type": provenance.event_type,
        "event_reason": provenance.event_reason,
        "created_at": provenance.created_at.isoformat(),
        "contention_group_id": provenance.contention_group_id,
        "contention_policy": provenance.contention_policy,
        "winner_proposal_id": provenance.winner_proposal_id,
        "target_paths": provenance.target_paths,
        "synthesis_engine": provenance.synthesis_engine,
    }


# ============================================================================
# Snapshot endpoints
# ============================================================================

@router.post("/repos/{repo_id}/snapshots", response_model=SnapshotResponse)
async def create_snapshot(
    repo_id: str,
    request: SnapshotCreate,
    repo_service: RepositoryService = Depends(get_repo_service),
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """Create a snapshot."""

    repo = repo_service.get_repository(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    doc_store = repo_service.get_document_store(repo_id)
    if not doc_store:
        raise HTTPException(status_code=500, detail="Document store not available")

    snapshot = await snapshot_service.create_snapshot(
        repo_id,
        doc_store,
        included_proposals=request.proposal_ids or [],
        created_by="api",
    )

    return SnapshotResponse(
        snapshot_id=snapshot.id,
        content_hash=snapshot.content_hash,
        file_count=snapshot.file_count,
        total_bytes=snapshot.total_bytes,
        download_url=f"/v1/repos/{repo_id}/snapshots/{snapshot.id}/download",
    )


@router.get("/repos/{repo_id}/snapshots")
async def list_snapshots(
    repo_id: str,
    limit: int = 50,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """List snapshots for a repository."""
    snapshots = snapshot_service.list_snapshots(repo_id, limit=limit)

    return {
        "snapshots": [
            {
                "id": s.id,
                "content_hash": s.content_hash,
                "file_count": s.file_count,
                "total_bytes": s.total_bytes,
                "created_at": s.created_at.isoformat(),
            }
            for s in snapshots
        ],
        "total": len(snapshots),
    }


@router.get("/repos/{repo_id}/snapshots/{snapshot_id}")
async def get_snapshot(
    repo_id: str,
    snapshot_id: str,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """Get snapshot details."""
    snapshot = snapshot_service.get_snapshot(snapshot_id)

    if not snapshot or snapshot.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    files = snapshot_service.list_files(snapshot_id)

    return {
        "id": snapshot.id,
        "repo_id": snapshot.repo_id,
        "content_hash": snapshot.content_hash,
        "file_count": snapshot.file_count,
        "total_bytes": snapshot.total_bytes,
        "files": files,
        "created_at": snapshot.created_at.isoformat(),
    }


@router.get("/repos/{repo_id}/snapshots/{snapshot_id}/download")
async def download_snapshot(
    repo_id: str,
    snapshot_id: str,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """Download snapshot as tarball."""
    snapshot = snapshot_service.get_snapshot(snapshot_id)

    if not snapshot or snapshot.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    tarball = await snapshot_service.create_tarball(snapshot_id)
    if not tarball:
        raise HTTPException(status_code=500, detail="Failed to create tarball")

    return Response(
        content=tarball,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f"attachment; filename={snapshot_id}.tar.gz"
        },
    )


@router.get("/repos/{repo_id}/snapshots/{snapshot_id}/files/{file_path:path}")
async def get_snapshot_file(
    repo_id: str,
    snapshot_id: str,
    file_path: str,
    snapshot_service: SnapshotService = Depends(get_snapshot_service),
):
    """Get content of a file from a snapshot."""
    content = snapshot_service.get_file_content(snapshot_id, file_path)

    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    return {"path": file_path, "content": content}


# ============================================================================
# Promotion endpoints
# ============================================================================

@router.post("/repos/{repo_id}/promotions", response_model=PromotionResponse)
async def request_promotion(
    repo_id: str,
    request: PromotionRequest,
    background_tasks: BackgroundTasks,
    repo_service: RepositoryService = Depends(get_repo_service),
    proposal_service: ProposalService = Depends(get_proposal_service),
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """Request promotion of proposals."""

    repo = repo_service.get_repository(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    doc_store = repo_service.get_document_store(repo_id)
    if not doc_store:
        raise HTTPException(status_code=500, detail="Document store not available")

    # Get proposals
    proposals = proposal_service.get_proposals_by_ids(request.proposal_ids, repo_id=repo_id)
    if not proposals:
        raise HTTPException(status_code=404, detail="No proposals found")
    if len(proposals) != len(request.proposal_ids):
        raise HTTPException(status_code=400, detail="One or more proposals were not found in this repository")

    try:
        # Evaluate and promote
        if len(proposals) == 1:
            candidate = await promotion_engine.evaluate_proposal(proposals[0], doc_store)
        else:
            candidate = await promotion_engine.evaluate_batch(proposals, doc_store)

        if candidate.score <= 0:
            for p in proposals:
                proposal_service.session.refresh(p)

            promoted_in_degradation = [
                p for p in proposals if p.status == ProposalStatus.PROMOTED
            ]
            unterminated_Statuses = {
                ProposalStatus.INGESTED,
                ProposalStatus.PENDING,
                ProposalStatus.EVALUATING,
            }
            to_reject = [
                (p.id, ProposalStatus.REJECTED, "required_gates_failed")
                for p in proposals
                if p.status in unterminated_Statuses
            ]
            if to_reject:
                await proposal_service.update_status_many(to_reject)

            if promoted_in_degradation:
                current_head = repo_service.get_head(repo_id)
                promoted_ids = [p.id for p in promoted_in_degradation]
                promoted_id_set = set(promoted_ids)
                excluded_ids = [
                    pid for pid in request.proposal_ids if pid not in promoted_id_set
                ]
                latest_promotion_id = (
                    (current_head.promotion_id if current_head else "")
                    or ""
                )
                return PromotionResponse(
                    promotion_id=latest_promotion_id,
                    success=True,
                    new_head_id=current_head.id if current_head else "",
                    new_head_sequence=current_head.sequence if current_head else 0,
                    included_proposals=promoted_ids,
                    excluded_proposals=excluded_ids,
                    evidence=candidate.evidence,
                    final_score=max(p.confidence_score for p in promoted_in_degradation),
                    message="Partial promotion successful via sequential degradation",
                )

            return PromotionResponse(
                promotion_id="",
                success=False,
                new_head_id="",
                new_head_sequence=0,
                included_proposals=[],
                excluded_proposals=request.proposal_ids,
                evidence=candidate.evidence,
                final_score=candidate.score,
                message="Promotion rejected: gates did not pass",
            )

        promotion = await promotion_engine.promote(
            repo_id,
            candidate,
            doc_store,
            triggered_by="api",
        )

        return PromotionResponse(
            promotion_id=promotion.id,
            success=True,
            new_head_id=promotion.to_head_id,
            new_head_sequence=promotion.to_head_sequence,
            included_proposals=[p.id for p in candidate.proposals],
            excluded_proposals=[],
            evidence=candidate.evidence,
            final_score=candidate.score,
            message="Promotion successful",
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/repos/{repo_id}/cohort/evaluate", response_model=CohortEvaluateResponse)
async def evaluate_cohort(
    repo_id: str,
    request: CohortEvaluateRequest,
    repo_service: RepositoryService = Depends(get_repo_service),
    proposal_service: ProposalService = Depends(get_proposal_service),
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """Evaluate and promote a cohort of deferred proposals."""
    if (request.proposal_ids is None) == (request.contention_group_id is None):
        raise HTTPException(
            status_code=400,
            detail="Exactly one of proposal_ids or contention_group_id must be provided"
        )

    repo = repo_service.get_repository(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    doc_store = repo_service.get_document_store(repo_id)
    if not doc_store:
        raise HTTPException(status_code=500, detail="Document store not available")

    proposal_ids = request.proposal_ids
    if request.contention_group_id:
        from packages.aether.models.entities import ProposalContention
        from sqlmodel import select
        cont_stmt = select(ProposalContention).where(
            ProposalContention.contention_group_id == request.contention_group_id
        )
        contentions = proposal_service.session.exec(cont_stmt).all()
        proposal_ids = [c.proposal_id for c in contentions]

    if not proposal_ids:
        raise HTTPException(status_code=404, detail="No proposals found in cohort")

    try:
        res = await promotion_engine.evaluate_and_promote_cohort(
            repo_id=repo_id,
            proposal_ids=proposal_ids,
            document_store=doc_store,
        )
        return CohortEvaluateResponse(
            promotion_id=res.promotion.id if res.promotion else None,
            success=res.promotion is not None,
            proposal_outcomes=res.proposal_outcomes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_id}/promotions", response_model=PromotionListResponse)
async def list_promotions(
    repo_id: str,
    limit: int = 50,
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """List promotions for a repository."""
    promotions = promotion_engine.get_promotion_history(repo_id, limit=limit)

    from ..models.entities import AetherHead
    res_list = []
    for p in promotions:
        head = promotion_engine.session.get(AetherHead, p.to_head_id)
        res_list.append({
            "id": p.id,
            "from_sequence": p.from_head_sequence,
            "to_sequence": p.to_head_sequence,
            "final_score": p.final_score,
            "triggered_by": p.triggered_by,
            "created_at": p.created_at.isoformat(),
            "snapshot_id": head.snapshot_id if head else None,
        })

    return PromotionListResponse(
        promotions=res_list,
        total=len(promotions),
    )


@router.get("/repos/{repo_id}/promotions/{promotion_id}")
async def get_promotion(
    repo_id: str,
    promotion_id: str,
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """Get promotion details."""
    promotion = promotion_engine.get_promotion(promotion_id)

    if not promotion or promotion.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Promotion not found")

    return {
        "id": promotion.id,
        "repo_id": promotion.repo_id,
        "from_head_id": promotion.from_head_id,
        "from_head_sequence": promotion.from_head_sequence,
        "to_head_id": promotion.to_head_id,
        "to_head_sequence": promotion.to_head_sequence,
        "included_proposals": promotion.included_proposals,
        "excluded_proposals": promotion.excluded_proposals,
        "evidence": promotion.evidence,
        "policy_score": promotion.policy_score,
        "confidence_score": promotion.confidence_score,
        "final_score": promotion.final_score,
        "triggered_by": promotion.triggered_by,
        "created_at": promotion.created_at.isoformat(),
    }


@router.get("/repos/{repo_id}/promotions/{promotion_id}/verify")
async def verify_promotion(
    repo_id: str,
    promotion_id: str,
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """Verify deterministic provenance for a promotion."""
    promotion = promotion_engine.get_promotion(promotion_id)
    if not promotion or promotion.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Promotion not found")

    try:
        return promotion_engine.verify_promotion_provenance(promotion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ============================================================================
# Operation graph endpoints
# ============================================================================

@router.get("/repos/{repo_id}/operation-graph")
async def list_operation_graph(
    repo_id: str,
    limit: int = 100,
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """List persisted operation-graph nodes for a repository."""
    nodes = promotion_engine.list_operation_nodes(repo_id, limit=limit)
    return {
        "nodes": [
            {
                "id": node.id,
                "promotion_id": node.promotion_id,
                "parent_node_id": node.parent_node_id,
                "parent_head_id": node.parent_head_id,
                "parent_head_sequence": node.parent_head_sequence,
                "to_head_id": node.to_head_id,
                "to_head_sequence": node.to_head_sequence,
                "snapshot_id": node.snapshot_id,
                "operation_type": node.operation_type,
                "node_hash": node.node_hash,
                "payload_sha256": node.payload_sha256,
                "replay_from_hash": node.replay_from_hash,
                "replay_to_hash": node.replay_to_hash,
                "created_at": node.created_at.isoformat(),
            }
            for node in nodes
        ],
        "total": len(nodes),
    }


@router.get("/repos/{repo_id}/operation-graph/{operation_node_id}")
async def get_operation_graph_node(
    repo_id: str,
    operation_node_id: str,
    promotion_engine: PromotionEngine = Depends(get_promotion_engine),
):
    """Get details for one operation-graph node."""
    node = promotion_engine.get_operation_node(operation_node_id)
    if not node or node.repo_id != repo_id:
        raise HTTPException(status_code=404, detail="Operation graph node not found")

    return {
        "id": node.id,
        "repo_id": node.repo_id,
        "promotion_id": node.promotion_id,
        "parent_node_id": node.parent_node_id,
        "parent_head_id": node.parent_head_id,
        "parent_head_sequence": node.parent_head_sequence,
        "to_head_id": node.to_head_id,
        "to_head_sequence": node.to_head_sequence,
        "snapshot_id": node.snapshot_id,
        "operation_type": node.operation_type,
        "node_hash": node.node_hash,
        "payload_sha256": node.payload_sha256,
        "parent_ref": node.parent_ref,
        "replay_from_hash": node.replay_from_hash,
        "replay_to_hash": node.replay_to_hash,
        "replay_gate_vector": node.replay_gate_vector,
        "included_proposals": node.included_proposals,
        "excluded_proposals": node.excluded_proposals,
        "created_at": node.created_at.isoformat(),
    }


# ============================================================================
# Conflict set endpoints
# ============================================================================

@router.get("/repos/{repo_id}/conflicts")
async def list_conflict_sets(
    repo_id: str,
    resolved: Optional[bool] = None,
    proposal_service: ProposalService = Depends(get_proposal_service),
):
    """List conflict sets for a repository."""
    conflict_sets = proposal_service.list_conflict_sets(repo_id, resolved=resolved)

    return {
        "conflict_sets": [
            {
                "id": cs.id,
                "path": cs.path,
                "proposal_ids": cs.proposal_ids,
                "active_choice_id": cs.active_choice_id,
                "resolved": cs.resolved,
                "created_at": cs.created_at.isoformat(),
            }
            for cs in conflict_sets
        ],
        "total": len(conflict_sets),
    }


# ============================================================================
# Health check
# ============================================================================

@router.get("/system/physics")
async def system_physics(
    window_seconds: int = 60,
    session: Session = Depends(get_session),
):
    """Queue-physics telemetry for intake/promotion flow diagnostics."""
    return ProposalService.get_system_physics_snapshot(
        session,
        window_seconds=window_seconds,
    )


@router.get("/system/build")
async def system_build():
    """System build information and runtime identity."""
    db_profile = "unknown"
    if _engine and _engine.url:
        if "postgresql" in _engine.url.drivername:
            db_profile = "postgresql"
        elif "sqlite" in _engine.url.drivername:
            db_profile = "sqlite"
            
    semantic_enabled = os.getenv("AETHER_SEMANTIC_MERGE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    synthesis_enabled = os.getenv("AETHER_SYNTHESIS_WORKER_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    server_profile = os.getenv("AETHER_SERVER_PROFILE", "default")
    
    return {
        "branch": BOOT_BRANCH,
        "commit": BOOT_COMMIT,
        "dirty": BOOT_DIRTY,
        "diff_fingerprint": BOOT_DIFF_FINGERPRINT,
        "semantic_merge_enabled": semantic_enabled,
        "synthesis_worker_enabled": synthesis_enabled,
        "server_profile": server_profile,
        "database_profile": db_profile,
    }


from fastapi import Query
from packages.aether.models.entities import SystemMetricEvent, Proposal, ProposalContention
from sqlmodel import select

@router.get("/repos/{repo_id}/telemetry/events")
async def get_telemetry_events(
    repo_id: str,
    source_run_id: Optional[str] = None,
    contention_group_id: Optional[str] = None,
    proposal_ids: Optional[list[str]] = Query(None),
    since: Optional[str] = None,
    until: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Retrieve telemetry events with strict filtering rules."""
    statement = select(SystemMetricEvent).where(SystemMetricEvent.repo_id == repo_id)
    
    if source_run_id:
        statement = statement.where(SystemMetricEvent.source_run_id == source_run_id)
        
    # Handle since/until ISO datetimes
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            statement = statement.where(SystemMetricEvent.created_at >= since_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid since format")
    if until:
        try:
            until_dt = datetime.fromisoformat(until)
            statement = statement.where(SystemMetricEvent.created_at <= until_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid until format")

    # If contention_group_id is specified, join Proposal and ProposalContention
    if contention_group_id:
        statement = statement.join(Proposal, Proposal.id == SystemMetricEvent.proposal_id)
        statement = statement.join(ProposalContention, ProposalContention.proposal_id == Proposal.id)
        statement = statement.where(ProposalContention.contention_group_id == contention_group_id)
        
    has_proposal_ids = isinstance(proposal_ids, (list, tuple, set))
    if has_proposal_ids:
        unique_pids = list(set(proposal_ids))
        statement = statement.where(SystemMetricEvent.proposal_id.in_(unique_pids))
        
    statement = statement.order_by(SystemMetricEvent.created_at.asc(), SystemMetricEvent.id.asc())
    events = session.exec(statement).all()
    
    result = []
    for ev in events:
        result.append({
            "id": ev.id,
            "repo_id": ev.repo_id,
            "proposal_id": ev.proposal_id,
            "event_type": ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type),
            "event_reason": ev.event_reason,
            "path": ev.path,
            "synthesis_engine": ev.synthesis_engine,
            "source_run_id": ev.source_run_id,
            "created_at": ev.created_at.isoformat(),
        })
    return {"events": result}


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "aether", "version": "0.1.0"}
