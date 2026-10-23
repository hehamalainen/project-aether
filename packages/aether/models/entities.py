"""Core Aether entities and data models."""

from datetime import UTC, datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import DateTime
from sqlmodel import SQLModel, Field as SQLField


def generate_id(prefix: str) -> str:
    """Generate a prefixed unique ID."""
    return f"{prefix}_{uuid4().hex[:12]}"


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class ProposalStatus(str, Enum):
    """Status of a proposal in the Aether system."""
    INGESTED = "INGESTED"
    PENDING = "PENDING"
    EVALUATING = "EVALUATING"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    SUPERSEDED = "SUPERSEDED"
    REBASING = "REBASING"


class SystemMetricEventType(str, Enum):
    """Event types used for queue-physics telemetry."""

    SUBMIT_ACCEPTED = "submit_accepted"
    STATUS_PROMOTED = "status_promoted"
    STATUS_REJECTED = "status_rejected"
    STATUS_QUARANTINED = "status_quarantined"
    STATUS_SUPERSEDED = "status_superseded"
    STATUS_REBASING = "status_rebasing"
    SEMANTIC_FALLBACK = "semantic_fallback"
    BATCH_GATE_FAIL = "batch_gate_fail"
    BATCH_SIZE_K = "batch_size_k"
    STRUCTURAL_MERGE = "structural_merge"
    SYNTHESIS_MERGE = "synthesis_merge"



class GateStatus(str, Enum):
    """Status of a gate evaluation."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


class ProposalSource(str, Enum):
    """Known proposal origins for source-aware intake and reporting."""

    SYNTHETIC_HARNESS = "synthetic_harness"
    MX_SWARM = "mx_swarm"
    MANUAL = "manual"
    OTHER = "other"


# ============================================================================
# SQLModel entities (persisted)
# ============================================================================

class Repository(SQLModel, table=True):
    """An Aether repository."""
    __tablename__ = "aether_repositories"

    id: str = SQLField(default_factory=lambda: generate_id("repo"), primary_key=True)
    name: str = SQLField(index=True)
    description: Optional[str] = None

    # Current canonical head
    head_id: Optional[str] = SQLField(default=None, index=True)
    head_sequence: int = SQLField(default=0)

    # Gate configuration (JSON)
    gate_config: Optional[str] = SQLField(default=None)

    # Timestamps
    created_at: datetime = SQLField(default_factory=_utc_now)
    updated_at: datetime = SQLField(default_factory=_utc_now)

    # Settings
    max_alternatives_per_conflict: int = SQLField(default=5)
    auto_promote: bool = SQLField(default=True)
    max_rebase_attempts: int = SQLField(default=2)


class Proposal(SQLModel, table=True):
    """A proposal submitted by an agent."""
    __tablename__ = "aether_proposals"

    id: str = SQLField(default_factory=lambda: generate_id("prop"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")

    # Agent identification
    agent_id: str = SQLField(index=True)
    task_id: Optional[str] = SQLField(default=None, index=True)
    source: str = SQLField(default=ProposalSource.MANUAL.value, index=True)
    source_run_id: Optional[str] = SQLField(default=None, index=True)
    source_task_id: Optional[str] = SQLField(default=None, index=True)
    source_agent_id: Optional[str] = SQLField(default=None, index=True)
    source_attempt_id: Optional[str] = SQLField(default=None, index=True)
    intent_type: Optional[str] = SQLField(default=None)

    # Base state this proposal applies to
    base_head_id: Optional[str] = SQLField(default=None, index=True)
    base_head_sequence: int = SQLField(default=0)

    # Change content
    change_type: str = SQLField(default="unified_diff")  # unified_diff, crdt_ops, full_file
    change_content: str = SQLField(default="")  # The actual diff or content

    # Scope information
    affected_paths: str = SQLField(default="[]")  # JSON array of paths
    affected_symbols: Optional[str] = SQLField(default=None)  # JSON array of symbols
    risk_tier: int = SQLField(default=1)  # 1=low, 2=medium, 3=high

    # Confidence signals
    confidence_score: float = SQLField(default=0.5)
    model_id: Optional[str] = SQLField(default=None)
    calibration_profile: Optional[str] = SQLField(default=None)
    rationale: Optional[str] = SQLField(default=None)

    # Dependencies
    dependencies: str = SQLField(default="[]")  # JSON array of proposal IDs

    # Policy requirements
    requires_tests: bool = SQLField(default=True)
    requires_security_scan: bool = SQLField(default=False)

    # Status tracking
    status: ProposalStatus = SQLField(default=ProposalStatus.PENDING)
    conflict_set_id: Optional[str] = SQLField(default=None, index=True)
    rebase_attempts: int = SQLField(default=0)

    # Timestamps
    submitted_at: datetime = SQLField(default_factory=_utc_now)
    evaluated_at: Optional[datetime] = SQLField(default=None)
    promoted_at: Optional[datetime] = SQLField(default=None)

    # Idempotency
    idempotency_key: Optional[str] = SQLField(default=None, unique=True, index=True)


class ConflictSet(SQLModel, table=True):
    """A set of overlapping proposals that conflict."""
    __tablename__ = "aether_conflict_sets"

    id: str = SQLField(default_factory=lambda: generate_id("cs"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")

    # Region definition
    path: str = SQLField(index=True)
    region_type: str = SQLField(default="text_range")  # text_range, ast_node, symbol
    start_line: Optional[int] = SQLField(default=None)
    end_line: Optional[int] = SQLField(default=None)
    symbol_name: Optional[str] = SQLField(default=None)

    # Proposals in this conflict set (JSON array of proposal IDs)
    proposal_ids: str = SQLField(default="[]")

    # Currently selected proposal
    active_choice_id: Optional[str] = SQLField(default=None)

    # Status
    resolved: bool = SQLField(default=False)
    resolved_at: Optional[datetime] = SQLField(default=None)

    created_at: datetime = SQLField(default_factory=_utc_now)


class Snapshot(SQLModel, table=True):
    """A deterministic snapshot of repository state."""
    __tablename__ = "aether_snapshots"

    id: str = SQLField(default_factory=lambda: generate_id("snap"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")

    # What this snapshot contains
    head_sequence: int = SQLField(index=True)
    included_proposals: str = SQLField(default="[]")  # JSON array of proposal IDs

    # Content hash for integrity
    content_hash: str = SQLField(index=True)

    # Storage location
    storage_path: Optional[str] = SQLField(default=None)

    # Metadata
    created_at: datetime = SQLField(default_factory=_utc_now)
    created_by: Optional[str] = SQLField(default=None)  # agent_id or "system"

    # Size info
    file_count: int = SQLField(default=0)
    total_bytes: int = SQLField(default=0)


class GateResult(SQLModel, table=True):
    """Result of running a gate on a snapshot."""
    __tablename__ = "aether_gate_results"

    id: str = SQLField(default_factory=lambda: generate_id("gate"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")
    snapshot_id: str = SQLField(index=True, foreign_key="aether_snapshots.id")
    proposal_id: Optional[str] = SQLField(default=None, index=True)

    # Gate identification
    gate_name: str = SQLField(index=True)  # syntax, build, unit_tests, lint, security
    gate_command: Optional[str] = SQLField(default=None)

    # Results
    status: GateStatus = SQLField(default=GateStatus.PENDING)
    exit_code: Optional[int] = SQLField(default=None)
    stdout: Optional[str] = SQLField(default=None)
    stderr: Optional[str] = SQLField(default=None)

    # Metrics
    duration_ms: Optional[int] = SQLField(default=None)
    tests_passed: Optional[int] = SQLField(default=None)
    tests_failed: Optional[int] = SQLField(default=None)
    coverage_pct: Optional[float] = SQLField(default=None)

    # Timestamps
    started_at: Optional[datetime] = SQLField(default=None)
    completed_at: Optional[datetime] = SQLField(default=None)


class Promotion(SQLModel, table=True):
    """Record of a head promotion."""
    __tablename__ = "aether_promotions"

    id: str = SQLField(default_factory=lambda: generate_id("prom"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")

    # State transition
    from_head_id: Optional[str] = SQLField(default=None)
    from_head_sequence: int = SQLField(default=0)
    to_head_id: str = SQLField(index=True)
    to_head_sequence: int = SQLField(index=True)

    # What was included/excluded
    included_proposals: str = SQLField(default="[]")  # JSON array
    excluded_proposals: str = SQLField(default="[]")  # JSON array

    # Evidence from gates
    evidence: str = SQLField(default="{}")  # JSON: {gate_name: status}

    # Scoring
    policy_score: float = SQLField(default=1.0)
    confidence_score: float = SQLField(default=0.5)
    final_score: float = SQLField(default=0.5)

    # Decision metadata
    decision_rationale: Optional[str] = SQLField(default=None)
    triggered_by: str = SQLField(default="auto")  # auto, manual, rollback

    # Timestamps
    created_at: datetime = SQLField(default_factory=_utc_now)


class OperationGraphNode(SQLModel, table=True):
    """Persisted operation-graph node for deterministic lineage and replay."""
    __tablename__ = "aether_operation_graph_nodes"

    id: str = SQLField(default_factory=lambda: generate_id("opn"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")
    promotion_id: str = SQLField(index=True, foreign_key="aether_promotions.id")

    # Causal pointers
    parent_node_id: Optional[str] = SQLField(default=None, index=True)
    parent_head_id: Optional[str] = SQLField(default=None, index=True)
    parent_head_sequence: int = SQLField(default=0, index=True)
    to_head_id: Optional[str] = SQLField(default=None, index=True)
    to_head_sequence: int = SQLField(default=0, index=True)
    snapshot_id: str = SQLField(index=True, foreign_key="aether_snapshots.id")

    # Node identity and replay hints
    operation_type: str = SQLField(default="promotion")
    node_hash: str = SQLField(index=True)
    payload_sha256: str = SQLField(index=True)
    parent_ref: Optional[str] = SQLField(default=None)
    replay_from_hash: Optional[str] = SQLField(default=None)
    replay_to_hash: Optional[str] = SQLField(default=None)
    replay_gate_vector: str = SQLField(default="")
    included_proposals: str = SQLField(default="[]")  # JSON array
    excluded_proposals: str = SQLField(default="[]")  # JSON array

    created_at: datetime = SQLField(default_factory=_utc_now)


class SystemMetricEvent(SQLModel, table=True):
    """Telemetry events for queueing and semantic-fallback diagnostics."""

    __tablename__ = "aether_system_metric_events"

    id: str = SQLField(default_factory=lambda: generate_id("met"), primary_key=True)
    repo_id: Optional[str] = SQLField(default=None, index=True, foreign_key="aether_repositories.id")
    proposal_id: Optional[str] = SQLField(default=None, index=True, foreign_key="aether_proposals.id")
    event_type: SystemMetricEventType = SQLField(index=True)
    event_reason: Optional[str] = SQLField(default=None)
    path: Optional[str] = SQLField(default=None)
    synthesis_engine: Optional[str] = SQLField(default=None)
    source_run_id: Optional[str] = SQLField(default=None, index=True)
    created_at: datetime = SQLField(default_factory=_utc_now, sa_type=DateTime, index=True)



class AetherHead(SQLModel, table=True):
    """Historical record of head states (for rollback/audit)."""
    __tablename__ = "aether_heads"

    id: str = SQLField(default_factory=lambda: generate_id("head"), primary_key=True)
    repo_id: str = SQLField(index=True, foreign_key="aether_repositories.id")
    sequence: int = SQLField(index=True)

    # Snapshot this head points to
    snapshot_id: str = SQLField(foreign_key="aether_snapshots.id")

    # Promotion that created this head
    promotion_id: Optional[str] = SQLField(default=None, foreign_key="aether_promotions.id")

    # Timestamps
    created_at: datetime = SQLField(default_factory=_utc_now)

    # Is this currently canonical?
    is_canonical: bool = SQLField(default=True)


class AgentMetric(SQLModel, table=True):
    """Historical performance metrics for an agent."""
    __tablename__ = "aether_agent_metrics"

    agent_id: str = SQLField(primary_key=True)
    scope: str = SQLField(default="global", primary_key=True) # e.g., "lang:python", "repo:Aether"
    
    # Raw counters
    total_proposals: int = SQLField(default=0)
    promoted_count: int = SQLField(default=0)
    hard_fail_count: int = SQLField(default=0)
    neutral_loss_count: int = SQLField(default=0)
    
    # Bayesian Prior (Beta distribution)
    # alpha: successes (promotions)
    # beta: failures (hard gate failures)
    trust_alpha: float = SQLField(default=3.0)
    trust_beta: float = SQLField(default=3.0)
    
    # Metadata
    last_seen: datetime = SQLField(default_factory=_utc_now)
    updated_at: datetime = SQLField(default_factory=_utc_now)


# ============================================================================
# Pydantic models (API layer)
# ============================================================================

class ProposalCreate(BaseModel):
    """Request to create a new proposal."""
    agent_id: str
    task_id: Optional[str] = None
    source: str = ProposalSource.MANUAL.value
    source_run_id: Optional[str] = None
    source_task_id: Optional[str] = None
    source_agent_id: Optional[str] = None
    source_attempt_id: Optional[str] = None
    intent_type: Optional[str] = None

    # Change
    change_type: str = "unified_diff"
    change_content: str

    # Scope
    affected_paths: list[str]
    affected_symbols: Optional[list[str]] = None
    risk_tier: int = 1

    # Confidence
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.5)
    base_head_id: Optional[str] = None
    base_head_sequence: Optional[int] = None
    model_id: Optional[str] = None
    calibration_profile: Optional[str] = None
    rationale: Optional[str] = None

    # Dependencies
    dependencies: list[str] = Field(default_factory=list)

    # Policy
    requires_tests: bool = True
    requires_security_scan: bool = False

    # Idempotency
    idempotency_key: Optional[str] = None

    # Contention Metadata (Optional)
    contention_mode: Optional[bool] = None
    contention_group_id: Optional[str] = None
    contention_policy: Optional[str] = None
    target_paths: Optional[list[str]] = None
    defer_evaluation: Optional[bool] = None


class ProposalResponse(BaseModel):
    """Response after creating a proposal."""
    proposal_id: str
    status: ProposalStatus
    source: str = ProposalSource.MANUAL.value
    conflict_set_id: Optional[str] = None
    immediate_gate_results: dict[str, GateStatus] = Field(default_factory=dict)
    message: str = ""


class SnapshotCreate(BaseModel):
    """Request to create a snapshot."""
    selection_type: str = "head"  # head, proposals
    proposal_ids: Optional[list[str]] = None
    at_sequence: Optional[int] = None


class SnapshotResponse(BaseModel):
    """Response with snapshot information."""
    snapshot_id: str
    content_hash: str
    file_count: int
    total_bytes: int
    download_url: Optional[str] = None


class PromotionRequest(BaseModel):
    """Request to promote proposals."""
    proposal_ids: list[str]
    skip_gates: list[str] = Field(default_factory=list)
    force: bool = False


class PromotionResponse(BaseModel):
    """Response after promotion."""
    promotion_id: str
    success: bool
    new_head_id: str
    new_head_sequence: int
    included_proposals: list[str]
    excluded_proposals: list[str]
    evidence: dict[str, str]
    final_score: float
    message: str = ""


class HeadResponse(BaseModel):
    """Current head state."""
    head_id: str
    sequence: int
    snapshot_id: str
    content_hash: str
    last_promotion_id: Optional[str] = None
    last_promotion_at: Optional[datetime] = None


class GateConfig(BaseModel):
    """Configuration for a verification gate."""
    name: str
    command: str
    required: bool = True
    timeout_seconds: int = 300
    order: int = 0


class RepoConfig(BaseModel):
    """Repository configuration."""
    gates: list[GateConfig] = Field(default_factory=list)
    max_alternatives_per_conflict: int = 5
    auto_promote: bool = True
    promotion_threshold: float = 0.7


# ============================================================================
# Contention registry models (Phase 4D)
# ============================================================================

class ProposalContention(SQLModel, table=True):
    """Contention metadata linked to a proposal."""
    __tablename__ = "aether_proposal_contentions"

    proposal_id: str = SQLField(primary_key=True, foreign_key="aether_proposals.id")
    contention_mode: bool = SQLField(default=False)
    contention_group_id: Optional[str] = SQLField(default=None, index=True)
    contention_policy: Optional[str] = SQLField(default=None)
    target_paths: str = SQLField(default="[]")  # JSON array of paths
    defer_evaluation: bool = SQLField(default=False)


class ContentionWinner(SQLModel, table=True):
    """Transactional registry of contention winners to enforce exact-one-winner."""
    __tablename__ = "aether_contention_winners"

    repo_id: str = SQLField(primary_key=True)
    contention_group_id: str = SQLField(primary_key=True)
    winner_proposal_id: str = SQLField(index=True)
    created_at: datetime = SQLField(default_factory=_utc_now)
