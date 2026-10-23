"""Public contracts for OtterBot secure runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class Decision(str, Enum):
    """Deterministic policy decision type."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class Outcome(str, Enum):
    """Execution outcome visible in audit telemetry."""

    SUCCESS = "success"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class EgressPolicy:
    """Network egress policy attached to a session."""

    allowed_domains: list[str] = field(default_factory=list)
    allow_private_networks: bool = False
    max_requests_per_minute: int = 30


@dataclass(frozen=True)
class ActionRequest:
    """Input request contract entering the action pipeline."""

    session_id: str
    user_id: str
    channel: str
    requested_capability: str
    target: str
    args: dict[str, Any] = field(default_factory=dict)
    risk_hint: float = 0.0


@dataclass(frozen=True)
class PolicyDecision:
    """Deterministic policy output for a requested action."""

    decision: Decision
    reason_codes: list[str]
    required_approval: bool
    allowed_scopes: list[str]
    egress_policy: EgressPolicy


@dataclass(frozen=True)
class ToolManifest:
    """Tool admission contract for broker and policy checks."""

    tool_id: str
    version: str
    required_permissions: list[str]
    network_destinations: list[str]
    data_classification: str
    signature: str
    provenance: str
    runtime_isolation: str = "sandboxed"
    kill_switch: bool = False
    requires_approval: bool = False


@dataclass(frozen=True)
class ExecutionEnvelope:
    """Secure execution envelope emitted by runtime to tool broker."""

    tool_id: str
    input_ref: str
    capability_token: str
    timeout_ms: int
    resource_limits: dict[str, str]
    session_id: str


@dataclass(frozen=True)
class ToolExecutionResult:
    """Tool execution result returned to runtime."""

    tool_id: str
    session_id: str
    output: dict[str, Any]


@dataclass(frozen=True)
class AuditEvent:
    """Immutable audit event emitted for every action."""

    ts: datetime
    session_id: str
    action_id: str
    intent: str
    tool: str
    policy_decision: str
    outcome: str
    reason_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionPolicy:
    """Session-level policy envelope enforced by the deterministic engine."""

    allowed_tools: list[str]
    data_boundaries: list[str]
    retention_days: int
    approval_threshold: float
    allowed_scopes: list[str]
    egress_policy: EgressPolicy
    require_human_approval_for: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OtterGuardAssessment:
    """Secondary risk-assessment signal from OtterGuard."""

    risk_score: float
    reason_codes: list[str]
    explanation: str


@dataclass(frozen=True)
class ActionResult:
    """Top-level runtime result for API/caller consumption."""

    action_id: str
    decision: Decision
    outcome: Outcome
    reason_codes: list[str] = field(default_factory=list)
    required_approval: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
