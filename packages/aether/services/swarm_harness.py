from __future__ import annotations
import json
import httpx
from ..models.events import EventType
from .event_bus import create_head_event, create_proposal_event
from .promotion_engine import ExternalCommitAbortedError
from .promotion_engine import PromotionEngine
"""Continuous swarm harness for sustained high-agent flow validation."""


import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from sqlalchemy import func, text
from sqlmodel import SQLModel, Session, create_engine, select

from ..crdt.document_store import DocumentStore
from ..models.entities import (
    AetherHead,
    ConflictSet,
    GateConfig,
    Proposal,
    ProposalCreate,
    ProposalStatus,
    RepoConfig,
    Repository,
    SystemMetricEvent,
    SystemMetricEventType,
)
from .event_bus import EventBus
from .gate_runner import GateRunner
from .promotion_engine import PromotionEngine
from .proposal_service import ProposalService
from .repository_service import RepositoryService
from .snapshot_service import SnapshotService


FINGERPRINT_ENV_KEYS = (
    "AETHER_MAX_COHERENCE_BACKLOG",
    "AETHER_BACKLOG_RESUME_LEVEL",
    "AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT",
    "AETHER_ROUTE_ACCEPTED_TO_PROMOTION_RATIO_LIMIT",
    "AETHER_HARNESS_RUNTIME_SEPARATE_ENGINE",
    "AETHER_RATIO_CONTROL_MIN_MU_TPS",
    "AETHER_RATIO_CONTROL_MIN_MU_EVENTS",
    "AETHER_COHERENCE_WINDOW_SECONDS",
    "AETHER_RETRY_AFTER_MAX_SECONDS",
    "AETHER_MAX_PATH_ACTIVE_PROPOSALS",
    "AETHER_GATE_HARD_TIMEOUT_SECONDS",
)

from ..models.entities import Promotion, Proposal, ProposalStatus
from ..crdt.structured_doc import StructuredDocument
from .promotion_engine import PromotionCandidate, generate_id

TRANSIENT_EXTERNAL_COMMIT_STATUSES = {409, 423, 429, 503}


def is_transient_external_commit_status(status_code: int) -> bool:
    return status_code in TRANSIENT_EXTERNAL_COMMIT_STATUSES


def get_agent_repo_index(agent_idx: int, num_agents: int, num_repos: int = 10) -> int:
    weights = [1.0 / (i + 1) for i in range(num_repos)]
    total_weight = sum(weights)
    cum_probs = []
    curr = 0.0
    for w in weights:
        curr += w / total_weight
        cum_probs.append(curr)
    
    fraction = agent_idx / num_agents
    for idx, cp in enumerate(cum_probs):
        if fraction < cp:
            return idx
    return num_repos - 1


class ExternalPromotionEngine(PromotionEngine):
    async def promote(
        self,
        repo_id: str,
        candidate: PromotionCandidate,
        document_store: StructuredDocument,
        triggered_by: str = "batch",
    ) -> Promotion:
        if not candidate.score > 0:
            raise RuntimeError("Cannot promote candidate with non-positive score")

        promotion_id = generate_id("prom")
        
        # We need the current repo head
        from sqlmodel import select
        from ..models.entities import Repository
        repo = self.session.exec(select(Repository).where(Repository.id == repo_id)).first()
        if not repo:
            raise RuntimeError(f"Repository {repo_id} not found")

        included_proposals = [p.id for p in candidate.proposals]
        import json
        conflict_resolutions = []
        # We derive conflict resolutions from the included proposals
        for p in candidate.proposals:
            if p.conflict_set_id:
                # Need to find the superseded proposals to pass to Rust
                conflict_set = self.proposal_service.get_conflict_set(p.conflict_set_id)
                if conflict_set:
                    all_ids = json.loads(conflict_set.proposal_ids)
                    losing_ids = [pid for pid in all_ids if pid != p.id]
                    conflict_resolutions.append({
                        "conflict_set_id": p.conflict_set_id,
                        "active_choice_id": p.id,
                        "losing_proposal_ids": losing_ids,
                    })
            
        operation_node = None
        if candidate.timings.get("replay_gate_vector"):
            operation_node = {
                "id": generate_id("op"),
                "parent_node_id": None,
                "operation_type": "PROMOTION",
                "node_hash": "mock_hash",
                "payload_sha256": "mock_payload",
                "parent_ref": repo.head_id or "root",
                "replay_from_hash": "mock",
                "replay_to_hash": "mock",
                "replay_gate_vector": candidate.timings.get("replay_gate_vector", "[]"),
            }

        payload = {
            "promotion_id": promotion_id,
            "snapshot_id": candidate.snapshot_id,
            "included_proposals": included_proposals,
            "excluded_proposals": candidate.excluded_proposal_ids,
            "evidence": candidate.evidence,
            "policy_score": candidate.score,
            "final_score": candidate.score,
            "confidence_score": candidate.score,
            "triggered_by": triggered_by,
            "operation_node": operation_node,
            "conflict_resolutions": conflict_resolutions,
        }

        # Issue external POST
        client = getattr(self, "_async_api_client", None)
        if client is None:
            raise RuntimeError("Async API client not initialized for ExternalPromotionEngine")
            
        try:
            resp = await client.post(
                f"/v1/repos/{repo_id}/commit_promotion",
                json=payload,
                timeout=30.0
            )
            if is_transient_external_commit_status(resp.status_code):
                raise ExternalCommitAbortedError(
                    f"External commit aborted: {resp.status_code} - {resp.text}"
                )
            elif resp.status_code != 200:
                raise RuntimeError(f"External commit failed: {resp.status_code} - {resp.text}")
            
            resp_data = resp.json()
            new_head_id = resp_data["new_head_id"]
            new_head_sequence = resp_data["new_head_sequence"]
        except httpx.TimeoutException:
            raise ExternalCommitAbortedError("External commit timeout")

        # Event Publication Parity
        # 1. PROPOSAL_PROMOTED
        for p in candidate.proposals:
            await self.event_bus.publish(create_proposal_event(
                EventType.PROPOSAL_PROMOTED,
                repo_id,
                p.id,
                p.agent_id,
                json.loads(p.affected_paths),
                old_status="PENDING",
                reason=None
            ))
            
        # 2. CONFLICT_RESOLVED
        for p in candidate.proposals:
            if p.conflict_set_id:
                conflict_set = self.proposal_service.get_conflict_set(p.conflict_set_id)
                if conflict_set:
                    all_ids = json.loads(conflict_set.proposal_ids)
                    losing_ids = [pid for pid in all_ids if pid != p.id]
                    await self.event_bus.publish(create_proposal_event(
                        EventType.CONFLICT_RESOLVED,
                        repo_id,
                        p.id,
                        "",
                        [conflict_set.path],
                        conflict_set_id=p.conflict_set_id,
                        superseded_proposals=losing_ids
                    ))

        # 3. HEAD_UPDATED
        await self.event_bus.publish(create_head_event(
            EventType.HEAD_UPDATED,
            repo_id,
            repo.head_id,
            new_head_id,
            repo.head_sequence,
            new_head_sequence,
            candidate.snapshot_id,
            promotion_id=promotion_id,
            included_proposals=included_proposals
        ))

        # Mock promotion object to return to the loop
        from datetime import datetime, UTC
        return Promotion(
            id=promotion_id,
            repo_id=repo_id,
            from_head_id=repo.head_id,
            from_head_sequence=repo.head_sequence,
            to_head_id=new_head_id,
            to_head_sequence=new_head_sequence,
            included_proposals=json.dumps(included_proposals),
            excluded_proposals=json.dumps(candidate.excluded_proposal_ids),
            evidence=json.dumps(candidate.evidence),
            policy_score=candidate.score,
            confidence_score=candidate.score,
            final_score=candidate.score,
            decision_rationale=None,
            triggered_by=triggered_by,
            created_at=datetime.now(UTC)
        )


@dataclass(frozen=True)
class ContinuousHarnessConfig:
    """Configuration for continuous swarm simulation."""

    num_agents: int = 1000
    num_files: int = 500
    file_lines: int = 100
    run_seconds: float = 60.0
    drain_seconds: float = 20.0
    contention_mode: str = "low"
    agent_interval_min_ms: int = 500
    agent_interval_max_ms: int = 2000
    max_inflight_submissions: int = 250
    promotion_batch_size: int = 25
    promotion_non_conflicting_batches: int = 4
    promotion_max_conflict_sets: int = 1
    gate_execution_mode: str = "parallel"
    gate_max_parallel: int = 4
    promotion_interval_ms: int = 50
    promotion_iteration_timeout_seconds: float = 30.0
    materialization_batch_size: int = 500
    materialization_interval_ms: int = 20
    monitor_interval_ms: int = 500
    submit_in_worker: bool = False
    target_mode: str = "direct"
    external_api_url: str | None = None
    traffic_mix: str = "full_file"
    submit_worker_threads: int = 4
    defer_conflict_detection: bool = True
    adaptive_intake_throttle: bool = True
    intake_backlog_high_watermark: int | None = None
    intake_backlog_low_watermark: int | None = None
    intake_throttle_check_interval_ms: int = 120
    physics_window_seconds: int = 60


@dataclass(frozen=True)
class ContinuousHarnessThresholds:
    """SLO thresholds used for pass/fail in the harness report."""

    min_submit_throughput: float = 40.0
    min_promotion_throughput: float = 0.2
    min_mu_eff: float = 33.3
    max_submit_p95_ms: float = 120.0
    max_conflict_rate: float = 0.35
    max_backlog_end: int = 1500
    max_submit_failure_rate: float = 0.02
    max_window_rho: float | None = None
    max_fallback_rate: float | None = None


@dataclass
class ContinuousHarnessMetrics:
    """Collected metrics for sustained submission + promotion flow."""

    submitted_total: int = 0
    submitted_success: int = 0
    submit_failures: int = 0
    submit_throttled: int = 0
    submit_latencies_ms: list[float] = field(default_factory=list)
    enqueue_to_ack_latencies_ms: list[float] = field(default_factory=list)
    ack_to_promote_latencies_ms: list[float] = field(default_factory=list)
    promotions_created: int = 0
    proposals_promoted: int = 0
    proposals_completed: int = 0
    conflict_sets: int = 0
    backlog_end: int = 0
    backlog_peak: int = 0
    backlog_samples: list[int] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    promotion_iterations: int = 0
    iteration_outcome_empty: int = 0
    iteration_outcome_scanned_no_candidate: int = 0
    iteration_outcome_candidate_no_promotion: int = 0
    iteration_outcome_promotion_created: int = 0
    total_proposals_scanned: int = 0
    total_candidates_built: int = 0
    conflict_sets_seen: int = 0
    conflict_sets_no_candidate: int = 0
    top_repeated_proposal_ids: dict[str, int] = field(default_factory=dict)
    top_repeated_conflict_set_ids: dict[str, int] = field(default_factory=dict)
    promotion_phase_totals_ms: dict[str, float] = field(default_factory=dict)
    intake_high_watermark: int = 0
    intake_low_watermark: int = 0
    throttle_wait_seconds: float = 0.0
    throttle_engaged_samples: int = 0
    intake_deferred_submissions: int = 0
    intake_backoff_events: int = 0
    intake_backoff_seconds_total: float = 0.0
    intake_admission_rate_samples: list[float] = field(default_factory=list)
    intake_controller_reason_counts: dict[str, int] = field(default_factory=dict)
    intake_controller_reason_seconds: dict[str, float] = field(default_factory=dict)
    intake_controller_transition_count: int = 0
    intake_controller_transition_samples: list[dict[str, Any]] = field(default_factory=list)
    intake_controller_latest_state: dict[str, Any] = field(default_factory=dict)
    submit_queue_peak: int = 0
    submit_buckets_5m: list[dict[str, Any]] = field(default_factory=list)
    create_snapshot_quarantines: int = 0
    materialize_gate_workspace_quarantines: int = 0
    quarantine_reasons_by_phase: dict[str, int] = field(default_factory=dict)
    quarantine_reasons_exact: dict[str, int] = field(default_factory=dict)
    quarantine_reason_example_by_phase: dict[str, str] = field(default_factory=dict)
    submit_phase_seconds: float = 0.0
    total_phase_seconds: float = 0.0
    lambda_events_window: int = 0
    mu_events_window: int = 0
    lambda_tps_window: float = 0.0
    mu_tps_window: float = 0.0
    rho_window: float | None = None
    rho_sample_ok: bool = False
    rho_sample_note: str = "no_sample"
    fallback_rate_window: float = 0.0
    semantic_fallback_hits_window: int = 0
    semantic_fallback_by_language: dict[str, int] = field(default_factory=dict)
    avg_batch_size_k: float = 0.0
    batch_gate_failure_rate: float = 0.0

    @property
    def mu_eff(self) -> float:
        if self.total_phase_seconds <= 0:
            return 0.0
        return self.proposals_promoted / self.total_phase_seconds

    @property
    def submit_throughput(self) -> float:
        if self.submit_phase_seconds <= 0:
            return 0.0
        return self.submitted_success / self.submit_phase_seconds

    @property
    def promotion_throughput(self) -> float:
        if self.total_phase_seconds <= 0:
            return 0.0
        return self.proposals_promoted / self.total_phase_seconds

    @property
    def submit_throughput_total_phase(self) -> float:
        if self.total_phase_seconds <= 0:
            return 0.0
        return self.submitted_success / self.total_phase_seconds

    @property
    def submit_to_promotion_ratio_total_phase(self) -> float:
        promotion_tps = self.promotion_throughput
        if promotion_tps <= 0:
            return float("inf") if self.submit_throughput_total_phase > 0 else 0.0
        return self.submit_throughput_total_phase / promotion_tps

    @property
    def completion_throughput(self) -> float:
        if self.total_phase_seconds <= 0:
            return 0.0
        return self.proposals_completed / self.total_phase_seconds

    @property
    def completion_throughput_submit_phase(self) -> float:
        if self.submit_phase_seconds <= 0:
            return 0.0
        return self.proposals_completed / self.submit_phase_seconds

    @property
    def submit_to_completion_ratio_total_phase(self) -> float:
        completion_tps = self.completion_throughput
        if completion_tps <= 0:
            return float("inf") if self.submit_throughput_total_phase > 0 else 0.0
        return self.submit_throughput_total_phase / completion_tps

    @property
    def submit_failure_rate(self) -> float:
        if self.submitted_total <= 0:
            return 0.0
        return self.submit_failures / self.submitted_total

    @property
    def submit_throttled_rate(self) -> float:
        if self.submitted_total <= 0:
            return 0.0
        return self.submit_throttled / self.submitted_total

    @property
    def conflict_rate(self) -> float:
        if self.submitted_success <= 0:
            return 0.0
        return self.conflict_sets / self.submitted_success

    @property
    def intake_admission_rate_avg(self) -> float:
        if not self.intake_admission_rate_samples:
            return 1.0
        return sum(self.intake_admission_rate_samples) / len(self.intake_admission_rate_samples)

    @property
    def p95_submit_latency_ms(self) -> float:
        if not self.submit_latencies_ms:
            return 0.0
        if len(self.submit_latencies_ms) < 20:
            return max(self.submit_latencies_ms)
        ordered = sorted(self.submit_latencies_ms)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]

    @property
    def p50_submit_latency_ms(self) -> float:
        if not self.submit_latencies_ms:
            return 0.0
        return statistics.median(self.submit_latencies_ms)

    @property
    def p95_enqueue_to_ack_latency_ms(self) -> float:
        if not self.enqueue_to_ack_latencies_ms:
            return 0.0
        if len(self.enqueue_to_ack_latencies_ms) < 20:
            return max(self.enqueue_to_ack_latencies_ms)
        ordered = sorted(self.enqueue_to_ack_latencies_ms)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]

    @property
    def p50_enqueue_to_ack_latency_ms(self) -> float:
        if not self.enqueue_to_ack_latencies_ms:
            return 0.0
        return statistics.median(self.enqueue_to_ack_latencies_ms)

    @property
    def p95_ack_to_promote_latency_ms(self) -> float:
        if not self.ack_to_promote_latencies_ms:
            return 0.0
        if len(self.ack_to_promote_latencies_ms) < 20:
            return max(self.ack_to_promote_latencies_ms)
        ordered = sorted(self.ack_to_promote_latencies_ms)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]

    @property
    def p50_ack_to_promote_latency_ms(self) -> float:
        if not self.ack_to_promote_latencies_ms:
            return 0.0
        return statistics.median(self.ack_to_promote_latencies_ms)

    @property
    def promotion_phase_avg_ms(self) -> dict[str, float]:
        if self.promotion_iterations <= 0:
            return {}
        return {
            phase: total / self.promotion_iterations
            for phase, total in self.promotion_phase_totals_ms.items()
        }


@dataclass
class ContinuousHarnessReport:
    """Structured report with metrics, checks, and pass/fail outcome."""

    config: ContinuousHarnessConfig
    thresholds: ContinuousHarnessThresholds
    metrics: ContinuousHarnessMetrics
    checks: dict[str, bool]
    passed: bool
    seed: int
    started_at: float
    completed_at: float
    config_fingerprint: str = ""
    config_fingerprint_payload: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    git_commit_sha: str = "unknown"
    git_commit_short: str = "unknown"
    git_dirty: bool | None = None
    mu_eff: float = 0.0
    avg_batch_size_k: float = 0.0
    batch_gate_failure_rate: float = 0.0
    integrity_verified: bool = True
    integrity_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": {
                "num_agents": self.config.num_agents,
                "num_files": self.config.num_files,
                "file_lines": self.config.file_lines,
                "run_seconds": self.config.run_seconds,
                "drain_seconds": self.config.drain_seconds,
                "contention_mode": self.config.contention_mode,
                "agent_interval_min_ms": self.config.agent_interval_min_ms,
                "agent_interval_max_ms": self.config.agent_interval_max_ms,
                "max_inflight_submissions": self.config.max_inflight_submissions,
                "promotion_batch_size": self.config.promotion_batch_size,
                "promotion_non_conflicting_batches": self.config.promotion_non_conflicting_batches,
                "promotion_max_conflict_sets": self.config.promotion_max_conflict_sets,
                "gate_execution_mode": self.config.gate_execution_mode,
                "gate_max_parallel": self.config.gate_max_parallel,
                "promotion_interval_ms": self.config.promotion_interval_ms,
                "promotion_iteration_timeout_seconds": self.config.promotion_iteration_timeout_seconds,
                "materialization_batch_size": self.config.materialization_batch_size,
                "materialization_interval_ms": self.config.materialization_interval_ms,
                "monitor_interval_ms": self.config.monitor_interval_ms,
                "submit_in_worker": self.config.submit_in_worker,
                "target_mode": self.config.target_mode,
                "external_api_url": self.config.external_api_url,
                "traffic_mix": self.config.traffic_mix,
                "submit_worker_threads": self.config.submit_worker_threads,
                "defer_conflict_detection": self.config.defer_conflict_detection,
                "adaptive_intake_throttle": self.config.adaptive_intake_throttle,
                "intake_backlog_high_watermark": self.config.intake_backlog_high_watermark,
                "intake_backlog_low_watermark": self.config.intake_backlog_low_watermark,
                "intake_throttle_check_interval_ms": self.config.intake_throttle_check_interval_ms,
                "physics_window_seconds": self.config.physics_window_seconds,
            },
            "thresholds": {
                "min_submit_throughput": self.thresholds.min_submit_throughput,
                "min_promotion_throughput": self.thresholds.min_promotion_throughput,
                "min_mu_eff": self.thresholds.min_mu_eff,
                "max_submit_p95_ms": self.thresholds.max_submit_p95_ms,
                "max_conflict_rate": self.thresholds.max_conflict_rate,
                "max_backlog_end": self.thresholds.max_backlog_end,
                "max_submit_failure_rate": self.thresholds.max_submit_failure_rate,
                "max_window_rho": self.thresholds.max_window_rho,
                "max_fallback_rate": self.thresholds.max_fallback_rate,
            },
            "metrics": {
                "submitted_total": self.metrics.submitted_total,
                "submitted_success": self.metrics.submitted_success,
                "submit_failures": self.metrics.submit_failures,
                "submit_throttled": self.metrics.submit_throttled,
                "submit_throughput": self.metrics.submit_throughput,
                "promotion_throughput": self.metrics.promotion_throughput,
                "submit_throughput_total_phase": self.metrics.submit_throughput_total_phase,
                "submit_to_promotion_ratio_total_phase": self.metrics.submit_to_promotion_ratio_total_phase,
                "completion_throughput": self.metrics.completion_throughput,
                "completion_throughput_submit_phase": self.metrics.completion_throughput_submit_phase,
                "submit_to_completion_ratio_total_phase": self.metrics.submit_to_completion_ratio_total_phase,
                "promotions_created": self.metrics.promotions_created,
                "proposals_promoted": self.metrics.proposals_promoted,
                "proposals_completed": self.metrics.proposals_completed,
                "conflict_sets": self.metrics.conflict_sets,
                "conflict_rate": self.metrics.conflict_rate,
                "p50_submit_latency_ms": self.metrics.p50_submit_latency_ms,
                "p95_submit_latency_ms": self.metrics.p95_submit_latency_ms,
                "p50_enqueue_to_ack_latency_ms": self.metrics.p50_enqueue_to_ack_latency_ms,
                "p95_enqueue_to_ack_latency_ms": self.metrics.p95_enqueue_to_ack_latency_ms,
                "p50_ack_to_promote_latency_ms": self.metrics.p50_ack_to_promote_latency_ms,
                "p95_ack_to_promote_latency_ms": self.metrics.p95_ack_to_promote_latency_ms,
                "submit_failure_rate": self.metrics.submit_failure_rate,
                "submit_throttled_rate": self.metrics.submit_throttled_rate,
                "backlog_end": self.metrics.backlog_end,
                "backlog_peak": self.metrics.backlog_peak,
                "status_counts": self.metrics.status_counts,
                "promotion_iterations": self.metrics.promotion_iterations,
                "iteration_outcome_empty": self.metrics.iteration_outcome_empty,
                "iteration_outcome_scanned_no_candidate": self.metrics.iteration_outcome_scanned_no_candidate,
                "iteration_outcome_candidate_no_promotion": self.metrics.iteration_outcome_candidate_no_promotion,
                "iteration_outcome_promotion_created": self.metrics.iteration_outcome_promotion_created,
                "total_proposals_scanned": self.metrics.total_proposals_scanned,
                "total_candidates_built": self.metrics.total_candidates_built,
                "conflict_sets_seen": self.metrics.conflict_sets_seen,
                "conflict_sets_no_candidate": self.metrics.conflict_sets_no_candidate,
                "top_repeated_proposal_ids": self.metrics.top_repeated_proposal_ids,
                "top_repeated_conflict_set_ids": self.metrics.top_repeated_conflict_set_ids,
                "promotion_phase_totals_ms": self.metrics.promotion_phase_totals_ms,
                "promotion_phase_avg_ms": self.metrics.promotion_phase_avg_ms,
                "intake_high_watermark": self.metrics.intake_high_watermark,
                "intake_low_watermark": self.metrics.intake_low_watermark,
                "throttle_wait_seconds": self.metrics.throttle_wait_seconds,
                "throttle_engaged_samples": self.metrics.throttle_engaged_samples,
                "intake_deferred_submissions": self.metrics.intake_deferred_submissions,
                "intake_backoff_events": self.metrics.intake_backoff_events,
                "intake_backoff_seconds_total": self.metrics.intake_backoff_seconds_total,
                "intake_admission_rate_avg": self.metrics.intake_admission_rate_avg,
                "intake_controller_reason_counts": self.metrics.intake_controller_reason_counts,
                "intake_controller_reason_seconds": self.metrics.intake_controller_reason_seconds,
                "intake_controller_transition_count": self.metrics.intake_controller_transition_count,
                "intake_controller_transition_samples": self.metrics.intake_controller_transition_samples,
                "intake_controller_latest_state": self.metrics.intake_controller_latest_state,
                "submit_queue_peak": self.metrics.submit_queue_peak,
                "submit_buckets_5m": self.metrics.submit_buckets_5m,
                "create_snapshot_quarantines": self.metrics.create_snapshot_quarantines,
                "materialize_gate_workspace_quarantines": self.metrics.materialize_gate_workspace_quarantines,
                "quarantine_reasons_by_phase": self.metrics.quarantine_reasons_by_phase,
                "quarantine_reasons_exact": self.metrics.quarantine_reasons_exact,
                "quarantine_reason_example_by_phase": self.metrics.quarantine_reason_example_by_phase,
                "submit_phase_seconds": self.metrics.submit_phase_seconds,
                "total_phase_seconds": self.metrics.total_phase_seconds,
                "lambda_events_window": self.metrics.lambda_events_window,
                "mu_events_window": self.metrics.mu_events_window,
                "lambda_tps_window": self.metrics.lambda_tps_window,
                "mu_tps_window": self.metrics.mu_tps_window,
                "rho_window": self.metrics.rho_window,
                "rho_sample_ok": self.metrics.rho_sample_ok,
                "rho_sample_note": self.metrics.rho_sample_note,
                "fallback_rate_window": self.metrics.fallback_rate_window,
                "semantic_fallback_hits_window": self.metrics.semantic_fallback_hits_window,
                "semantic_fallback_by_language": self.metrics.semantic_fallback_by_language,
                "avg_batch_size_k": self.metrics.avg_batch_size_k,
                "batch_gate_failure_rate": self.metrics.batch_gate_failure_rate,
                "mu_eff": self.metrics.mu_eff,
            },
            "checks": self.checks,
            "passed": self.passed,
            "seed": self.seed,
            "config_fingerprint": self.config_fingerprint,
            "config_fingerprint_payload": self.config_fingerprint_payload,
            "interrupted": self.interrupted,
            "git_commit_sha": self.git_commit_sha,
            "git_commit_short": self.git_commit_short,
            "git_dirty": self.git_dirty,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "mu_eff": self.mu_eff,
            "avg_batch_size_k": self.avg_batch_size_k,
            "batch_gate_failure_rate": self.batch_gate_failure_rate,
            "integrity_verified": self.integrity_verified,
            "integrity_reasons": self.integrity_reasons,
        }


class ContinuousSwarmHarness:
    """Runs sustained swarm traffic and promotion loops against in-memory Aether."""

    def __init__(
        self,
        session: Session,
        storage_path: Path,
        config: ContinuousHarnessConfig,
        thresholds: ContinuousHarnessThresholds,
        seed: int = 7,
        session_factory: Callable[[], Session] | None = None,
        runtime_session_factory: Callable[[], Session] | None = None,
        shutdown_event: threading.Event | None = None,
    ):
        self.session = session
        self.storage_path = storage_path
        self.config = config
        self.thresholds = thresholds
        self.seed = seed
        self.run_id = f"swarm-{seed}"
        self.rng = random.Random(seed)
        self.session_factory = session_factory
        self.runtime_session_factory = runtime_session_factory
        self._shutdown_event = shutdown_event

        self.event_bus = EventBus()
        self.repo_service = RepositoryService(session, storage_path)
        self.proposal_service = ProposalService(
            session,
            self.event_bus,
            {},
            submit_in_worker=self.config.submit_in_worker and self.session_factory is not None,
            session_factory=self.session_factory,
            worker_threads=self.config.submit_worker_threads,
            defer_conflict_detection=self.config.defer_conflict_detection,
        )
        self.snapshot_service = SnapshotService(session, self.event_bus, storage_path)
        self.gate_runner = GateRunner(session, self.event_bus)
        if self.config.target_mode == "external":
            import os
            os.environ["AETHER_EXTERNAL_API_URL"] = self.config.external_api_url or "http://127.0.0.1:8100"
            self.promotion_engine = ExternalPromotionEngine(
                session,
                self.event_bus,
                self.proposal_service,
                self.snapshot_service,
                self.gate_runner,
                storage_path,
                gate_execution_mode=self.config.gate_execution_mode,
                gate_max_parallel=self.config.gate_max_parallel,
            )
        else:
            self.promotion_engine = PromotionEngine(
                session,
                self.event_bus,
                self.proposal_service,
                self.snapshot_service,
                self.gate_runner,
                storage_path,
                gate_execution_mode=self.config.gate_execution_mode,
                gate_max_parallel=self.config.gate_max_parallel,
            )

        self.repos: list[Repository] = []
        self.document_stores: dict[str, DocumentStore] = {}
        self.repo: Repository | None = None
        self.document_store: DocumentStore | None = None
        self.files: list[str] = []

        self.metrics = ContinuousHarnessMetrics()
        self._scanned_proposal_counter = Counter()
        self._scanned_conflict_set_counter = Counter()
        self._proposal_ack_times: dict[str, float] = {}
        self._submit_observations: list[dict[str, float | None]] = []
        self._started_perf = 0.0
        self._submit_stop = asyncio.Event()
        self._materialize_stop = asyncio.Event()
        self._promote_stop = asyncio.Event()
        self._monitor_stop = asyncio.Event()
        # The dispatcher queue should absorb one pending submit per agent without
        # forcing hot retry loops in the producer tasks.
        self._submit_queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(
            maxsize=max(self.config.num_agents, self.config.max_inflight_submissions * 4, 512)
        )
        self._submit_lock = asyncio.Semaphore(config.max_inflight_submissions)
        self._admission_permits = asyncio.Semaphore(0)
        self._admission_lock = asyncio.Lock()
        self._shutdown_requested = False
        self._current_backlog = 0
        self._intake_pressure = 0.0
        self._admission_rate = 1.0
        self._admission_backoff_until = 0.0
        self._admission_credit = 1.0
        self._admission_credit_cap = float(max(1, self.config.submit_worker_threads))
        self._intake_throttle_active = False
        self._intake_controller_last_signature: tuple[Any, ...] | None = None
        self._physics_sample_interval_seconds = max(
            1.0,
            (self.config.intake_throttle_check_interval_ms / 1000.0) * 4.0,
        )
        self._next_physics_sample_at = 0.0
        self._latest_lambda_tps = 0.0
        self._latest_mu_tps = 0.0
        self._latest_lambda_events = 0
        self._latest_mu_events = 0
        self._ratio_limit = self._parse_env_float("AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT", 1.0, minimum=0.0)
        if self._ratio_limit <= 0.0:
            self._ratio_limit = 1.0
        self._ratio_limit = min(self._ratio_limit, 1.0)
        self._ratio_min_mu_tps = self._parse_env_float("AETHER_RATIO_CONTROL_MIN_MU_TPS", 0.2, minimum=0.0)
        self._ratio_min_mu_events = self._parse_env_int("AETHER_RATIO_CONTROL_MIN_MU_EVENTS", 5, minimum=1)
        self._bootstrap_admission_tps = max(0.95, self.thresholds.min_submit_throughput * 1.1)
        (
            self._intake_high_watermark,
            self._intake_low_watermark,
        ) = self._resolve_intake_watermarks()
        self.metrics.intake_high_watermark = self._intake_high_watermark
        self.metrics.intake_low_watermark = self._intake_low_watermark

        self._api_client: Any | None = None
        self._api_event_bus: EventBus | None = None
        if self.config.target_mode == "testclient":
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from ..api.routes import router, set_services

            app = FastAPI()
            self._api_event_bus = EventBus()
            set_services(self.session.get_bind(), self._api_event_bus, self.storage_path)
            app.include_router(router)
            self._api_client = TestClient(app)
        elif self.config.target_mode == "external":
            import httpx
            if not self.config.external_api_url:
                raise ValueError("external_api_url must be provided for target_mode='external'")
            self._api_client = httpx.Client(base_url=self.config.external_api_url, timeout=30.0)
            self._async_api_client = httpx.AsyncClient(base_url=self.config.external_api_url, timeout=30.0)
        else:
            self._async_api_client = None

    def _open_runtime_session(self) -> tuple[Session, bool]:
        """Return a short-lived session for sustained loops when available."""
        if self.runtime_session_factory is None:
            return self.session, False
        return self.runtime_session_factory(), True

    def _build_runtime_services(
        self,
        session: Session,
    ) -> tuple[ProposalService, SnapshotService, PromotionEngine]:
        """Create fresh service instances bound to the provided session."""
        proposal_service = ProposalService(
            session,
            self.event_bus,
            {},
            submit_in_worker=False,
            session_factory=self.session_factory,
            worker_threads=self.config.submit_worker_threads,
            defer_conflict_detection=self.config.defer_conflict_detection,
        )
        snapshot_service = SnapshotService(session, self.event_bus, self.storage_path)
        gate_runner = GateRunner(session, self.event_bus)
        if self.config.target_mode == "external":
            os.environ["AETHER_EXTERNAL_API_URL"] = self.config.external_api_url or "http://127.0.0.1:8100"
            promotion_engine = ExternalPromotionEngine(
                session,
                self.event_bus,
                proposal_service,
                snapshot_service,
                gate_runner,
                self.storage_path,
                gate_execution_mode=self.config.gate_execution_mode,
                gate_max_parallel=self.config.gate_max_parallel,
            )
            if hasattr(self, "_async_api_client"):
                promotion_engine._async_api_client = self._async_api_client
        else:
            promotion_engine = PromotionEngine(
                session,
                self.event_bus,
                proposal_service,
                snapshot_service,
                gate_runner,
                self.storage_path,
                gate_execution_mode=self.config.gate_execution_mode,
                gate_max_parallel=self.config.gate_max_parallel,
            )
        return proposal_service, snapshot_service, promotion_engine

    def _build_config_fingerprint_payload(self) -> dict[str, Any]:
        env_payload = {
            key: value
            for key in FINGERPRINT_ENV_KEYS
            if (value := os.getenv(key)) is not None
        }
        return {
            "config": asdict(self.config),
            "thresholds": asdict(self.thresholds),
            "env": env_payload,
        }

    @staticmethod
    def _compute_config_fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_env_float(name: str, default: float, minimum: float = 0.0) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        return max(minimum, value)

    @staticmethod
    def _parse_env_int(name: str, default: int, minimum: int = 0) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return max(minimum, value)

    @staticmethod
    def _find_repo_root() -> Path | None:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / ".git").exists():
                return parent
        return None

    @classmethod
    def _resolve_git_metadata(cls) -> tuple[str, str, bool | None]:
        repo_root = cls._find_repo_root()
        if repo_root is None:
            return "unknown", "unknown", None

        try:
            commit = subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
        except Exception:
            return "unknown", "unknown", None

        commit_short = commit[:12] if commit else "unknown"
        dirty: bool | None
        try:
            status = subprocess.check_output(
                ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            dirty = bool(status.strip())
        except Exception:
            dirty = None

        return commit or "unknown", commit_short, dirty

    async def _watch_shutdown_event(self) -> None:
        if self._shutdown_event is None:
            return
        while True:
            if self._shutdown_event.is_set():
                self._shutdown_requested = True
                self._submit_stop.set()
                self._materialize_stop.set()
                self._promote_stop.set()
                self._monitor_stop.set()
                return
            if self._submit_stop.is_set() and self._materialize_stop.is_set() and self._promote_stop.is_set():
                return
            await asyncio.sleep(0.25)

    def _resolve_intake_watermarks(self) -> tuple[int, int]:
        """Resolve intake throttling watermarks from config and SLO thresholds."""
        high = self.config.intake_backlog_high_watermark
        if high is None:
            high = max(200, int(self.thresholds.max_backlog_end * 0.8))

        low = self.config.intake_backlog_low_watermark
        if low is None:
            low = max(100, int(high * 0.9))

        if low >= high:
            low = max(0, high - 1)
        return high, low

    def _validate_config(self) -> None:
        if self.config.num_agents <= 0:
            raise ValueError("num_agents must be > 0")
        if self.config.num_files <= 0:
            raise ValueError("num_files must be > 0")
        if self.config.file_lines <= 0:
            raise ValueError("file_lines must be > 0")
        if self.config.run_seconds <= 0:
            raise ValueError("run_seconds must be > 0")
        if self.config.drain_seconds < 0:
            raise ValueError("drain_seconds must be >= 0")
        if self.config.agent_interval_min_ms <= 0:
            raise ValueError("agent_interval_min_ms must be > 0")
        if self.config.agent_interval_max_ms < self.config.agent_interval_min_ms:
            raise ValueError("agent_interval_max_ms must be >= agent_interval_min_ms")
        if self.config.max_inflight_submissions <= 0:
            raise ValueError("max_inflight_submissions must be > 0")
        if self.config.promotion_batch_size <= 0:
            raise ValueError("promotion_batch_size must be > 0")
        if self.config.promotion_non_conflicting_batches <= 0:
            raise ValueError("promotion_non_conflicting_batches must be > 0")
        if self.config.promotion_max_conflict_sets < 0:
            raise ValueError("promotion_max_conflict_sets must be >= 0")
        if self.config.gate_execution_mode not in {"sequential", "fast_fail", "parallel"}:
            raise ValueError("gate_execution_mode must be one of: sequential, fast_fail, parallel")
        if self.config.gate_max_parallel <= 0:
            raise ValueError("gate_max_parallel must be > 0")
        if self.config.promotion_interval_ms <= 0:
            raise ValueError("promotion_interval_ms must be > 0")
        if self.config.promotion_iteration_timeout_seconds <= 0:
            raise ValueError("promotion_iteration_timeout_seconds must be > 0")
        if self.config.materialization_batch_size <= 0:
            raise ValueError("materialization_batch_size must be > 0")
        if self.config.materialization_interval_ms <= 0:
            raise ValueError("materialization_interval_ms must be > 0")
        if self.config.monitor_interval_ms <= 0:
            raise ValueError("monitor_interval_ms must be > 0")
        if self.config.submit_worker_threads <= 0:
            raise ValueError("submit_worker_threads must be > 0")
        if self.config.intake_backlog_high_watermark is not None and self.config.intake_backlog_high_watermark <= 0:
            raise ValueError("intake_backlog_high_watermark must be > 0")
        if self.config.intake_backlog_low_watermark is not None and self.config.intake_backlog_low_watermark < 0:
            raise ValueError("intake_backlog_low_watermark must be >= 0")
        if self.config.intake_throttle_check_interval_ms <= 0:
            raise ValueError("intake_throttle_check_interval_ms must be > 0")
        if self.config.physics_window_seconds <= 0:
            raise ValueError("physics_window_seconds must be > 0")

    def _resolve_file_count(self) -> int:
        if self.config.contention_mode == "high":
            return max(1, self.config.num_files // 4)
        if self.config.contention_mode == "low":
            return self.config.num_files * 2
        return self.config.num_files

    def _generate_file_content(self, path: str) -> str:
        content = [f'"""Module: {path}"""', ""]
        for i in range(self.config.file_lines):
            if i % 12 == 0:
                content.append(f"def function_{i}():")
                content.append("    pass")
                content.append("")
            else:
                content.append(f"# line {i}")
        return "\n".join(content)

    def _build_diff(self, file_path: str, line_num: int, old_line: str, new_line: str) -> str:
        return (
            f"diff --git a/{file_path} b/{file_path}\n"
            f"--- a/{file_path}\n"
            f"+++ b/{file_path}\n"
            f"@@ -{line_num},1 +{line_num},1 @@\n"
            f"-{old_line}\n"
            f"+{new_line}\n"
        )

    async def setup(self) -> None:
        self._validate_config()

        skip_setup = os.getenv("AETHER_HARNESS_SKIP_SETUP") == "1"
        repo_name = os.getenv("AETHER_HARNESS_REPO_NAME", f"continuous-swarm-{self.config.num_agents}")

        num_repos = 10
        self.repos = []
        self.document_stores = {}

        if skip_setup:
            for idx in range(num_repos):
                repo_name_full = f"{repo_name}-repo-{idx}"
                repo_opt = None
                for attempt in range(30):
                    for r in self.repo_service.list_repositories():
                        if r.name == repo_name_full:
                            repo_opt = r
                            break
                    if repo_opt:
                        break
                    await asyncio.sleep(1.0)

                if not repo_opt:
                    raise RuntimeError(f"Repository {repo_name_full} not found for skipped setup")
                self.repos.append(repo_opt)
                doc_store = self.repo_service.get_document_store(repo_opt.id)
                self.document_stores[repo_opt.id] = doc_store
                self.proposal_service.set_document_store(repo_opt.id, doc_store)

            self.repo = self.repos[0]
            self.document_store = self.document_stores[self.repo.id]
            file_count = self._resolve_file_count()
            for i in range(file_count):
                self.files.append(f"src/module_{i:03d}.py")
        else:
            repo_config = RepoConfig(
                gates=[
                    GateConfig(name="syntax", command="true", required=True, timeout_seconds=1, order=0),
                    GateConfig(name="tests", command="true", required=True, timeout_seconds=1, order=1),
                ],
                auto_promote=False,
            )

            file_count = self._resolve_file_count()
            for idx in range(num_repos):
                repo_name_full = f"{repo_name}-repo-{idx}"
                repo = self.repo_service.create_repository(
                    name=repo_name_full,
                    config=repo_config,
                )
                self.repos.append(repo)
                doc_store = DocumentStore(repo.id)
                self.document_stores[repo.id] = doc_store

                for i in range(file_count):
                    path = f"src/module_{i:03d}.py"
                    doc_store.apply_full_file(path, self._generate_file_content(path), "init")

                self.proposal_service.set_document_store(repo.id, doc_store)

                # Create an initial persisted snapshot/head before diff-based proposals
                initial_snapshot = await self.snapshot_service.create_snapshot(
                    repo.id,
                    doc_store,
                    [],
                    created_by="harness_setup",
                )
                
                head = AetherHead(
                    repo_id=repo.id,
                    sequence=initial_snapshot.head_sequence,
                    snapshot_id=initial_snapshot.id,
                    promotion_id=None,
                    is_canonical=True,
                )
                self.session.add(head)
                self.session.flush()

                repo.head_id = head.id
                repo.head_sequence = head.sequence
                self.session.add(repo)

            self.session.commit()
            self.repo = self.repos[0]
            self.document_store = self.document_stores[self.repo.id]
            for i in range(file_count):
                self.files.append(f"src/module_{i:03d}.py")

    async def _agent_loop(self, agent_id: str) -> None:
        seq = 0
        while not self._submit_stop.is_set():
            seq += 1
            delay_ms = self.rng.randint(self.config.agent_interval_min_ms, self.config.agent_interval_max_ms)
            await asyncio.sleep(delay_ms / 1000.0)
            if self._submit_stop.is_set():
                break
            if self.config.adaptive_intake_throttle:
                await self._enqueue_submit_intent(agent_id=agent_id, seq=seq)
                continue
            await self._submit_once(agent_id=agent_id, seq=seq)

    async def _enqueue_submit_intent(self, agent_id: str, seq: int) -> None:
        """Queue submit intent so a central dispatcher owns paced admission."""
        if self._submit_stop.is_set():
            return

        if self._submit_queue.full():
            self.metrics.intake_deferred_submissions += 1
            return

        self._submit_queue.put_nowait((agent_id, seq))
        self.metrics.submit_queue_peak = max(self.metrics.submit_queue_peak, self._submit_queue.qsize())

    @staticmethod
    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        if len(values) < 20:
            return max(values)
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[idx]

    async def _apply_adaptive_intake_throttle(self) -> bool:
        """Acquire a paced shared submit permit."""
        if not self.config.adaptive_intake_throttle:
            return True

        if self._admission_permits.locked():
            self.metrics.intake_deferred_submissions += 1
        started = time.perf_counter()
        await self._admission_permits.acquire()
        self.metrics.throttle_wait_seconds += time.perf_counter() - started
        return not self._submit_stop.is_set()

    async def _submit_dispatcher_loop(self) -> None:
        """Serialize paced submit dispatch so agents do not race the API lock."""
        wait_slice = max(0.05, self.config.intake_throttle_check_interval_ms / 1000.0)
        while True:
            if self._submit_stop.is_set():
                break
            try:
                agent_id, seq = await asyncio.wait_for(self._submit_queue.get(), timeout=wait_slice)
            except asyncio.TimeoutError:
                continue
            try:
                allowed = await self._apply_adaptive_intake_throttle()
                if not allowed:
                    break
                if self._submit_stop.is_set():
                    break
                await self._submit_once(agent_id=agent_id, seq=seq)
            finally:
                self._submit_queue.task_done()

    async def _admission_controller_loop(self) -> None:
        """Shape intake with a shared paced permit budget instead of per-agent sampling."""
        if not self.config.adaptive_intake_throttle:
            return

        interval = max(0.02, self.config.intake_throttle_check_interval_ms / 1000.0)
        last_tick = time.perf_counter()
        while not self._submit_stop.is_set():
            permits_to_release = 0
            async with self._admission_lock:
                now = time.perf_counter()
                elapsed = max(interval, now - last_tick)
                last_tick = now
                in_backoff = now < self._admission_backoff_until
                pressure = max(0.0, min(1.0, self._intake_pressure))
                observed_limit: float | None = None
                active_limiters: list[str] = []

                target_tps = self._bootstrap_admission_tps
                if (
                    self._latest_mu_tps >= self._ratio_min_mu_tps
                    and self._latest_mu_events >= self._ratio_min_mu_events
                ):
                    observed_limit = self._latest_mu_tps * self._ratio_limit
                    target_tps = min(
                        target_tps,
                        max(self._bootstrap_admission_tps * 0.9, observed_limit),
                    )
                    active_limiters.append("ratio_limit")

                # Deterministic walls first, then taper between watermarks.
                if self._current_backlog >= self._intake_high_watermark or in_backoff:
                    target_tps = 0.0
                    if self._current_backlog >= self._intake_high_watermark:
                        active_limiters.append("backlog_wall")
                    if in_backoff:
                        active_limiters.append("api_backoff")
                elif pressure > 0.0:
                    target_tps *= max(0.0, 1.0 - pressure)
                    active_limiters.append("backlog_taper")

                normalized_rate = min(1.0, target_tps / max(self._bootstrap_admission_tps, 1e-9))
                self._admission_rate = normalized_rate
                self.metrics.intake_admission_rate_samples.append(normalized_rate)

                if target_tps <= 0.0:
                    self._admission_credit = 0.0
                else:
                    # Accumulate against actual elapsed wall time so event-loop stalls do not starve intake.
                    self._admission_credit = min(
                        self._admission_credit_cap,
                        self._admission_credit + (target_tps * elapsed),
                    )
                    permits_to_release = int(self._admission_credit)
                    if permits_to_release > 0:
                        self._admission_credit -= permits_to_release

                self._record_intake_controller_state(
                    now=now,
                    elapsed=elapsed,
                    active_limiters=active_limiters,
                    target_tps=target_tps,
                    normalized_rate=normalized_rate,
                    permits_to_release=permits_to_release,
                    in_backoff=in_backoff,
                    pressure=pressure,
                    observed_limit=observed_limit,
                )

            for _ in range(permits_to_release):
                self._admission_permits.release()

            await asyncio.sleep(interval)

    async def _submit_once(self, agent_id: str, seq: int) -> None:
        if not self.repos:
            return
        if not self.files:
            return

        agent_idx_num = int(agent_id.split("_")[1])
        repo_idx = get_agent_repo_index(agent_idx_num, self.config.num_agents)
        repo = self.repos[repo_idx]
        document_store = self.document_stores[repo.id]

        if self.config.traffic_mix in ("unified_diff", "mixed"):
            agent_idx = hash(agent_id) % len(self.files)
            file_path = self.files[agent_idx]
        else:
            file_path = self.rng.choice(self.files)

        self.metrics.submitted_total += 1
        try:
            start = time.perf_counter()

            old_content = document_store.get_file_content(file_path) if document_store else None
            if not old_content:
                old_content = self._generate_file_content(file_path)

            lines = old_content.splitlines(keepends=True)
            if not lines:
                return
            line_num = self.rng.randint(0, len(lines) - 1)
            original_line = lines[line_num]
            lines[line_num] = f"# modified by {agent_id}\n"
            new_content = "".join(lines)

            import difflib
            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=file_path,
                tofile=file_path,
            ))
            diff = "".join(diff_lines)

            if self.config.target_mode == "external":
                payload = {
                    "agent_id": agent_id,
                    "task_id": f"{agent_id}-task-{seq}",
                    "source": "swarm_harness",
                    "source_run_id": self.run_id,
                    "source_task_id": f"{agent_id}-task-{seq}",
                    "source_agent_id": agent_id,
                    "source_attempt_id": f"{agent_id}-attempt-{seq}",
                    "intent_type": "continuous_harness_update",
                    "change_type": "unified_diff",
                    "change_content": diff,
                    "affected_paths": [file_path],
                    "confidence_score": self.rng.uniform(0.7, 0.98),
                    "rationale": f"continuous harness line {line_num}",
                    "dependencies": [],
                    "requires_tests": True,
                    "requires_security_scan": False,
                    "idempotency_key": f"{agent_id}-{seq}",
                }
                status_code, response_body, retry_after_seconds, route_timings = await asyncio.to_thread(
                    self._submit_via_api_route,
                    repo.id,
                    payload,
                )
                if status_code in (200, 201):
                    proposal_id = str(response_body.get("proposal_id", ""))
                    if proposal_id:
                        ack_at = time.perf_counter()
                        enqueue_to_ack_ms = (ack_at - start) * 1000.0
                        self.metrics.submitted_success += 1
                        self.metrics.submit_latencies_ms.append(enqueue_to_ack_ms)
                        self.metrics.enqueue_to_ack_latencies_ms.append(enqueue_to_ack_ms)
                        self._proposal_ack_times[proposal_id] = ack_at
                        self._submit_observations.append(
                            {
                                "offset_seconds": max(0.0, ack_at - self._started_perf),
                                "submit_latency_ms": enqueue_to_ack_ms,
                                "route_handler_ms": route_timings.get("route_handler_ms", 0.0),
                                "service_total_ms": route_timings.get("service_total_ms", 0.0),
                                "db_write_ms": route_timings.get("db_write_ms", 0.0),
                                "conflict_or_enqueue_ms": route_timings.get("conflict_or_enqueue_ms", 0.0),
                            }
                        )
                    else:
                        self.metrics.submit_failures += 1
                elif status_code == 429:
                    self.metrics.submit_throttled += 1
                    self.metrics.intake_backoff_events += 1
                    async with self._admission_lock:
                        self._admission_rate = 0.0
                        self._admission_credit = 0.0
                        backoff_seconds = retry_after_seconds
                        if backoff_seconds is None:
                            base_sleep = self.config.intake_throttle_check_interval_ms / 1000.0
                            backoff_seconds = base_sleep * 6.0
                        self.metrics.intake_backoff_seconds_total += backoff_seconds
                        self._admission_backoff_until = max(
                            self._admission_backoff_until,
                            time.perf_counter() + backoff_seconds,
                        )
                else:
                    self.metrics.submit_failures += 1
            else:
                proposal = ProposalCreate(
                    agent_id=agent_id,
                    task_id=f"{agent_id}-task-{seq}",
                    source="synthetic_harness",
                    source_run_id=self.run_id,
                    source_task_id=f"{agent_id}-task-{seq}",
                    source_agent_id=agent_id,
                    source_attempt_id=f"{agent_id}-attempt-{seq}",
                    intent_type="continuous_harness_update",
                    change_type="unified_diff",
                    change_content=diff,
                    affected_paths=[file_path],
                    confidence_score=self.rng.uniform(0.7, 0.98),
                    rationale=f"continuous harness line {line_num}",
                )
                async with self._submit_lock:
                    response = await self.proposal_service.submit_proposal(repo.id, proposal)
                ack_at = time.perf_counter()
                enqueue_to_ack_ms = (ack_at - start) * 1000.0
                self.metrics.submitted_success += 1
                self.metrics.submit_latencies_ms.append(enqueue_to_ack_ms)
                self.metrics.enqueue_to_ack_latencies_ms.append(enqueue_to_ack_ms)
                self._proposal_ack_times[response.proposal_id] = ack_at
        except Exception as e:
            import traceback; print(f"SUBMIT ONCE ERROR: {e}"); traceback.print_exc()
            self.metrics.submit_failures += 1

    def _submit_via_api_route(
        self,
        repo_id: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any], float | None, dict[str, float]]:
        """Submit proposal through API route path (exercises route-level backpressure)."""
        if self._api_client is None:
            raise RuntimeError("API client not initialized")
        response = self._api_client.post(f"/v1/repos/{repo_id}/proposals", json=payload)
        try:
            body = response.json()
        except Exception:
            body = {}
        retry_after_raw = response.headers.get("Retry-After")
        retry_after_seconds: float | None = None
        if retry_after_raw is not None:
            try:
                retry_after_seconds = max(0.0, float(retry_after_raw))
            except (TypeError, ValueError):
                retry_after_seconds = None
        timing_headers = {
            "route_handler_ms": float(response.headers.get("X-Aether-Route-Handler-Ms", "0") or 0.0),
            "service_total_ms": float(response.headers.get("X-Aether-Submit-Service-Ms", "0") or 0.0),
            "db_write_ms": float(response.headers.get("X-Aether-Submit-DB-Write-Ms", "0") or 0.0),
            "conflict_or_enqueue_ms": float(
                response.headers.get("X-Aether-Submit-Conflict-Enqueue-Ms", "0") or 0.0
            ),
        }
        return response.status_code, body, retry_after_seconds, timing_headers

    @staticmethod
    def _quarantine_phase_from_reason(reason: str | None) -> str:
        """Extract the failing sub-phase from a persisted quarantine reason."""
        if not reason:
            return "unknown"
        parts = reason.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return "unknown"

    def _load_completion_events(
        self,
        started_at_wall: float,
    ) -> list[tuple[float, SystemMetricEventType, str | None]]:
        """Load completion/quarantine events with wall-clock offsets for diagnostics."""
        if not self.repos:
            return []
        runtime_session, should_close = self._open_runtime_session()
        statement = (
            select(SystemMetricEvent.created_at, SystemMetricEvent.event_type, SystemMetricEvent.event_reason)
            .where(
                SystemMetricEvent.repo_id.in_([r.id for r in self.repos]),
                SystemMetricEvent.event_type.in_(
                    (
                        SystemMetricEventType.STATUS_PROMOTED,
                        SystemMetricEventType.STATUS_REJECTED,
                        SystemMetricEventType.STATUS_SUPERSEDED,
                        SystemMetricEventType.STATUS_QUARANTINED,
                    )
                ),
            )
            .order_by(SystemMetricEvent.created_at.asc())
        )
        try:
            events: list[tuple[float, SystemMetricEventType, str | None]] = []
            for created_at, event_type, event_reason in runtime_session.exec(statement):
                if getattr(created_at, "tzinfo", None) is None:
                    event_epoch = created_at.replace(tzinfo=UTC).timestamp()
                else:
                    event_epoch = created_at.timestamp()
                offset = max(0.0, float(event_epoch) - started_at_wall)
                events.append((offset, event_type, event_reason))
            return events
        finally:
            if should_close:
                runtime_session.close()

    def _build_submit_buckets(self, started_at_wall: float) -> list[dict[str, Any]]:
        bucket_seconds = 300.0
        completion_offsets: list[float] = []
        quarantine_offsets: list[float] = []
        quarantine_phase_offsets: dict[str, list[float]] = {}
        for offset, event_type, event_reason in self._load_completion_events(started_at_wall):
            completion_offsets.append(offset)
            if event_type == SystemMetricEventType.STATUS_QUARANTINED:
                quarantine_offsets.append(offset)
                phase = self._quarantine_phase_from_reason(event_reason)
                quarantine_phase_offsets.setdefault(phase, []).append(offset)

        if not self._submit_observations and not completion_offsets:
            return []

        max_offset = 0.0
        if self._submit_observations:
            max_offset = max(
                max_offset,
                max(float(sample["offset_seconds"] or 0.0) for sample in self._submit_observations),
            )
        if completion_offsets:
            max_offset = max(max_offset, max(completion_offsets))

        bucket_count = max(1, int(max_offset // bucket_seconds) + 1)
        buckets: list[dict[str, Any]] = []
        for bucket_index in range(bucket_count):
            start_second = bucket_index * bucket_seconds
            end_second = start_second + bucket_seconds
            submit_samples = [
                sample
                for sample in self._submit_observations
                if start_second <= float(sample["offset_seconds"] or 0.0) < end_second
            ]
            completions = sum(1 for offset in completion_offsets if start_second <= offset < end_second)
            quarantines = sum(1 for offset in quarantine_offsets if start_second <= offset < end_second)
            quarantine_phase_counts = {
                phase: sum(1 for offset in offsets if start_second <= offset < end_second)
                for phase, offsets in quarantine_phase_offsets.items()
            }
            quarantine_phase_counts = {
                phase: count for phase, count in quarantine_phase_counts.items() if count > 0
            }
            create_snapshot_quarantines = sum(
                1
                for offset in quarantine_phase_offsets.get("create_snapshot", [])
                if start_second <= offset < end_second
            )
            materialize_gate_workspace_quarantines = sum(
                1
                for offset in quarantine_phase_offsets.get("materialize_gate_workspace", [])
                if start_second <= offset < end_second
            )
            submit_latencies = [float(sample["submit_latency_ms"] or 0.0) for sample in submit_samples]
            route_latencies = [
                float(sample["route_handler_ms"] or 0.0)
                for sample in submit_samples
                if sample.get("route_handler_ms")
            ]
            db_latencies = [
                float(sample["db_write_ms"] or 0.0)
                for sample in submit_samples
                if sample.get("db_write_ms")
            ]
            enqueue_latencies = [
                float(sample["conflict_or_enqueue_ms"] or 0.0)
                for sample in submit_samples
                if sample.get("conflict_or_enqueue_ms")
            ]
            buckets.append(
                {
                    "bucket_index": bucket_index,
                    "start_second": int(start_second),
                    "end_second": int(end_second),
                    "accepted_submits": len(submit_samples),
                    "completions": completions,
                    "quarantines": quarantines,
                    "create_snapshot_quarantines": create_snapshot_quarantines,
                    "materialize_gate_workspace_quarantines": materialize_gate_workspace_quarantines,
                    "quarantine_phase_counts": quarantine_phase_counts,
                    "submit_p95_ms": self._p95(submit_latencies),
                    "route_latency_p95_ms": self._p95(route_latencies),
                    "route_db_write_p95_ms": self._p95(db_latencies),
                    "route_conflict_enqueue_p95_ms": self._p95(enqueue_latencies),
                }
            )
        return buckets

    async def _promotion_loop(self) -> None:
        if not self.repos:
            return

        while not self._promote_stop.is_set():
            for repo in self.repos:
                doc_store = self.document_stores[repo.id]
                runtime_session, should_close = self._open_runtime_session()
                try:
                    _, _, promotion_engine = self._build_runtime_services(runtime_session)
                    scan_limit = max(
                        self.config.promotion_batch_size
                        * self.config.promotion_non_conflicting_batches
                        * 8,
                        400,
                    )
                    promotions = await promotion_engine.process_pending_proposals(
                        repo.id,
                        doc_store,
                        batch_size=self.config.promotion_batch_size,
                        proposal_scan_limit=scan_limit,
                        max_conflict_sets=self.config.promotion_max_conflict_sets,
                        max_non_conflicting_batches=self.config.promotion_non_conflicting_batches,
                        max_processing_seconds=self.config.promotion_iteration_timeout_seconds,
                        recover_stuck_after_seconds=max(
                            15.0,
                            self.config.promotion_iteration_timeout_seconds * 2.0,
                        ),
                        prioritize_non_conflicting=True,
                    )
                    iteration_timing = promotion_engine.get_last_iteration_timing()
                    if iteration_timing:
                        self.metrics.promotion_iterations += 1
                        for phase, value in iteration_timing.items():
                            self.metrics.promotion_phase_totals_ms[phase] = (
                                self.metrics.promotion_phase_totals_ms.get(phase, 0.0) + float(value)
                            )
                        
                        scanned = iteration_timing.get("proposals_scanned_count", 0.0)
                        built = iteration_timing.get("candidates_built_count", 0.0)
                        promoted = iteration_timing.get("promotions_created_count", 0.0)
                        
                        self.metrics.total_proposals_scanned += int(scanned)
                        self.metrics.total_candidates_built += int(built)
                        self.metrics.conflict_sets_seen += int(iteration_timing.get("conflict_sets_seen_count", 0.0))
                        self.metrics.conflict_sets_no_candidate += int(iteration_timing.get("conflict_sets_no_candidate_count", 0.0))
                        
                        if scanned == 0:
                            self.metrics.iteration_outcome_empty += 1
                        elif scanned > 0 and built == 0:
                            self.metrics.iteration_outcome_scanned_no_candidate += 1
                        elif built > 0 and promoted == 0:
                            self.metrics.iteration_outcome_candidate_no_promotion += 1
                        elif promoted > 0:
                            self.metrics.iteration_outcome_promotion_created += 1
                            
                    scanned_ids = getattr(promotion_engine, "_last_scanned_proposal_ids", [])
                    for pid in scanned_ids:
                        self._scanned_proposal_counter[pid] += 1
                    
                    scanned_cs_ids = getattr(promotion_engine, "_last_scanned_conflict_set_ids", [])
                    for csid in scanned_cs_ids:
                        self._scanned_conflict_set_counter[csid] += 1

                    if promotions:
                        self.metrics.promotions_created += len(promotions)
                except Exception as e:
                    import traceback; print(f"PROMO LOOP ERROR: {e}"); traceback.print_exc()
                    runtime_session.rollback()
                finally:
                    if should_close:
                        runtime_session.close()

            if self._promote_stop.is_set():
                break
            await asyncio.sleep(self.config.promotion_interval_ms / 1000.0)

    async def _materialization_loop(self) -> None:
        """Materialize INGESTED proposals outside promotion iterations."""
        if not self.repos:
            return

        while not self._materialize_stop.is_set():
            for repo in self.repos:
                runtime_session, should_close = self._open_runtime_session()
                try:
                    proposal_service, _, _ = self._build_runtime_services(runtime_session)
                    await proposal_service.materialize_ingested_queue(
                        repo.id,
                        limit=self.config.materialization_batch_size,
                    )
                except Exception as e:
                    import traceback; print(f"MAT LOOP ERROR: {e}"); traceback.print_exc()
                    runtime_session.rollback()
                finally:
                    if should_close:
                        runtime_session.close()

            if self._materialize_stop.is_set():
                break
            await asyncio.sleep(self.config.materialization_interval_ms / 1000.0)

    async def _monitor_loop(self) -> None:
        while not self._monitor_stop.is_set():
            now = time.perf_counter()
            runtime_session, should_close = self._open_runtime_session()
            try:
                ingested = self._count_status(ProposalStatus.INGESTED, session=runtime_session)
                pending = self._count_status(ProposalStatus.PENDING, session=runtime_session)
                evaluating = self._count_status(ProposalStatus.EVALUATING, session=runtime_session)
                if now >= self._next_physics_sample_at:
                    physics = ProposalService.get_system_physics_snapshot(
                        runtime_session,
                        window_seconds=self.config.physics_window_seconds,
                    )
                    events = physics.get("events", {})
                    self._latest_lambda_tps = float(physics.get("lambda_tps", 0.0) or 0.0)
                    self._latest_mu_tps = float(physics.get("mu_tps", 0.0) or 0.0)
                    self._latest_lambda_events = int(events.get("submit_accepted", 0) or 0)
                    self._latest_mu_events = (
                        int(events.get("status_promoted", 0) or 0)
                        + int(events.get("status_rejected", 0) or 0)
                        + int(events.get("status_quarantined", 0) or 0)
                        + int(events.get("status_superseded", 0) or 0)
                    )
                    self._next_physics_sample_at = now + self._physics_sample_interval_seconds
            finally:
                if should_close:
                    runtime_session.close()
            backlog = ingested + pending + evaluating
            self._current_backlog = backlog
            self.metrics.backlog_samples.append(backlog)
            self.metrics.backlog_peak = max(self.metrics.backlog_peak, backlog)
            self.metrics.submit_queue_peak = max(self.metrics.submit_queue_peak, self._submit_queue.qsize())
            span = max(1, self._intake_high_watermark - self._intake_low_watermark)
            pressure = (backlog - self._intake_low_watermark) / span
            self._intake_pressure = max(0.0, min(1.0, pressure))
            if self.config.adaptive_intake_throttle:
                self._intake_throttle_active = self._intake_pressure > 0.0
                if self._intake_throttle_active:
                    self.metrics.throttle_engaged_samples += 1
            await asyncio.sleep(self.config.monitor_interval_ms / 1000.0)

    def _record_intake_controller_state(
        self,
        *,
        now: float,
        elapsed: float,
        active_limiters: list[str],
        target_tps: float,
        normalized_rate: float,
        permits_to_release: int,
        in_backoff: bool,
        pressure: float,
        observed_limit: float | None,
    ) -> None:
        limiters = tuple(sorted(set(active_limiters))) or ("none",)
        for limiter in limiters:
            self.metrics.intake_controller_reason_counts[limiter] = (
                self.metrics.intake_controller_reason_counts.get(limiter, 0) + 1
            )
            self.metrics.intake_controller_reason_seconds[limiter] = (
                self.metrics.intake_controller_reason_seconds.get(limiter, 0.0) + elapsed
            )

        state = {
            "offset_seconds": max(0.0, now - self._started_perf),
            "limiters": list(limiters),
            "target_tps": target_tps,
            "normalized_rate": normalized_rate,
            "backlog": self._current_backlog,
            "submit_queue_depth": self._submit_queue.qsize(),
            "pressure": pressure,
            "in_backoff": in_backoff,
            "permits_to_release": permits_to_release,
            "admission_credit": self._admission_credit,
            "latest_lambda_tps": self._latest_lambda_tps,
            "latest_mu_tps": self._latest_mu_tps,
            "latest_lambda_events": self._latest_lambda_events,
            "latest_mu_events": self._latest_mu_events,
            "observed_limit_tps": observed_limit,
        }
        self.metrics.intake_controller_latest_state = state

        signature = (
            limiters,
            in_backoff,
            int(round(pressure * 1000.0)),
            int(round(target_tps * 1000.0)),
            int(round(self._latest_mu_tps * 1000.0)),
            self._current_backlog,
            permits_to_release,
        )
        if signature == self._intake_controller_last_signature:
            return

        self._intake_controller_last_signature = signature
        self.metrics.intake_controller_transition_count += 1
        if len(self.metrics.intake_controller_transition_samples) < 64:
            self.metrics.intake_controller_transition_samples.append(dict(state))

    def _count_status(self, status: ProposalStatus, *, repo_id: str | None = None, session: Session | None = None) -> int:
        if not self.repos:
            return 0
        active_session = session or self.session
        statement = select(func.count()).select_from(Proposal).where(
            Proposal.status == status
        )
        if repo_id:
            statement = statement.where(Proposal.repo_id == repo_id)
        else:
            statement = statement.where(Proposal.repo_id.in_([r.id for r in self.repos]))
            
        value = active_session.exec(statement).one()
        if isinstance(value, tuple):
            return int(value[0])
        return int(value)

    def _count_conflict_sets(self, *, session: Session | None = None) -> int:
        if not self.repos:
            return 0
        active_session = session or self.session
        value = active_session.exec(
            select(func.count()).select_from(ConflictSet).where(
                ConflictSet.repo_id.in_([r.id for r in self.repos])
            )
        ).one()
        if isinstance(value, tuple):
            return int(value[0])
        return int(value)

    def verify_integrity(self, session: Session) -> bool:
        """Verify the invariant constraints and telemetry event integrity."""
        from packages.aether.models.entities import Proposal, ProposalStatus, ConflictSet, SystemMetricEvent, SystemMetricEventType
        import json
        
        self.integrity_reasons = []
        
        # 1. Invariant count verification: unique proposals submitted == terminal + active
        all_run_proposals = list(session.exec(select(Proposal).where(Proposal.source_run_id == self.run_id)))
        total_unique_submitted = len({p.id for p in all_run_proposals})
        total_terminal = len({p.id for p in all_run_proposals if p.status in (ProposalStatus.PROMOTED, ProposalStatus.REJECTED, ProposalStatus.SUPERSEDED, ProposalStatus.QUARANTINED)})
        total_active = len({p.id for p in all_run_proposals if p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING, ProposalStatus.REBASING)})
        
        if total_unique_submitted != total_terminal + total_active:
            self.integrity_reasons.append(f"unique proposals submitted count mismatch: total={total_unique_submitted}, terminal={total_terminal}, active={total_active}")
            return False
            
        # 2. Conflict sets run isolation check
        all_conflict_sets = list(session.exec(select(ConflictSet)))
        for cs in all_conflict_sets:
            try:
                cs_proposal_ids = json.loads(cs.proposal_ids)
                if not isinstance(cs_proposal_ids, list):
                    self.integrity_reasons.append(f"ConflictSet {cs.id} proposal_ids is not a list")
                    return False
                
                # Check if this conflict set has any proposal belonging to the current run
                has_current = False
                for pid in cs_proposal_ids:
                    prop = session.get(Proposal, pid)
                    if prop and prop.source_run_id == self.run_id:
                        has_current = True
                        break
                        
                if has_current:
                    # If it belongs to current run, ALL proposals in it must belong to current run
                    for pid in cs_proposal_ids:
                        prop = session.get(Proposal, pid)
                        if prop and prop.source_run_id != self.run_id:
                            self.integrity_reasons.append(f"ConflictSet {cs.id} contains mixed runs (proposal {pid} run_id {prop.source_run_id} != harness run_id {self.run_id})")
                            return False
            except Exception as e:
                self.integrity_reasons.append(f"ConflictSet {cs.id} validation exception: {str(e)}")
                return False
                
        # 3. Batch size telemetry and malformed JSON check
        batch_events = list(session.exec(
            select(SystemMetricEvent).where(
                SystemMetricEvent.event_type.in_([SystemMetricEventType.BATCH_SIZE_K, SystemMetricEventType.BATCH_GATE_FAIL]),
                SystemMetricEvent.source_run_id == self.run_id
            )
        ))
        for ev in batch_events:
            if not ev.event_reason:
                self.integrity_reasons.append(f"Event {ev.id} missing reason")
                return False
            try:
                metadata = json.loads(ev.event_reason)
                if not isinstance(metadata, dict):
                    self.integrity_reasons.append(f"Event {ev.id} reason is not a JSON dict")
                    return False
                
                mixed_run = metadata.get("mixed_run")
                if mixed_run is True:
                    # For mixed runs, expect source_run_id to be non-null and match self.run_id
                    if ev.source_run_id is None:
                        self.integrity_reasons.append(f"Mixed run event {ev.id} has null source_run_id")
                        return False
                    if ev.source_run_id != self.run_id:
                        self.integrity_reasons.append(f"Mixed run event {ev.id} source_run_id {ev.source_run_id} != harness run_id {self.run_id}")
                        return False
                    run_member_count = metadata.get("run_member_count", {})
                    if not isinstance(run_member_count, dict) or self.run_id not in run_member_count:
                        self.integrity_reasons.append(f"Harness run_id {self.run_id} not in run_member_count for event {ev.id}")
                        return False
                elif mixed_run is False:
                    if ev.source_run_id is None:
                        self.integrity_reasons.append(f"Homogeneous run event {ev.id} has null source_run_id")
                        return False
                else:
                    self.integrity_reasons.append(f"Event {ev.id} has invalid mixed_run flag: {mixed_run}")
                    return False
            except Exception as e:
                self.integrity_reasons.append(f"Event {ev.id} verification error: {str(e)}")
                return False
                
        return True

    def _status_counts(self, *, session: Session | None = None) -> dict[str, int]:
        if not self.repos:
            return {}
        active_session = session or self.session
        res = active_session.exec(
            select(Proposal).where(
                Proposal.repo_id.in_([r.id for r in self.repos]),
                Proposal.source_run_id == self.run_id
            )
        )
        proposals = res.all() if hasattr(res, "all") else list(res)
        # Apply manual filtering in case of mocked session execution (which ignores SQL filters)
        repo_ids = [r.id for r in self.repos]
        proposals = [
            p for p in proposals
            if getattr(p, "source_run_id", None) == self.run_id
            and getattr(p, "repo_id", None) in repo_ids
        ]
        counter = Counter(proposal.status.value for proposal in proposals)
        return dict(counter)

    def _evaluate_checks(self) -> dict[str, bool]:
        rho_window = self.metrics.rho_window
        rho_threshold = self.thresholds.max_window_rho
        fallback_threshold = self.thresholds.max_fallback_rate
        rho_drained_exception = (
            self.metrics.backlog_end == 0
            and self.metrics.lambda_events_window == 0
            and self.metrics.mu_events_window == 0
        )
        rho_sample_ok = self.metrics.rho_sample_ok or rho_drained_exception
        window_rho_ok = True
        if rho_threshold is not None:
            if rho_window is None:
                window_rho_ok = rho_drained_exception
            else:
                window_rho_ok = rho_window <= rho_threshold
        return {
            "submit_throughput_ok": self.metrics.submit_throughput >= self.thresholds.min_submit_throughput,
            # Compare integration service rate against the active intake window, not the intake+drain wall clock.
            "promotion_throughput_ok": (
                self.metrics.completion_throughput_submit_phase
                >= self.thresholds.min_promotion_throughput
            ),
            "mu_eff_ok": self.metrics.mu_eff >= self.thresholds.min_mu_eff,
            "submit_p95_ok": self.metrics.p95_submit_latency_ms <= self.thresholds.max_submit_p95_ms,
            "conflict_rate_ok": self.metrics.conflict_rate <= self.thresholds.max_conflict_rate,
            "backlog_end_ok": self.metrics.backlog_end <= self.thresholds.max_backlog_end,
            "submit_failure_rate_ok": self.metrics.submit_failure_rate <= self.thresholds.max_submit_failure_rate,
            "rho_sample_ok": rho_sample_ok,
            "window_rho_ok": window_rho_ok,
            "fallback_rate_ok": (
                fallback_threshold is None
                or self.metrics.fallback_rate_window <= fallback_threshold
            ),
        }

    def _capture_physics_window(self) -> None:
        runtime_session, should_close = self._open_runtime_session()
        try:
            physics = ProposalService.get_system_physics_snapshot(
                runtime_session,
                window_seconds=self.config.physics_window_seconds,
            )
        finally:
            if should_close:
                runtime_session.close()
        events = physics.get("events", {})
        submit_accepted = int(events.get("submit_accepted", 0) or 0)
        status_promoted = int(events.get("status_promoted", 0) or 0)
        status_rejected = int(events.get("status_rejected", 0) or 0)
        status_quarantined = int(events.get("status_quarantined", 0) or 0)
        status_superseded = int(events.get("status_superseded", 0) or 0)
        mu_events = status_promoted + status_rejected + status_quarantined + status_superseded

        self.metrics.lambda_events_window = submit_accepted
        self.metrics.mu_events_window = mu_events
        self.metrics.lambda_tps_window = float(physics.get("lambda_tps", 0.0) or 0.0)
        self.metrics.mu_tps_window = float(physics.get("mu_tps", 0.0) or 0.0)
        rho_value = physics.get("rho")
        self.metrics.rho_window = float(rho_value) if rho_value is not None else None
        if mu_events > 0:
            self.metrics.rho_sample_ok = True
            self.metrics.rho_sample_note = "ok"
        elif submit_accepted > 0:
            self.metrics.rho_sample_ok = False
            self.metrics.rho_sample_note = "no_mu_events"
        else:
            self.metrics.rho_sample_ok = False
            self.metrics.rho_sample_note = "window_empty"
        self.metrics.fallback_rate_window = float(physics.get("fallback_rate", 0.0) or 0.0)
        self.metrics.semantic_fallback_hits_window = int(
            events.get("semantic_fallback_hits", 0) or 0
        )
        by_language = physics.get("semantic_fallback", {}).get("by_language", {})
        if isinstance(by_language, dict):
            self.metrics.semantic_fallback_by_language = {
                str(language): int(count)
                for language, count in by_language.items()
                if isinstance(language, str)
            }

    async def run(self) -> ContinuousHarnessReport:
        if os.getenv("AETHER_HARNESS_RUN_ID"):
            self.run_id = os.getenv("AETHER_HARNESS_RUN_ID")

        await self.setup()
        started_at = time.time()
        started_perf = time.perf_counter()
        self._started_perf = started_perf
        shutdown_watch_task: asyncio.Task[Any] | None = None
        try:
            agent_start_idx = int(os.getenv("AETHER_HARNESS_AGENT_START_IDX", "0"))
            agent_count = int(os.getenv("AETHER_HARNESS_AGENT_COUNT", str(self.config.num_agents)))
            agent_tasks = [
                asyncio.create_task(self._agent_loop(f"agent_{i:04d}"))
                for i in range(agent_start_idx, agent_start_idx + agent_count)
            ]
            if self._shutdown_event is not None:
                shutdown_watch_task = asyncio.create_task(self._watch_shutdown_event())
            materialization_task: asyncio.Task[Any] | None = None
            admission_task: asyncio.Task[Any] | None = None
            submit_dispatch_tasks: list[asyncio.Task[Any]] = []
            if self.config.defer_conflict_detection and os.getenv("AETHER_HARNESS_DISABLE_MATERIALIZATION") != "1":
                materialization_task = asyncio.create_task(self._materialization_loop())
            if self.config.adaptive_intake_throttle:
                admission_task = asyncio.create_task(self._admission_controller_loop())
                dispatcher_workers = max(1, min(self.config.submit_worker_threads, self.config.max_inflight_submissions))
                submit_dispatch_tasks = [
                    asyncio.create_task(self._submit_dispatcher_loop())
                    for _ in range(dispatcher_workers)
                ]
            promotion_task = None
            if os.getenv("AETHER_HARNESS_DISABLE_PROMOTION") != "1":
                promotion_task = asyncio.create_task(self._promotion_loop())
            monitor_task = None
            if os.getenv("AETHER_HARNESS_DISABLE_MONITOR") != "1":
                monitor_task = asyncio.create_task(self._monitor_loop())

            try:
                await asyncio.wait_for(
                    self._submit_stop.wait(),
                    timeout=self.config.run_seconds,
                )
            except asyncio.TimeoutError:
                pass
            self._submit_stop.set()
            if self.config.adaptive_intake_throttle:
                for _ in range(self.config.num_agents * 2):
                    self._admission_permits.release()
            submit_shutdown_timeout = max(2.0, (self.config.agent_interval_max_ms / 1000.0) * 3.0)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*agent_tasks, return_exceptions=True),
                    timeout=submit_shutdown_timeout,
                )
            except asyncio.TimeoutError:
                for task in agent_tasks:
                    task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*agent_tasks, return_exceptions=True),
                        timeout=submit_shutdown_timeout,
                    )
                except asyncio.TimeoutError:
                    # Do not block report generation indefinitely on stuck submission tasks.
                    pass

            # Throughput SLO uses actual submit window when interrupted early.
            submit_elapsed = max(0.001, time.perf_counter() - started_perf)
            self.metrics.submit_phase_seconds = min(self.config.run_seconds, submit_elapsed)
            self._capture_physics_window()

            if self.config.drain_seconds > 0 and not self._shutdown_requested:
                try:
                    await asyncio.wait_for(
                        self._promote_stop.wait(),
                        timeout=self.config.drain_seconds,
                    )
                except asyncio.TimeoutError:
                    pass

            self._materialize_stop.set()
            self._promote_stop.set()
            self._monitor_stop.set()
            tasks: list[asyncio.Task[Any]] = []
            if promotion_task:
                tasks.append(promotion_task)
            if monitor_task:
                tasks.append(monitor_task)
            if admission_task:
                tasks.append(admission_task)
            tasks.extend(submit_dispatch_tasks)
            if materialization_task:
                tasks.append(materialization_task)
            shutdown_timeout = max(2.0, self.config.promotion_iteration_timeout_seconds * 2.0)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=shutdown_timeout,
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            completed_perf = time.perf_counter()
            self.metrics.total_phase_seconds = completed_perf - started_perf
            runtime_session, should_close = self._open_runtime_session()
            try:
                self.metrics.conflict_sets = self._count_conflict_sets(session=runtime_session)
                self.metrics.status_counts = self._status_counts(session=runtime_session)

                if os.getenv("AETHER_HARNESS_MULTIPROCESS_MASTER") == "1":
                    total_submitted = runtime_session.exec(
                        select(func.count(Proposal.id))
                        .where(Proposal.source_run_id == self.run_id)
                    ).one()
                    
                    promoted_count = runtime_session.exec(
                        select(func.count(Proposal.id))
                        .where(Proposal.source_run_id == self.run_id)
                        .where(Proposal.status == ProposalStatus.PROMOTED)
                    ).one()
                    
                    rejected_count = runtime_session.exec(
                        select(func.count(Proposal.id))
                        .where(Proposal.source_run_id == self.run_id)
                        .where(Proposal.status == ProposalStatus.REJECTED)
                    ).one()
                    
                    superseded_count = runtime_session.exec(
                        select(func.count(Proposal.id))
                        .where(Proposal.source_run_id == self.run_id)
                        .where(Proposal.status == ProposalStatus.SUPERSEDED)
                    ).one()
                    
                    quarantined_count = runtime_session.exec(
                        select(func.count(Proposal.id))
                        .where(Proposal.source_run_id == self.run_id)
                        .where(Proposal.status == ProposalStatus.QUARANTINED)
                    ).one()

                    self.metrics.submitted_success = total_submitted
                    self.metrics.submitted_total = total_submitted
                    self.metrics.proposals_promoted = promoted_count
                    self.metrics.proposals_completed = promoted_count + rejected_count + quarantined_count + superseded_count
                
                stmt = select(SystemMetricEvent.event_type, SystemMetricEvent.event_reason).where(
                    SystemMetricEvent.repo_id.in_([r.id for r in self.repos]),
                    SystemMetricEvent.event_type.in_(
                        [
                            SystemMetricEventType.BATCH_SIZE_K,
                            SystemMetricEventType.BATCH_GATE_FAIL,
                        ]
                    ),
                    SystemMetricEvent.source_run_id == self.run_id,
                )
                batch_sizes = []
                gate_fails = 0
                for event_type, event_reason in runtime_session.exec(stmt):
                    if event_type == SystemMetricEventType.BATCH_SIZE_K:
                        try:
                            reason_data = json.loads(event_reason)
                            if isinstance(reason_data, dict):
                                total_batch_size = reason_data.get("total_batch_size", 0)
                            else:
                                total_batch_size = int(reason_data)
                            batch_sizes.append(total_batch_size)
                        except Exception:
                            try:
                                batch_sizes.append(int(event_reason))
                            except (ValueError, TypeError):
                                pass
                    elif event_type == SystemMetricEventType.BATCH_GATE_FAIL:
                        gate_fails += 1
                if batch_sizes:
                    self.metrics.avg_batch_size_k = sum(batch_sizes) / len(batch_sizes)
                    self.metrics.batch_gate_failure_rate = gate_fails / len(batch_sizes)
                else:
                    self.metrics.avg_batch_size_k = 0.0
                    self.metrics.batch_gate_failure_rate = 0.0
            finally:
                if should_close:
                    runtime_session.close()
            self.metrics.backlog_end = (
                self.metrics.status_counts.get(ProposalStatus.INGESTED.value, 0)
                + self.metrics.status_counts.get(ProposalStatus.PENDING.value, 0)
                + self.metrics.status_counts.get(ProposalStatus.EVALUATING.value, 0)
            )
            self.metrics.proposals_promoted = max(
                self.metrics.proposals_promoted,
                self.metrics.status_counts.get(ProposalStatus.PROMOTED.value, 0),
            )
            self.metrics.proposals_completed = (
                self.metrics.status_counts.get(ProposalStatus.PROMOTED.value, 0)
                + self.metrics.status_counts.get(ProposalStatus.REJECTED.value, 0)
                + self.metrics.status_counts.get(ProposalStatus.QUARANTINED.value, 0)
                + self.metrics.status_counts.get(ProposalStatus.SUPERSEDED.value, 0)
            )
            self.metrics.submit_buckets_5m = self._build_submit_buckets(started_at)
            self.metrics.create_snapshot_quarantines = sum(
                int(bucket.get("create_snapshot_quarantines", 0) or 0)
                for bucket in self.metrics.submit_buckets_5m
            )
            self.metrics.materialize_gate_workspace_quarantines = sum(
                int(bucket.get("materialize_gate_workspace_quarantines", 0) or 0)
                for bucket in self.metrics.submit_buckets_5m
            )
            quarantine_reason_counts: Counter[str] = Counter()
            quarantine_reason_counts_exact: Counter[str] = Counter()
            quarantine_reason_examples: dict[str, str] = {}
            for _, event_type, event_reason in self._load_completion_events(started_at):
                if event_type != SystemMetricEventType.STATUS_QUARANTINED:
                    continue
                phase = self._quarantine_phase_from_reason(event_reason)
                quarantine_reason_counts[phase] += 1
                if event_reason:
                    quarantine_reason_counts_exact[event_reason] += 1
                    quarantine_reason_examples.setdefault(phase, event_reason)
            self.metrics.quarantine_reasons_by_phase = dict(quarantine_reason_counts)
            self.metrics.quarantine_reasons_exact = dict(quarantine_reason_counts_exact.most_common(10))
            self.metrics.quarantine_reason_example_by_phase = quarantine_reason_examples
            self.metrics.top_repeated_proposal_ids = dict(self._scanned_proposal_counter.most_common(20))
            self.metrics.top_repeated_conflict_set_ids = dict(self._scanned_conflict_set_counter.most_common(20))

            self._print_per_repo_summary()

            checks = self._evaluate_checks()
            passed = all(checks.values())
            
            # Swarm Harness Integrity Verification Checks
            integrity_verified = self.verify_integrity(runtime_session)

            if not integrity_verified:
                passed = False

            fingerprint_payload = self._build_config_fingerprint_payload()
            config_fingerprint = self._compute_config_fingerprint(fingerprint_payload)
            git_commit_sha, git_commit_short, git_dirty = self._resolve_git_metadata()

            return ContinuousHarnessReport(
                config=self.config,
                thresholds=self.thresholds,
                metrics=self.metrics,
                checks=checks,
                passed=passed,
                seed=self.seed,
                config_fingerprint=config_fingerprint,
                config_fingerprint_payload=fingerprint_payload,
                interrupted=self._shutdown_requested,
                git_commit_sha=git_commit_sha,
                git_commit_short=git_commit_short,
                git_dirty=git_dirty,
                started_at=started_at,
                completed_at=time.time(),
                mu_eff=self.metrics.mu_eff,
                avg_batch_size_k=self.metrics.avg_batch_size_k,
                batch_gate_failure_rate=self.metrics.batch_gate_failure_rate,
                integrity_verified=integrity_verified,
                integrity_reasons=getattr(self, "integrity_reasons", []),
            )
        finally:
            if shutdown_watch_task is not None:
                shutdown_watch_task.cancel()
                await asyncio.gather(shutdown_watch_task, return_exceptions=True)
            if self._api_client is not None:
                self._api_client.close()
            if getattr(self, "_async_api_client", None) is not None:
                await self._async_api_client.aclose()

    def _print_per_repo_summary(self) -> None:
        print("\n=== Per-Repository Summary ===")
        runtime_session, should_close = self._open_runtime_session()
        try:
            for repo in self.repos:
                assigned_agents = sum(
                    1 for i in range(self.config.num_agents)
                    if get_agent_repo_index(i, self.config.num_agents) == self.repos.index(repo)
                )
                
                submitted = runtime_session.exec(
                    select(func.count(Proposal.id))
                    .where(Proposal.repo_id == repo.id)
                ).one()
                
                promoted = runtime_session.exec(
                    select(func.count(Proposal.id))
                    .where(Proposal.repo_id == repo.id)
                    .where(Proposal.status == ProposalStatus.PROMOTED)
                ).one()

                superseded = runtime_session.exec(
                    select(func.count(Proposal.id))
                    .where(Proposal.repo_id == repo.id)
                    .where(Proposal.status == ProposalStatus.SUPERSEDED)
                ).one()
                
                backlog = runtime_session.exec(
                    select(func.count(Proposal.id))
                    .where(Proposal.repo_id == repo.id)
                    .where(Proposal.status.in_([ProposalStatus.INGESTED, ProposalStatus.PENDING, ProposalStatus.EVALUATING]))
                ).one()

                run_time = self.metrics.submit_phase_seconds or 1.0
                mu_eff = promoted / run_time
                
                print(f"  {repo.name} (ID: {repo.id[:8]}...):")
                print(f"    Assigned Agents: {assigned_agents}")
                print(f"    Submitted:       {submitted}")
                print(f"    Promoted:        {promoted} ({mu_eff:.2f} proposals/sec)")
                print(f"    Superseded:      {superseded}")
                print(f"    Active Backlog:  {backlog}")
        except Exception as e:
            print(f"Error printing per-repo summary: {e}")
        finally:
            if should_close:
                runtime_session.close()


async def run_continuous_swarm_harness(
    config: ContinuousHarnessConfig,
    thresholds: ContinuousHarnessThresholds | None = None,
    seed: int = 7,
    shutdown_event: threading.Event | None = None,
    db_path: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> ContinuousHarnessReport:
    """Run continuous swarm harness in an isolated in-memory environment."""
    resolved_thresholds = thresholds or ContinuousHarnessThresholds()

    if storage_path is None:
        storage_path = Path(tempfile.mkdtemp(prefix="aether_continuous_harness_"))
    else:
        storage_path = Path(storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)
        
    import os
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        cargs = {"timeout": 30} if db_url.startswith("sqlite") else {}
        engine = create_engine(db_url, connect_args=cargs)
    else:
        if db_path is None:
            db_path = storage_path / "harness.db"
        else:
            db_path = Path(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=30000;"))
            conn.execute(text("PRAGMA synchronous=NORMAL;"))
    if os.getenv("AETHER_HARNESS_SKIP_SETUP") != "1":
        SQLModel.metadata.create_all(engine)

    runtime_engine = engine
    use_separate_runtime_engine = os.getenv("AETHER_HARNESS_RUNTIME_SEPARATE_ENGINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if use_separate_runtime_engine:
        if db_url:
            pool_size = int(os.environ.get("DATABASE_MAX_CONNECTIONS", "5"))
            cargs = {"timeout": 30} if db_url.startswith("sqlite") else {}
            runtime_engine = create_engine(
                db_url,
                connect_args=cargs,
                pool_size=pool_size,
                max_overflow=10,
            )
        else:
            runtime_engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False, "timeout": 30},
                pool_size=5,
                max_overflow=10,
            )
        if runtime_engine.dialect.name == "sqlite":
            with runtime_engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL;"))
                conn.execute(text("PRAGMA busy_timeout=30000;"))
                conn.execute(text("PRAGMA synchronous=NORMAL;"))

    def _session_factory() -> Session:
        return Session(engine)

    def _runtime_session_factory() -> Session:
        return Session(runtime_engine)

    with Session(engine) as session:
        harness = ContinuousSwarmHarness(
            session=session,
            storage_path=storage_path,
            config=config,
            thresholds=resolved_thresholds,
            seed=seed,
            session_factory=_session_factory,
            runtime_session_factory=_runtime_session_factory if use_separate_runtime_engine else None,
            shutdown_event=shutdown_event,
        )
        return await harness.run()


def run_continuous_swarm_harness_sync(
    config: ContinuousHarnessConfig,
    thresholds: ContinuousHarnessThresholds | None = None,
    seed: int = 7,
    shutdown_event: threading.Event | None = None,
    db_path: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> ContinuousHarnessReport:
    """Synchronous wrapper for CLI and scripts."""
    return asyncio.run(
        run_continuous_swarm_harness(
            config=config,
            thresholds=thresholds,
            seed=seed,
            shutdown_event=shutdown_event,
            db_path=db_path,
            storage_path=storage_path,
        )
    )
