"""Aether event models for realtime streaming."""

from datetime import UTC, datetime
from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class EventType(str, Enum):
    """Types of events in the Aether system."""
    # Proposal lifecycle
    PROPOSAL_SUBMITTED = "proposal.submitted"
    PROPOSAL_EVALUATING = "proposal.evaluating"
    PROPOSAL_PROMOTED = "proposal.promoted"
    PROPOSAL_REJECTED = "proposal.rejected"
    PROPOSAL_QUARANTINED = "proposal.quarantined"

    # Head updates
    HEAD_UPDATED = "head.updated"
    HEAD_ROLLBACK = "head.rollback"

    # Conflict events
    CONFLICT_DETECTED = "conflict.detected"
    CONFLICT_RESOLVED = "conflict.resolved"

    # Gate events
    GATE_STARTED = "gate.started"
    GATE_COMPLETED = "gate.completed"
    GATE_FAILED = "gate.failed"

    # Snapshot events
    SNAPSHOT_CREATED = "snapshot.created"

    # Promotion events
    PROMOTION_STARTED = "promotion.started"
    PROMOTION_COMPLETED = "promotion.completed"
    PROMOTION_FAILED = "promotion.failed"


class AetherEvent(BaseModel):
    """Base event model."""
    event_id: str
    event_type: EventType
    repo_id: str
    timestamp: datetime = Field(default_factory=_utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProposalEvent(AetherEvent):
    """Event related to a proposal."""
    proposal_id: str
    agent_id: str
    affected_paths: list[str] = Field(default_factory=list)


class PromotionEvent(AetherEvent):
    """Event related to a promotion."""
    promotion_id: str
    from_sequence: int
    to_sequence: int
    included_proposals: list[str] = Field(default_factory=list)


class HeadUpdateEvent(AetherEvent):
    """Event when head is updated."""
    old_head_id: Optional[str] = None
    new_head_id: str
    old_sequence: int
    new_sequence: int
    snapshot_id: str


class ConflictEvent(AetherEvent):
    """Event when conflict is detected or resolved."""
    conflict_set_id: str
    path: str
    proposal_ids: list[str] = Field(default_factory=list)
    active_choice_id: Optional[str] = None


class GateEvent(AetherEvent):
    """Event for gate execution."""
    gate_name: str
    snapshot_id: str
    proposal_id: Optional[str] = None
    status: str  # RUNNING, PASS, FAIL, ERROR
    duration_ms: Optional[int] = None
