"""
Promotion engine for selecting and promoting proposals.

Phase 1 Enhanced Implementation:
- Probabilistic scoring based on confidence + evidence
- Conflict set resolution with parallel top-k evaluation
- Deterministic policy enforcement (gates must pass)
- Head advancement with audit trail
- Tie-breaking by submission time for equal scores
- Excluded proposal tracking
"""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
import time
import traceback
from typing import Any, Optional
import structlog

class ReentrantAsyncLock:
    """A reentrant async lock to serialize promotions safely without deadlocks."""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    async def acquire(self):
        current_task = asyncio.current_task()
        if self._owner == current_task:
            self._depth += 1
            return True

        await self._lock.acquire()
        self._owner = current_task
        self._depth = 1
        return True

    def release(self):
        current_task = asyncio.current_task()
        if self._owner != current_task:
            raise RuntimeError("Cannot release a lock owned by another task")
        
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def release_all(self) -> int:
        """Release the lock fully regardless of depth, returning depth to restore."""
        current_task = asyncio.current_task()
        if self._owner != current_task:
            return 0
        depth = self._depth
        self._depth = 0
        self._owner = None
        self._lock.release()
        return depth

    async def acquire_all(self, depth: int):
        """Restore the lock owner and depth."""
        if depth == 0:
            return
        await self._lock.acquire()
        self._owner = asyncio.current_task()
        self._depth = depth

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


from sqlmodel import Session
from sqlmodel import select

from ..models.entities import (
    Proposal,
    ProposalStatus,
    Promotion,
    Repository,
    GateConfig,
    GateResult,
    GateStatus,
    ConflictSet,
    OperationGraphNode,
    AetherHead,
    generate_id,
    SystemMetricEventType,
    ContentionWinner,
)
from ..models.events import EventType
from ..crdt.document_store import DocumentStore
from .event_bus import EventBus, create_head_event
from .proposal_service import ProposalService
from .snapshot_service import SnapshotService
from .gate_runner import GateRunner, DEFAULT_GATES
from .trust_service import TrustService

logger = structlog.get_logger()


@dataclass
class ScoringWeights:
    """Weights for the scoring function."""
    confidence: float = 0.3  # α - agent confidence weight
    tests: float = 0.5  # β - test evidence weight
    static: float = 0.2  # γ - static analysis weight


@dataclass
class CohortResult:
    promotion: Optional[Promotion]
    proposal_outcomes: list[dict[str, Any]]


@dataclass
class PromotionCandidate:
    """A candidate for promotion."""
    proposals: list[Proposal]
    score: float
    evidence: dict[str, str]
    gate_results: list[GateResult]
    snapshot_id: str
    timings: dict[str, float] = field(default_factory=dict)
    # Phase 1: Track excluded proposals for audit
    excluded_proposal_ids: list[str] = field(default_factory=list)
    # Timestamp for tie-breaking
    earliest_submission: Optional[datetime] = None
    is_rebased: bool = False

    def __post_init__(self):
        """Set earliest submission time from proposals."""
        if self.proposals and not self.earliest_submission:
            self.earliest_submission = min(p.submitted_at for p in self.proposals)


@dataclass
class EvaluationResult:
    """Result of evaluating a single proposal (for parallel evaluation)."""
    proposal: Proposal
    candidate: Optional[PromotionCandidate]
    error: Optional[str] = None
    success: bool = True


class GateTimeoutError(RuntimeError):
    """Raised when a gate run times out for a candidate evaluation."""


class PromotionPhaseError(RuntimeError):
    """Raised when a promotion phase fails so callers can pinpoint the hot path."""

    def __init__(self, phase: str, original: Exception):
        self.phase = phase
        self.original = original
        super().__init__(f"{phase}: {type(original).__name__}: {original}")


class EvaluationPhaseError(RuntimeError):
    """Raised when proposal evaluation fails so long-run buckets expose the exact phase."""

    def __init__(self, phase: str, original: Exception):
        self.phase = phase
        self.original = original
        super().__init__(f"{phase}: {type(original).__name__}: {original}")


class ExternalCommitAbortedError(RuntimeError):
    pass


class ContentionWinnerExistsError(ValueError):
    """Raised when a temporal contention winner already exists for a group."""
    def __init__(self, message: str, winner_id: str, contention_group_id: str, contention_policy: str, target_paths: list[str]):
        super().__init__(message)
        self.winner_id = winner_id
        self.contention_group_id = contention_group_id
        self.contention_policy = contention_policy
        self.target_paths = target_paths


class OracleSufficiencyError(ValueError):
    """Raised when a candidate passes required gates but fails golden semantic assertions."""
    pass


class PromotionEngine:
    """
    Engine for evaluating and promoting proposals.

    Core responsibilities:
    1. Evaluate pending proposals against gates
    2. Score candidates based on confidence + evidence
    3. Select best candidate (or batch) for promotion
    4. Advance canonical head while maintaining buildability
    """

    def __init__(
        self,
        session: Session,
        event_bus: EventBus,
        proposal_service: ProposalService,
        snapshot_service: SnapshotService,
        gate_runner: GateRunner,
        storage_path: Path,
        gate_execution_mode: str = "fast_fail",
        gate_max_parallel: int = 4,
    ):
        self.session = session
        self.event_bus = event_bus
        self.proposal_service = proposal_service
        self.snapshot_service = snapshot_service
        self.gate_runner = gate_runner
        self.storage_path = storage_path
        mode = gate_execution_mode.strip().lower()
        if mode not in {"sequential", "fast_fail", "parallel"}:
            raise ValueError(f"Unsupported gate execution mode: {gate_execution_mode}")
        self.gate_execution_mode = mode
        self.gate_max_parallel = max(1, gate_max_parallel)
        self.weights = ScoringWeights()
        self.trust_service = TrustService(session)
        self._last_iteration_timing: dict[str, float] = {}
        self._last_promotion_phase_timing: dict[str, float] = {}
        self._promotion_lock = ReentrantAsyncLock()
        self._active_pg_lock_key: Optional[int] = None
        # Feature flag: default to False
        self.semantic_merge_enabled = os.getenv("AETHER_SEMANTIC_MERGE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        self.synthesis_worker_enabled = os.getenv("AETHER_SYNTHESIS_WORKER_ENABLED", "false").lower() in ("1", "true", "yes", "on")

    def get_last_iteration_timing(self) -> dict[str, float]:
        """Return timing breakdown for the latest promotion iteration."""
        return dict(self._last_iteration_timing)

    def get_last_promotion_phase_timing(self) -> dict[str, float]:
        """Return timing breakdown for the latest promotion execution."""
        return dict(self._last_promotion_phase_timing)

    def get_repo_gates(self, repo_id: str) -> list[GateConfig]:
        """Get gate configuration for a repository."""
        repo = self.session.get(Repository, repo_id)
        if not repo or not repo.gate_config:
            return DEFAULT_GATES

        try:
            config = json.loads(repo.gate_config)
            if "gates" in config and config["gates"]:
                return [GateConfig(**g) for g in config["gates"]]
        except Exception as e:
            logger.error("failed_to_parse_repo_gates", error=str(e), gate_config=repo.gate_config)

        return DEFAULT_GATES

    def get_repo_golden_assertions(self, repo_id: str) -> dict:
        """Get golden assertions from the repository gate configuration."""
        repo = self.session.get(Repository, repo_id)
        if not repo or not repo.gate_config:
            return {}

        try:
            config = json.loads(repo.gate_config)
            return config.get("golden_assertions", {})
        except Exception as e:
            logger.error("failed_to_parse_golden_assertions", error=str(e))
            return {}

    def derive_pg_lock_key(self, proposals: list[Proposal]) -> int:
        """Derive a signed 64-bit integer key from the contention unit."""
        conflict_set_id = None
        for p in proposals:
            if p.conflict_set_id:
                conflict_set_id = p.conflict_set_id
                break
        if conflict_set_id:
            key_str = f"conflict_set:{conflict_set_id}"
        else:
            paths = set()
            for p in proposals:
                if p.affected_paths:
                    try:
                        p_paths = json.loads(p.affected_paths)
                        if isinstance(p_paths, list):
                            paths.update(p_paths)
                    except Exception:
                        pass
            if paths:
                key_str = f"paths:{','.join(sorted(list(paths)))}"
            else:
                key_str = f"proposals:{','.join(sorted([p.id for p in proposals]))}"
        
        hasher = hashlib.sha256(key_str.encode("utf-8"))
        val = int(hasher.hexdigest()[:16], 16)
        if val >= 0x8000000000000000:
            val -= 0x10000000000000000
        return val

    async def _acquire_contention_lock(self, proposals: list[Proposal]) -> Optional[int]:
        """Acquire a Postgres advisory lock based on proposals."""
        if not self.session.bind or self.session.bind.dialect.name != "postgresql":
            return None
        lock_key = self.derive_pg_lock_key(proposals)
        await self._acquire_contention_lock_by_key(lock_key)
        return lock_key

    async def _acquire_contention_lock_by_key(self, lock_key: int) -> None:
        """Acquire a Postgres advisory lock by key with retry and jitter."""
        if not self.session.bind or self.session.bind.dialect.name != "postgresql":
            return
        
        logger.info("acquiring_pg_advisory_lock", lock_key=lock_key)
        
        from sqlmodel import text
        max_attempts = 15
        base_delay = 0.05
        max_delay = 0.5
        
        for attempt in range(max_attempts):
            try:
                result = self.session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": lock_key}
                )
                locked = result.scalar()
                if locked:
                    logger.info("pg_advisory_lock_acquired", lock_key=lock_key, attempt=attempt)
                    self._active_pg_lock_key = lock_key
                    return
                else:
                    try:
                        self.session.rollback()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("pg_advisory_lock_attempt_failed", lock_key=lock_key, attempt=attempt, error=str(e))
                try:
                    self.session.rollback()
                except Exception:
                    pass
            
            if attempt < max_attempts - 1:
                import random
                delay = min(max_delay, base_delay * (2 ** attempt))
                jittered_delay = delay * (0.5 + random.random())
                await asyncio.sleep(jittered_delay)
                
        raise RuntimeError(f"lock_acquisition_timeout: Failed to acquire PostgreSQL advisory lock for key {lock_key}")

    async def _release_contention_lock(self) -> None:
        """Release the Postgres advisory lock by rolling back the session transaction."""
        self._active_pg_lock_key = None
        if not self.session.bind or self.session.bind.dialect.name != "postgresql":
            return
        try:
            self.session.rollback()
            logger.info("pg_advisory_lock_released")
        except Exception as e:
            logger.warning("pg_advisory_lock_release_failed", error=str(e))

    @staticmethod
    def _find_timed_out_gate(gate_results: list[GateResult]) -> str | None:
        """Return gate name when any gate result indicates a timeout."""
        for gate_result in gate_results:
            if gate_result.status != GateStatus.ERROR:
                continue
            stderr = (gate_result.stderr or "").lower()
            if "timed out" in stderr:
                return gate_result.gate_name
        return None

    @staticmethod
    def _format_exception_reason(exc: Exception, *, prefix: str) -> str:
        phase = None
        root_exc = exc
        if isinstance(exc, (PromotionPhaseError, EvaluationPhaseError)):
            phase = exc.phase
            if exc.__cause__ is not None:
                root_exc = exc.__cause__
            else:
                root_exc = exc.original

        trace = traceback.extract_tb(root_exc.__traceback__)
        if trace:
            frame = trace[-1]
            location = f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}"
            if phase:
                return f"{prefix}:{phase}: {type(root_exc).__name__}: {root_exc} @ {location}"[:200]
            return f"{prefix}: {type(root_exc).__name__}: {root_exc} @ {location}"[:200]
        if phase:
            return f"{prefix}:{phase}: {type(root_exc).__name__}: {root_exc}"[:200]
        return f"{prefix}: {type(root_exc).__name__}: {root_exc}"[:200]

    def _is_time_budget_exceeded(
        self,
        started_at: float,
        max_processing_seconds: Optional[float],
    ) -> bool:
        """Return True when the promotion iteration exhausted its time budget."""
        if max_processing_seconds is None:
            return False
        return (time.perf_counter() - started_at) >= max_processing_seconds

    def _get_proposal_symbols(self, proposal: Proposal) -> set[str]:
        """Decode proposal's affected symbols and return them as set of path::node_id."""
        raw = self.proposal_service._decode_affected_symbols(proposal.affected_symbols)
        if not raw:
            return set()
        symbols = set()
        for path, node_ids in raw.items():
            for node_id in node_ids:
                symbols.add(f"{path}::{node_id}")
        return symbols

    def _is_structurally_mergeable(self, p1: Proposal, p2: Proposal) -> bool:
        """
        Check if two proposals are eligible for Class 1 structural merge.
        Under strict rules:
          - Whitelist is class_definition only.
          - Symbol ID format must be CLASS:<classname>::FUNC:<methodname> or similar.
          - Common overlapping symbols must ONLY consist of parent CLASS:<classname> symbols (no method-level collisions).
        """
        from .structural_merge import is_class_member_node
        
        p1_decoded = self.proposal_service._decode_affected_symbols(p1.affected_symbols)
        p2_decoded = self.proposal_service._decode_affected_symbols(p2.affected_symbols)
        
        if not p1_decoded or not p2_decoded:
            return False
        
        # Check if they overlap on any path.
        common_paths = set(p1_decoded.keys()).intersection(p2_decoded.keys())
        if not common_paths:
            return True  # No path overlap, so they don't overlap on symbols either
            
        for path in common_paths:
            p1_symbols = set(p1_decoded[path])
            p2_symbols = set(p2_decoded[path])
            
            common_symbols = p1_symbols.intersection(p2_symbols)
            if common_symbols:
                # Overlapping symbols are allowed only if they are parent CLASS:<classname> symbols.
                for sym in common_symbols:
                    if not sym.startswith("CLASS:") or "::" in sym:
                        return False
                        
        # Now verify all symbols modified in both proposals are whitelisted.
        for path, symbols in p1_decoded.items():
            for sym in symbols:
                if sym.startswith("CLASS:") and "::" in sym:
                    if not is_class_member_node(sym):
                        return False
                elif not sym.startswith("CLASS:"):
                    return False
                    
        for path, symbols in p2_decoded.items():
            for sym in symbols:
                if sym.startswith("CLASS:") and "::" in sym:
                    if not is_class_member_node(sym):
                        return False
                elif not sym.startswith("CLASS:"):
                    return False
                    
        return True

    async def attempt_semantic_merge_synthesis(
        self,
        repo_id: str,
        path: str,
        proposal: Proposal,
        base_content: str,
        current_content: str,
        content_b: str,
    ) -> Optional[str]:
        """
        Attempt to resolve a Class 2 semantic-merge conflict on a class member
        by invoking the standalone synthesis worker CLI.
        """
        if not self.synthesis_worker_enabled:
            return None

        # Parse documents using StructuredDocument
        from .structural_merge import StructuredDocument, is_class_member_node, get_modified_nodes, RGADocument
        base_doc = StructuredDocument("base")
        base_doc.set_content(base_content)

        doc_a = StructuredDocument("replica_a")
        doc_a.set_content(current_content)

        doc_b = StructuredDocument("replica_b")
        doc_b.set_content(content_b)

        # Check for parser errors
        if "__raw__" in base_doc.nodes or "__raw__" in doc_a.nodes or "__raw__" in doc_b.nodes:
            logger.info("synthesis_ineligible: parse_failed_or_raw_fallback")
            return None

        mod_a = get_modified_nodes(base_doc, doc_a)
        mod_b = get_modified_nodes(base_doc, doc_b)

        # We must have a direct overlap on class member node(s)
        overlap = mod_a.intersection(mod_b)
        if not overlap:
            return None

        # Track 3 v1: exactly one symbol overlap is supported, and it must be a class member
        if len(overlap) != 1:
            logger.info("synthesis_ineligible: multiple_overlapping_symbols", count=len(overlap))
            return None

        symbol_id = list(overlap)[0]
        if not is_class_member_node(symbol_id):
            logger.info("synthesis_ineligible: non_class_member_symbol", symbol=symbol_id)
            return None

        # Get parent symbol ID (e.g. CLASS:User)
        parts = symbol_id.split("::")
        parent_symbol_id = parts[0]

        # Extract node texts
        base_node_text = base_doc.nodes[symbol_id].get_text()
        left_node_text = doc_a.nodes[symbol_id].get_text()
        right_node_text = doc_b.nodes[symbol_id].get_text()

        # Build SynthesisRequestV1
        import uuid
        import hashlib
        from datetime import datetime, UTC
        from pathlib import Path
        from ..models.synthesis import (
            SynthesisRequestV1,
            ContractConstraintsV1,
            VerificationContextV1,
            GenerationPolicyV1,
            ModelPolicyV1,
            RequestProvenanceV1,
        )

        job_id = f"syn_{uuid.uuid4().hex[:12]}"
        
        # Calculate request hash from body fields
        request_body_str = f"{job_id}:{repo_id}:{path}:{symbol_id}:{base_node_text}:{left_node_text}:{right_node_text}"
        request_hash = hashlib.sha256(request_body_str.encode("utf-8")).hexdigest()

        prov = RequestProvenanceV1(
            request_created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            request_creator="aether-promotion-engine",
            policy_version="phase4-track3-v1",
            request_hash=request_hash
        )

        constraints = ContractConstraintsV1(
            must_preserve_signature=True,
            must_preserve_return_annotation=True,
            must_not_modify_imports=False,
            must_not_modify_tests=True,
            must_not_modify_manifest=True
        )

        # Verification targets
        test_targets = []
        lint_targets = []
        typecheck_targets = []
        try:
            from .gate_manifest import load_gate_manifest
            repo = self.session.get(Repository, repo_id)
            repo_root = Path(repo.workspace_path) if (repo and repo.workspace_path) else Path.cwd()
            manifest_res = load_gate_manifest(repo_root, [path])
            if manifest_res.selection_source != "fail_closed":
                for entry in manifest_res.entries:
                    test_targets.extend(entry.test_targets)
                    lint_targets.extend(entry.lint_targets)
                    typecheck_targets.extend(entry.typecheck_targets)
        except Exception:
            pass

        context = VerificationContextV1(
            gate_test_selection="manifest" if test_targets else "file_fallback",
            test_targets=test_targets,
            lint_targets=lint_targets,
            typecheck_targets=typecheck_targets
        )

        policy = GenerationPolicyV1(
            max_candidates=3,
            timeout_seconds=120,
            max_prompt_tokens=24000,
            max_completion_tokens=6000,
            temperature=0.2,
            seed=12345,
            output_format="replacement_node"
        )

        model_policy = ModelPolicyV1(
            provider="configured_provider",
            model="configured_model",
            allow_remote_model=False,
            prompt_template_version="phase4-synthesis-v1"
        )

        repo = self.session.get(Repository, repo_id)
        current_head = repo.head_id if repo else "head_unknown"

        request = SynthesisRequestV1(
            job_id=job_id,
            repo_id=repo_id,
            language="python",
            file_path=path,
            base_head_id=proposal.base_head_id or "head_unknown",
            current_head_id=current_head or "head_unknown",
            conflict_class="class2_isomorphic_mutation",
            symbol_id=symbol_id,
            parent_symbol_id=parent_symbol_id,
            base_node_hash=hashlib.sha256(base_node_text.encode("utf-8")).hexdigest(),
            left_proposal_id="prop_left_dummy",
            right_proposal_id=proposal.id,
            base_node_text=base_node_text,
            left_node_text=left_node_text,
            right_node_text=right_node_text,
            left_diff="",
            right_diff="",
            allowed_paths=[path],
            protected_paths=[
                ".aether/test_manifest.json",
                "pyproject.toml",
                "pytest.ini",
                "mypy.ini",
                ".github/workflows/*"
            ],
            contract_constraints=constraints,
            verification_context=context,
            generation_policy=policy,
            model_policy=model_policy,
            provenance=prov
        )

        # Release locks and database transaction state to comply with ADR
        lock_key = getattr(self, "_active_pg_lock_key", None)
        depth = self._promotion_lock.release_all()
        self.session.rollback()
        
        try:
            from .synthesis_adapter import SynthesisAdapter
            adapter = SynthesisAdapter()
            resp_status, resp = await adapter.invoke_worker(request, timeout_seconds=120)
        finally:
            await self._promotion_lock.acquire_all(depth)
            if lock_key is not None:
                await self._acquire_contention_lock_by_key(lock_key)

        if resp_status != "candidate_ready" or not resp or not resp.candidates:
            logger.warning("synthesis_worker_did_not_produce_candidate", status=resp_status)
            return None

        # Build merged document applying the synthesized candidate replacement
        candidate_text = resp.candidates[0].candidate_text
        merged_doc = StructuredDocument("merged")
        
        # Initialize with base nodes
        for n_id in base_doc.list_nodes():
            doc = RGADocument("merged")
            doc.set_text(base_doc.nodes[n_id].get_text())
            merged_doc.nodes[n_id] = doc
            merged_doc.node_order.append(n_id)

        # Helper to insert class member deterministically
        def insert_class_member(n_id: str, content: str):
            parts = n_id.split("::")
            class_prefix = parts[0]
            head_id = f"{class_prefix}::__head__"
            class_methods = []
            for existing_id in merged_doc.node_order:
                if existing_id.startswith(f"{class_prefix}::") and existing_id != head_id:
                    class_methods.append(existing_id)
            if n_id not in class_methods:
                class_methods.append(n_id)
            class_methods.sort()

            start_idx = -1
            if head_id in merged_doc.node_order:
                start_idx = merged_doc.node_order.index(head_id)
            elif class_prefix in merged_doc.node_order:
                start_idx = merged_doc.node_order.index(class_prefix)
            
            if start_idx == -1:
                merged_doc.update_node(n_id, content)
                return

            for m_id in class_methods:
                if m_id in merged_doc.node_order:
                    merged_doc.node_order.remove(m_id)
            
            for i, m_id in enumerate(class_methods):
                merged_doc.node_order.insert(start_idx + 1 + i, m_id)
                
            merged_doc.update_node(n_id, content)

        # Apply the synthesized candidate text to the conflicting symbol node
        merged_doc.update_node(symbol_id, candidate_text)

        # Apply other non-conflicting modified nodes from mod_a
        for n_id in mod_a:
            if n_id == symbol_id:
                continue
            content = doc_a.nodes[n_id].get_text()
            if n_id in merged_doc.nodes:
                merged_doc.update_node(n_id, content)
            else:
                insert_class_member(n_id, content)

        # Apply other non-conflicting modified nodes from mod_b
        for n_id in mod_b:
            if n_id == symbol_id:
                continue
            content = doc_b.nodes[n_id].get_text()
            if n_id in merged_doc.nodes:
                merged_doc.update_node(n_id, content)
            else:
                insert_class_member(n_id, content)

        try:
            return merged_doc.get_full_text()
        except Exception as e:
            logger.error("synthesis_merge_reconstruction_failed", error=str(e))
            return None

    def _find_temporal_conflicts(self, repo_id: str, proposal: Proposal) -> list[Proposal]:
        """Find recently promoted proposals that touch overlapping symbols since the proposal's base head."""
        p_symbols = self._get_proposal_symbols(proposal)
        if not p_symbols:
            return []

        from sqlmodel import select
        statement = select(Promotion).where(
            Promotion.repo_id == repo_id,
            Promotion.to_head_sequence > proposal.base_head_sequence
        )
        proms = self.session.exec(statement).all()
        
        promoted_ids = []
        for prom in proms:
            try:
                ids = json.loads(prom.included_proposals)
                if isinstance(ids, list):
                    promoted_ids.extend(ids)
            except Exception:
                continue
                
        if not promoted_ids:
            return []

        promoted_proposals = self.session.exec(
            select(Proposal).where(Proposal.id.in_(promoted_ids))
        ).all()
        
        conflicting = []
        for prom_p in promoted_proposals:
            prom_symbols = self._get_proposal_symbols(prom_p)
            if p_symbols.intersection(prom_symbols):
                conflicting.append(prom_p)
                
        return conflicting

    def _check_fallback_temporal_contention(self, repo_id: str, proposal: Proposal) -> Optional[Proposal]:
        """
        Query already promoted proposals for the same repo_id, check if they are in contention mode
        and share overlapping paths or symbols, to prevent temporal races.
        Only considers winners promoted AFTER the incoming proposal's base head.
        """
        p_paths = set(self.proposal_service._safe_json_loads(proposal.affected_paths, []))
        p_symbols = self._get_proposal_symbols(proposal)
        
        from sqlmodel import select
        from packages.aether.models.entities import Promotion
        
        # Query promoted proposals
        statement = select(Proposal).where(
            Proposal.repo_id == repo_id,
            Proposal.status == ProposalStatus.PROMOTED
        )
        promoted = self.session.exec(statement).all()
        for prom_p in promoted:
            # Check if this promoted proposal was also in contention mode
            cont = self.proposal_service.get_contention_metadata(prom_p.id)
            if not cont or not cont.contention_mode:
                continue
                
            # Only consider if promoted AFTER the incoming proposal's base head sequence
            prom_stmt = select(Promotion).where(
                Promotion.repo_id == repo_id,
                Promotion.included_proposals.like(f'%"{prom_p.id}"%')
            )
            promotion_record = self.session.exec(prom_stmt).first()
            promoted_seq = promotion_record.to_head_sequence if promotion_record else (prom_p.base_head_sequence + 1)
            
            if promoted_seq <= proposal.base_head_sequence:
                continue
                
            prom_paths = set(self.proposal_service._safe_json_loads(prom_p.affected_paths, []))
            # Path overlap
            if p_paths.intersection(prom_paths):
                return prom_p
                
            # Symbol overlap
            if p_symbols:
                prom_symbols = self._get_proposal_symbols(prom_p)
                if prom_symbols and p_symbols.intersection(prom_symbols):
                    return prom_p
                    
        return None

    def _get_proposal_content(self, proposal: Proposal, path: str, document_store: DocumentStore) -> str:
        """Fetch the fully applied file content for a proposal starting from its base head snapshot."""
        if not proposal.base_head_id:
            return document_store.get_file_content(path) or ""
            
        base_snap_path = self.snapshot_service.get_snapshot_path(proposal.base_head_id)
        if not base_snap_path:
            return document_store.get_file_content(path) or ""

        from ..crdt.document_store import DocumentStore as DS
        base_store = DS(proposal.repo_id, base_path=str(base_snap_path))
        base_content = self.snapshot_service.get_file_content(proposal.base_head_id, path) or ""
        file_doc = base_store.get_document(path)
        if file_doc.structured:
            file_doc.structured.set_content(base_content)
        base_store._replace_document_text(file_doc, base_content)

        if proposal.change_type == "unified_diff":
            base_store.apply_diff_batch([(proposal.change_content, proposal.id)])
        elif proposal.change_type == "full_file":
            base_store.apply_full_file(path, proposal.change_content, proposal.id)

        return base_store.get_file_content(path) or ""





    def _recover_stale_evaluating(
        self,
        repo_id: str,
        stale_after_seconds: float,
    ) -> int:
        """
        Requeue stale EVALUATING proposals left behind by interrupted iterations.

        This keeps the queue live if a previous iteration was interrupted mid-flight.
        """
        if stale_after_seconds <= 0:
            return 0

        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        statement = select(Proposal).where(
            Proposal.repo_id == repo_id,
            Proposal.status == ProposalStatus.EVALUATING,
        )
        evaluating = list(self.session.exec(statement))

        recovered = 0
        for proposal in evaluating:
            evaluated_at = proposal.evaluated_at
            if evaluated_at and evaluated_at.tzinfo is None:
                evaluated_at = evaluated_at.replace(tzinfo=UTC)

            if not evaluated_at or evaluated_at <= cutoff:
                proposal.status = ProposalStatus.PENDING
                proposal.evaluated_at = None
                self.session.add(proposal)
                recovered += 1

        if recovered:
            self.session.commit()

        return recovered

    def _build_provenance_evidence(
        self,
        *,
        repo_id: str,
        candidate: PromotionCandidate,
        from_head_id: Optional[str],
        from_head_sequence: int,
        to_head_sequence: int,
        from_content_hash: str,
        to_content_hash: str,
        triggered_by: str,
    ) -> dict[str, str]:
        """
        Build deterministic provenance fields for the promotion evidence blob.

        Stored in evidence to avoid schema migrations while still enabling replay
        and operation-graph style lineage checks.
        """
        proposal_vector: list[dict[str, Any]] = []
        for proposal in sorted(candidate.proposals, key=lambda p: p.id):
            proposal_vector.append(
                {
                    "id": proposal.id,
                    "agent_id": proposal.agent_id,
                    "task_id": proposal.task_id,
                    "change_type": proposal.change_type,
                    "base_head_id": proposal.base_head_id,
                    "base_head_sequence": proposal.base_head_sequence,
                    "affected_paths": proposal.affected_paths,
                }
            )

        gate_vector = sorted(
            f"{result.gate_name}:{result.status.value}"
            for result in candidate.gate_results
        )
        graph_payload = {
            "repo_id": repo_id,
            "parent_head_id": from_head_id,
            "parent_sequence": from_head_sequence,
            "to_sequence": to_head_sequence,
            "snapshot_id": candidate.snapshot_id,
            "score": round(candidate.score, 8),
            "triggered_by": triggered_by,
            "proposals": proposal_vector,
            "excluded_proposals": sorted(candidate.excluded_proposal_ids),
            "gates": gate_vector,
            "from_content_hash": from_content_hash,
            "to_content_hash": to_content_hash,
        }
        canonical_payload = json.dumps(graph_payload, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        parent = f"{from_head_id}@{from_head_sequence}" if from_head_id else f"root@{from_head_sequence}"
        return {
            "_op_graph_parent": parent,
            "_op_graph_node": payload_hash,
            "_op_graph_payload_sha256": payload_hash,
            "_op_graph_kind": "promotion",
            "_replay_snapshot_id": candidate.snapshot_id,
            "_replay_from_hash": from_content_hash,
            "_replay_to_hash": to_content_hash,
            "_replay_gate_vector": "|".join(gate_vector),
        }

    def _resolve_parent_operation_node(
        self,
        *,
        repo_id: str,
        from_head_id: Optional[str],
        from_head_sequence: int,
    ) -> Optional[OperationGraphNode]:
        """Find the most recent operation node that produced the parent head."""
        statement = select(OperationGraphNode).where(
            OperationGraphNode.repo_id == repo_id,
            OperationGraphNode.to_head_sequence == from_head_sequence,
        )
        if from_head_id:
            statement = statement.where(OperationGraphNode.to_head_id == from_head_id)
        statement = statement.order_by(OperationGraphNode.created_at.desc())
        return self.session.exec(statement).first()

    def compute_score(
        self,
        proposal: Proposal,
        gate_results: list[GateResult],
        gates: list[GateConfig],
    ) -> tuple[float, float, float]:
        """
        Compute promotion score for a proposal.

        Returns: (final_score, policy_score, confidence_score)

        Score formula:
            score = S_policy * (α * p0 + β * E_tests + γ * E_static)
        where:
            S_policy = 1.0 if all required gates pass, 0.0 otherwise
            p0 = calibrated prior confidence
            E_tests = test pass rate
            E_static = static analysis pass rate
        """
        # Policy score: binary - all required gates must pass
        required_gates = {g.name for g in gates if g.required}
        result_map = {r.gate_name: r for r in gate_results}

        policy_score = 1.0
        for gate_name in required_gates:
            if gate_name not in result_map:
                policy_score = 0.0
                break
            if result_map[gate_name].status not in (GateStatus.PASS, GateStatus.SKIP):
                policy_score = 0.0
                break

        # Confidence score (from agent) - ADJUSTED BY TRUST
        raw_confidence = proposal.confidence_score
        confidence = self.trust_service.get_combined_agent_score(proposal.agent_id, raw_confidence)

        # Test evidence score
        test_results = [r for r in gate_results if "test" in r.gate_name.lower()]
        if test_results:
            test_passes = sum(1 for r in test_results if r.status == GateStatus.PASS)
            e_tests = test_passes / len(test_results)
        else:
            e_tests = 0.5  # Neutral if no tests

        # Static analysis evidence
        static_results = [
            r for r in gate_results
            if r.gate_name in ("lint", "syntax", "security")
        ]
        if static_results:
            static_passes = sum(1 for r in static_results if r.status == GateStatus.PASS)
            e_static = static_passes / len(static_results)
        else:
            e_static = 0.5  # Neutral if no static analysis

        # Compute weighted score
        raw_score = (
            self.weights.confidence * confidence +
            self.weights.tests * e_tests +
            self.weights.static * e_static
        )

        # Final score is policy-gated
        final_score = policy_score * raw_score

        return final_score, policy_score, raw_score

    @staticmethod
    def _parse_affected_paths(proposal: Proposal) -> list[str]:
        """Decode proposal affected paths with defensive fallback."""
        try:
            raw_paths = json.loads(proposal.affected_paths)
        except Exception:
            return []
        if not isinstance(raw_paths, list):
            return []
        return [path for path in raw_paths if isinstance(path, str) and path]

    async def _apply_proposal_change(
        self,
        document_store: DocumentStore,
        proposal: Proposal,
        was_rebasing: bool = False,
    ) -> list[str]:
        """Apply one proposal mutation into a document store clone/live view."""
        proposal_id = proposal.id
        change_type = proposal.change_type
        change_content = proposal.change_content
 
        if change_type is None:
            raise ValueError(f"Unsupported proposal change_type: None for proposal {proposal_id}")
        if change_content is None:
            raise ValueError(f"Proposal {proposal_id} change_content cannot be None")
 
        if change_type == "unified_diff":
            paths, _ = await asyncio.to_thread(
                document_store.apply_diff,
                change_content,
                proposal_id,
            )
            return paths
 
        if change_type == "full_file":
            affected_paths = self._parse_affected_paths(proposal)
            if not affected_paths:
                raise ValueError(f"Proposal {proposal_id} full_file requires affected_paths")
            
            cont_meta = self.proposal_service.get_contention_metadata(proposal_id)
            is_semantic_merge = (
                self.semantic_merge_enabled
                and cont_meta is not None
                and cont_meta.contention_policy == "semantic_merge"
            )

            if was_rebasing and is_semantic_merge:
                from .structural_merge import attempt_structural_merge
                for path in affected_paths:
                    base_content = self.snapshot_service.get_file_content(proposal.base_head_id, path) or ""
                    current_content = document_store.get_file_content(path) or ""
                    
                    if current_content.strip() != base_content.strip() and base_content.strip() != "":
                        # Attempt Class 1 structural merge
                        merged, failure_reason = attempt_structural_merge(base_content, current_content, change_content)
                        if merged is not None:
                            logger.info("rebase_structural_merge_applied", proposal_id=proposal_id, path=path)
                            document_store.apply_full_file(path, merged, proposal_id)
                            self.proposal_service._record_metric_event(
                                event_type=SystemMetricEventType.STRUCTURAL_MERGE,
                                repo_id=proposal.repo_id,
                                proposal_id=proposal_id,
                                reason="structural_merge_applied",
                                path=path,
                            )
                            continue
                        
                        # Attempt Class 2 synthesis merge
                        if self.synthesis_worker_enabled:
                            merged_synth = await self.attempt_semantic_merge_synthesis(
                                repo_id=proposal.repo_id,
                                path=path,
                                proposal=proposal,
                                base_content=base_content,
                                current_content=current_content,
                                content_b=change_content,
                            )
                            if merged_synth is not None:
                                logger.info("rebase_synthesis_merge_applied", proposal_id=proposal_id, path=path)
                                document_store.apply_full_file(path, merged_synth, proposal_id)
                                self.proposal_service._record_metric_event(
                                    event_type=SystemMetricEventType.SYNTHESIS_MERGE,
                                    repo_id=proposal.repo_id,
                                    proposal_id=proposal_id,
                                    reason="synthesis_merge_applied",
                                    path=path,
                                    synthesis_engine="deterministic_simulation",
                                )
                                continue
                        
                        raise ValueError(f"Semantic merge conflict on path {path} for proposal {proposal_id}")
                    else:
                        document_store.apply_full_file(path, change_content, proposal_id)
            else:
                def _apply_full():
                    for path in affected_paths:
                        document_store.apply_full_file(path, change_content, proposal_id)
                await asyncio.to_thread(_apply_full)
            return affected_paths

        if change_type in {"aip.v0", "aip_mutation"}:
            paths, _ = await asyncio.to_thread(
                document_store.apply_aip_mutations,
                change_content,
                proposal_id,
            )
            return paths

        raise ValueError(f"Unsupported proposal change_type: {change_type}")

    async def _recover_external_commit_abort(self, proposals: list[Proposal]) -> None:
        """Requeue proposals after an external commit race without aborting the loop."""
        self.session.rollback()
        if not proposals:
            return
        to_requeue = []
        self.session.expire_all()
        for p in proposals:
            self.session.refresh(p)
            if p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING):
                to_requeue.append(p)
        if to_requeue:
            await self.proposal_service.update_status_many(
                [(proposal.id, ProposalStatus.PENDING, "concurrent_head_update") for proposal in to_requeue]
            )

    @staticmethod
    def _snapshot_subphase_timing_payload(snapshot_metrics: dict[str, Any]) -> dict[str, float]:
        """Flatten snapshot sub-phase timings so harness reports can aggregate them."""
        payload: dict[str, float] = {}
        for entry in snapshot_metrics.get("sub_phases", []):
            if not isinstance(entry, dict):
                continue
            phase_name = entry.get("sub_phase")
            if not isinstance(phase_name, str) or not phase_name:
                continue
            safe_name = phase_name.replace("-", "_")
            duration_ms = float(entry.get("duration_ms", 0.0) or 0.0)
            identity_map_size = float(entry.get("identity_map_size", 0.0) or 0.0)
            payload[f"snapshot_phase_{safe_name}_ms"] = duration_ms
            payload[f"snapshot_phase_{safe_name}_identity_map_size"] = identity_map_size
        return payload

    @staticmethod
    def _accumulate_snapshot_subphase_timings(
        phase_timing: dict[str, float],
        candidate_timings: dict[str, float],
    ) -> None:
        """Accumulate flattened snapshot sub-phase timings into iteration totals."""
        for key, value in candidate_timings.items():
            if not key.startswith("snapshot_phase_"):
                continue
            phase_timing[key] = phase_timing.get(key, 0.0) + float(value or 0.0)

    async def evaluate_proposal(
        self,
        proposal: Proposal,
        document_store: DocumentStore,
    ) -> PromotionCandidate:
        """
        Evaluate a single proposal.

        1. Apply proposal to document store copy
        2. Create snapshot
        3. Run gates
        4. Compute score

        Returns PromotionCandidate with results.
        """
        started_at = time.perf_counter()
        phase = "mark_evaluating"
        try:
            # Capture if the proposal is currently in REBASING state
            was_rebasing = (proposal.status == ProposalStatus.REBASING)
            
            # Mark as evaluating
            status_started = time.perf_counter()
            is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"
            await self.proposal_service.update_status(proposal.id, ProposalStatus.EVALUATING, commit=not is_pg)
            status_ms = (time.perf_counter() - status_started) * 1000.0

            # Clone document store and apply proposal.
            phase = "clone_store"
            clone_started = time.perf_counter()
            candidate_store = document_store.clone()
            clone_ms = (time.perf_counter() - clone_started) * 1000.0
            clone_metrics = document_store.get_last_clone_metrics()

            phase = "apply_change"
            apply_started = time.perf_counter()
            changed_paths = await self._apply_proposal_change(candidate_store, proposal, was_rebasing=was_rebasing)
            apply_ms = (time.perf_counter() - apply_started) * 1000.0

            # Create snapshot
            phase = "create_snapshot"
            snapshot_started = time.perf_counter()
            snapshot = await self.snapshot_service.create_snapshot(
                proposal.repo_id,
                candidate_store,
                included_proposals=[proposal.id],
                created_by=proposal.agent_id,
                changed_paths_override=changed_paths,
                commit=not is_pg,
            )
            snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
            snapshot_metrics = self.snapshot_service.get_last_create_metrics()

            # Get gates and run them
            phase = "load_gates"
            gates = self.get_repo_gates(proposal.repo_id)
            phase = "materialize_gate_workspace"
            gate_workspace = self.snapshot_service.materialize_gate_workspace(snapshot.id)

            phase = "run_gates"
            gates_started = time.perf_counter()
            try:
                if gate_workspace:
                    gate_results = await self.gate_runner.run_all_gates(
                        snapshot.id,
                        gates,
                        gate_workspace,
                        proposal_id=proposal.id,
                        execution_mode=self.gate_execution_mode,
                        max_parallel=self.gate_max_parallel,
                    )
                else:
                    gate_results = []
            finally:
                if gate_workspace:
                    self.snapshot_service.cleanup_gate_workspace(gate_workspace)
            gates_ms = (time.perf_counter() - gates_started) * 1000.0
            timed_out_gate = self._find_timed_out_gate(gate_results)
            if timed_out_gate:
                raise GateTimeoutError(f"gate_timeout:{timed_out_gate}")

            # Compute score
            phase = "compute_score"
            score_started = time.perf_counter()
            final_score, policy_score, confidence_score = self.compute_score(
                proposal, gate_results, gates
            )
            score_ms = (time.perf_counter() - score_started) * 1000.0
        except Exception as exc:
            raise EvaluationPhaseError(phase, exc) from exc

        # Update metrics if required gates failed
        if policy_score == 0.0:
            # Hard failure
            self.trust_service.record_hard_fail(proposal.agent_id, commit=False)

        # Build evidence dict
        evidence = self.gate_runner.compute_evidence(gate_results)
        timing_payload = {
            "status_ms": status_ms,
            "clone_ms": clone_ms,
            "clone_file_count": float(clone_metrics.get("file_count", 0.0)),
            "clone_total_bytes": float(clone_metrics.get("total_bytes", 0.0)),
            "apply_ms": apply_ms,
            "snapshot_ms": snapshot_ms,
            "snapshot_hash_ms": float(snapshot_metrics.get("content_hash_ms", 0.0)),
            "snapshot_materialize_ms": float(snapshot_metrics.get("materialize_ms", 0.0)),
            "snapshot_db_commit_ms": float(snapshot_metrics.get("db_commit_ms", 0.0)),
            "snapshot_publish_ms": float(snapshot_metrics.get("publish_ms", 0.0)),
            "gates_ms": gates_ms,
            "score_ms": score_ms,
            "total_ms": (time.perf_counter() - started_at) * 1000.0,
        }
        timing_payload.update(self._snapshot_subphase_timing_payload(snapshot_metrics))

        return PromotionCandidate(
            proposals=[proposal],
            score=final_score,
            evidence=evidence,
            gate_results=gate_results,
            snapshot_id=snapshot.id,
            timings=timing_payload,
            is_rebased=was_rebasing,
        )

    async def evaluate_batch(
        self,
        proposals: list[Proposal],
        document_store: DocumentStore,
    ) -> PromotionCandidate:
        """
        Evaluate a batch of non-conflicting proposals together.

        Returns PromotionCandidate for the batch.
        """
        if not proposals:
            raise ValueError("Empty proposal batch")

        started_at = time.perf_counter()
        repo_id = proposals[0].repo_id
        is_rebased = any(p.status == ProposalStatus.REBASING for p in proposals)
        is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"

        to_mark_evaluating = [
            (p.id, ProposalStatus.EVALUATING, None)
            for p in proposals
            if p.status in (ProposalStatus.INGESTED, ProposalStatus.PENDING)
        ]
        if to_mark_evaluating:
            await self.proposal_service.update_status_many(
                to_mark_evaluating,
                commit=not is_pg,
            )

        # Helper to check overlap
        def check_overlap(p1: Proposal, p2: Proposal) -> bool:
            p1_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p1.affected_paths, []))
            p2_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p2.affected_paths, []))
            p1_symbols = self.proposal_service._decode_affected_symbols(p1.affected_symbols)
            p2_symbols = self.proposal_service._decode_affected_symbols(p2.affected_symbols)
            return self.proposal_service._has_semantic_conflict(p1_paths, p1_symbols, p2_paths, p2_symbols)

        # 1. Proposal-Local Validation Checks
        valid_proposals = []
        diff_payloads = []  # list of (diff_text, proposal_id)
        
        for proposal in proposals:
            try:
                # Malformed diff parse check
                if proposal.change_type == "unified_diff":
                    # Parse the unified diff to check for malformed parts
                    from ..crdt.document_store import parse_unified_diff
                    parsed = parse_unified_diff(proposal.change_content)
                    # Check for basic invalid start coordinates
                    for path, hunks in parsed.items():
                        for hunk in hunks:
                            if hunk.old_start <= 0:
                                if not (hunk.old_start == 0 and hunk.old_count == 0):
                                    raise ValueError("invalid hunk old_start")
                    diff_payloads.append((proposal.change_content, proposal.id))
                elif proposal.change_type == "full_file":
                    pass
                valid_proposals.append(proposal)
            except Exception as e:
                logger.exception("proposal_local_validation_failed", proposal_id=proposal.id, error=str(e))
                # Reject individual proposal
                await self.proposal_service.update_status(
                    proposal.id, ProposalStatus.REJECTED, f"proposal_local_failure: {str(e)}"
                )
                self.trust_service.record_rejection(proposal.agent_id, commit=False)

        if not valid_proposals:
            logger.warning("no_valid_proposals_in_batch_after_local_validation", repo_id=repo_id)
            return PromotionCandidate(
                proposals=[],
                score=0.0,
                evidence={},
                gate_results=[],
                snapshot_id="",
                timings={},
            )

        # Record BATCH_SIZE_K metric event (value is in event_reason)
        self.proposal_service._record_metric_event(
            event_type=SystemMetricEventType.BATCH_SIZE_K,
            repo_id=repo_id,
            proposals=valid_proposals,
        )

        # Clone and apply all proposals together (Aggregated Fold)
        clone_started = time.perf_counter()
        candidate_store = document_store.clone()
        clone_ms = (time.perf_counter() - clone_started) * 1000.0
        clone_metrics = document_store.get_last_clone_metrics()

        apply_started = time.perf_counter()
        batch_changed_paths: set[str] = set()
        try:
            from .structural_merge import attempt_structural_merge
            for proposal in valid_proposals:
                paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(proposal.affected_paths, []))
                for path in paths:
                    base_content = self.snapshot_service.get_file_content(proposal.base_head_id, path) or ""
                    current_content = candidate_store.get_file_content(path) or ""
                    
                    if current_content.strip() != base_content.strip() and base_content.strip() != "":
                        if self.semantic_merge_enabled:
                            content_b = self._get_proposal_content(proposal, path, document_store)
                            merged, failure_reason = attempt_structural_merge(base_content, current_content, content_b)
                            if merged is not None:
                                logger.info("structural_merge_applied", proposal_id=proposal.id, path=path)
                                candidate_store.apply_full_file(path, merged, proposal.id)
                                batch_changed_paths.add(path)
                                self.proposal_service._record_metric_event(
                                    event_type=SystemMetricEventType.STRUCTURAL_MERGE,
                                    repo_id=repo_id,
                                    proposal_id=proposal.id,
                                    reason="structural_merge_applied",
                                    path=path,
                                )
                                continue
                            else:
                                self.proposal_service._record_metric_event(
                                    event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                                    repo_id=repo_id,
                                    proposal_id=proposal.id,
                                    reason=failure_reason or "structural_ineligible",
                                    path=path,
                                )
                            
                            if self.synthesis_worker_enabled:
                                merged_synth = await self.attempt_semantic_merge_synthesis(
                                    repo_id=repo_id,
                                    path=path,
                                    proposal=proposal,
                                    base_content=base_content,
                                    current_content=current_content,
                                    content_b=content_b,
                                )
                                if merged_synth is not None:
                                    logger.info("synthesis_merge_applied", proposal_id=proposal.id, path=path)
                                    candidate_store.apply_full_file(path, merged_synth, proposal.id)
                                    batch_changed_paths.add(path)
                                    self.proposal_service._record_metric_event(
                                        event_type=SystemMetricEventType.SYNTHESIS_MERGE,
                                        repo_id=repo_id,
                                        proposal_id=proposal.id,
                                        reason="synthesis_merge_applied",
                                        path=path,
                                        synthesis_engine="deterministic_simulation",
                                    )
                                    continue
                        raise ValueError(f"Conflict on path {path} for proposal {proposal.id}")
                    
                    if proposal.change_type == "unified_diff":
                        candidate_store.apply_diff_batch([(proposal.change_content, proposal.id)])
                    elif proposal.change_type == "full_file":
                        candidate_store.apply_full_file(path, proposal.change_content, proposal.id)
                    batch_changed_paths.add(path)
        except Exception as e:
            # If the aggregated batch fold fails (e.g. overlap exception), sequential degrade immediately
            logger.exception("aggregated_fold_failed_triggering_sequential_degradation", error=str(e))
            valid_proposals = []  # Forces sequential degradation fallback below

        # If we successfully folded the batch, try creating a snapshot and running gates
        gates_passed = False
        gate_results = []
        gates = self.get_repo_gates(repo_id)
        snapshot = None

        if valid_proposals:
            snapshot_started = time.perf_counter()
            proposal_ids = [p.id for p in valid_proposals]
            is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"
            snapshot = await self.snapshot_service.create_snapshot(
                repo_id,
                candidate_store,
                included_proposals=proposal_ids,
                created_by="batch_evaluation",
                changed_paths_override=batch_changed_paths,
                commit=not is_pg,
            )
            snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
            snapshot_metrics = self.snapshot_service.get_last_create_metrics()

            # Run gates
            gate_workspace = self.snapshot_service.materialize_gate_workspace(snapshot.id)
            gates_started = time.perf_counter()
            try:
                if gate_workspace:
                    gate_results = await self.gate_runner.run_all_gates(
                        snapshot.id,
                        gates,
                        gate_workspace,
                        execution_mode=self.gate_execution_mode,
                        max_parallel=self.gate_max_parallel,
                    )
                else:
                    gate_results = []
            finally:
                if gate_workspace:
                    self.snapshot_service.cleanup_gate_workspace(gate_workspace)
            gates_ms = (time.perf_counter() - gates_started) * 1000.0
            
            timed_out_gate = self._find_timed_out_gate(gate_results)
            if timed_out_gate:
                raise GateTimeoutError(f"gate_timeout:{timed_out_gate}")

            gates_passed = self.gate_runner.all_required_gates_pass(gate_results, gates)

        # 2. Batch-Level Semantic / Gate Failures (Sequential Degradation Fallback)
        if not gates_passed:
            # Emit BATCH_GATE_FAIL metric
            self.proposal_service._record_metric_event(
                event_type=SystemMetricEventType.BATCH_GATE_FAIL,
                repo_id=repo_id,
                proposals=proposals,
                reason="batch_gate_failure",
            )

            logger.info("batch_gate_failed_starting_sequential_degradation", repo_id=repo_id, batch_size=len(proposals))

            # Sequential Degradation
            working_store = document_store.clone()
            accepted_winners = []

            ordered_proposals = sorted(proposals, key=lambda p: (-p.confidence_score, p.submitted_at or 0, p.id))
 
            for p in ordered_proposals:
                if p.status == ProposalStatus.REJECTED:
                    continue
 
                was_rebasing = (p.status == ProposalStatus.REBASING)
                
                # Check overlap with prior accepted winners in this loop
                overlap = False
                for winner in accepted_winners:
                    if check_overlap(p, winner):
                        overlap = True
                        break
 
                if overlap:
                    logger.info("sequential_degradation_superseded", proposal_id=p.id)
                    cont_meta = self.proposal_service.get_contention_metadata(p.id)
                    is_semantic_merge = (
                        self.semantic_merge_enabled
                        and cont_meta is not None
                        and cont_meta.contention_policy == "semantic_merge"
                    )
                    if is_semantic_merge:
                        repo = self.session.get(Repository, repo_id)
                        max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                        
                        p.rebase_attempts = (p.rebase_attempts or 0) + 1
                        self.session.add(p)
                        
                        if p.rebase_attempts <= max_rebase:
                            reason_json = json.dumps({
                                "event_reason": "STALE_BASE",
                                "contention_group_id": "sequential_degradation_overlap"
                            })
                            await self.proposal_service.update_status(p.id, ProposalStatus.REBASING, reason_json)
                        else:
                            reason_json = json.dumps({
                                "event_reason": "rebase_budget_exhaustion",
                                "contention_group_id": "sequential_degradation_overlap"
                            })
                            await self.proposal_service.update_status(p.id, ProposalStatus.SUPERSEDED, reason_json)
                            self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                    else:
                        await self.proposal_service.update_status(
                            p.id, ProposalStatus.SUPERSEDED, "sequential_degradation_overlap"
                        )
                        self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                    continue
 
                # Try applying p's change to working_store
                try:
                    pre_apply_store = working_store.clone()
                    cont_meta = self.proposal_service.get_contention_metadata(p.id)
                    is_semantic_merge = (
                        self.semantic_merge_enabled
                        and cont_meta is not None
                        and cont_meta.contention_policy == "semantic_merge"
                    )
                    changed_paths = await self._apply_proposal_change(working_store, p, was_rebasing=was_rebasing)
                except Exception as e:
                    logger.exception("sequential_degradation_apply_failed", proposal_id=p.id, error=str(e))
                    if was_rebasing and is_semantic_merge:
                        if isinstance(e, ValueError) and "conflict" in str(e).lower():
                            reason_json = json.dumps({
                                "event_reason": "SUPERSEDED_SEMANTIC_CONFLICT",
                                "error": str(e)
                            })
                            await self.proposal_service.update_status(
                                p.id, ProposalStatus.SUPERSEDED, reason_json
                            )
                            self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                        else:
                            repo = self.session.get(Repository, repo_id)
                            max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                            
                            p.rebase_attempts = (p.rebase_attempts or 0) + 1
                            self.session.add(p)
                            
                            if p.rebase_attempts <= max_rebase:
                                await self.proposal_service.update_status(
                                    p.id, ProposalStatus.REBASING, f"sequential_degradation_apply_failed: {str(e)[:160]}"
                                )
                            else:
                                reason_json = json.dumps({
                                    "event_reason": "SUPERSEDED_EVALUATION_ERROR",
                                    "error": str(e)
                                })
                                await self.proposal_service.update_status(
                                    p.id, ProposalStatus.SUPERSEDED, reason_json
                                )
                                self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                    else:
                        await self.proposal_service.update_status(
                            p.id, ProposalStatus.REJECTED, f"sequential_degradation_apply_failed: {str(e)}"
                        )
                        self.trust_service.record_rejection(p.agent_id, commit=False)
                    working_store = pre_apply_store
                    continue
 
                is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"
                try:
                    temp_snapshot = await self.snapshot_service.create_snapshot(
                        repo_id,
                        working_store,
                        included_proposals=[p.id],
                        created_by="sequential_degradation_eval",
                        changed_paths_override=set(changed_paths),
                        commit=not is_pg,
                    )
                    temp_gate_workspace = self.snapshot_service.materialize_gate_workspace(temp_snapshot.id)
                    try:
                        if temp_gate_workspace:
                            temp_gate_results = await self.gate_runner.run_all_gates(
                                temp_snapshot.id,
                                gates,
                                temp_gate_workspace,
                                execution_mode=self.gate_execution_mode,
                                max_parallel=self.gate_max_parallel,
                            )
                        else:
                            temp_gate_results = []
                    finally:
                        if temp_gate_workspace:
                            self.snapshot_service.cleanup_gate_workspace(temp_gate_workspace)
 
                    if self.gate_runner.all_required_gates_pass(temp_gate_results, gates):
                        logger.info("sequential_degradation_promoted", proposal_id=p.id)
                        p_candidate = PromotionCandidate(
                            proposals=[p],
                            score=p.confidence_score,
                            evidence=self.gate_runner.compute_evidence(temp_gate_results),
                            gate_results=temp_gate_results,
                            snapshot_id=temp_snapshot.id,
                            timings={},
                            is_rebased=was_rebasing,
                        )
                        await self.promote(
                            repo_id, p_candidate, pre_apply_store, triggered_by="sequential_degradation"
                        )
                        # Sync parent's document_store
                        await self._apply_proposal_change(document_store, p, was_rebasing=was_rebasing)
                        accepted_winners.append(p)
                    else:
                        logger.info("sequential_degradation_rejected", proposal_id=p.id)
                        if was_rebasing and is_semantic_merge:
                            reason_json = json.dumps({
                                "event_reason": "SUPERSEDED_GATE_FAILURE"
                            })
                            await self.proposal_service.update_status(
                                p.id, ProposalStatus.SUPERSEDED, reason_json
                            )
                        else:
                            await self.proposal_service.update_status(
                                p.id, ProposalStatus.REJECTED, "sequential_degradation_gate_failed"
                            )
                            self.trust_service.record_rejection(p.agent_id, commit=False)
                        working_store = pre_apply_store
                except Exception as e:
                    logger.exception("sequential_degradation_eval_error", proposal_id=p.id, error=str(e))
                    if was_rebasing and is_semantic_merge:
                        repo = self.session.get(Repository, repo_id)
                        max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                        
                        p.rebase_attempts = (p.rebase_attempts or 0) + 1
                        self.session.add(p)
                        
                        if p.rebase_attempts <= max_rebase:
                            await self.proposal_service.update_status(
                                p.id, ProposalStatus.REBASING, f"sequential_degradation_eval_failed: {str(e)[:160]}"
                            )
                        else:
                            reason_json = json.dumps({
                                "event_reason": "SUPERSEDED_EVALUATION_ERROR",
                                "error": str(e)
                            })
                            await self.proposal_service.update_status(
                                p.id, ProposalStatus.SUPERSEDED, reason_json
                            )
                            self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                    else:
                        await self.proposal_service.update_status(
                            p.id, ProposalStatus.REJECTED, f"sequential_degradation_eval_failed: {str(e)}"
                        )
                        self.trust_service.record_rejection(p.agent_id, commit=False)
                    working_store = pre_apply_store

            # Return empty candidate to indicate batch loop is finished
            degradation_evidence: dict[str, str] = {
                "_sequential_degradation": "true",
                "_promoted_proposal_ids": ",".join(w.id for w in accepted_winners),
            }
            return PromotionCandidate(
                proposals=[],
                score=0.0,
                evidence=degradation_evidence,
                gate_results=[],
                snapshot_id="",
                timings={},
            )

        # Successful batch evaluation (all valid proposals pass gates)
        apply_ms = (time.perf_counter() - apply_started) * 1000.0

        # Compute aggregate score (average of proposal confidences)
        avg_confidence = sum(p.confidence_score for p in valid_proposals) / len(valid_proposals)

        # Create a synthetic proposal for scoring
        @dataclass
        class BatchProposal:
            agent_id: str
            confidence_score: float

        batch = BatchProposal(agent_id="batch", confidence_score=avg_confidence)
        score_started = time.perf_counter()
        final_score, policy_score, _ = self.compute_score(batch, gate_results, gates)
        score_ms = (time.perf_counter() - score_started) * 1000.0

        evidence = self.gate_runner.compute_evidence(gate_results)
        timing_payload = {
            "clone_ms": clone_ms,
            "clone_file_count": float(clone_metrics.get("file_count", 0.0)),
            "clone_total_bytes": float(clone_metrics.get("total_bytes", 0.0)),
            "apply_ms": apply_ms,
            "snapshot_ms": snapshot_ms,
            "snapshot_hash_ms": float(snapshot_metrics.get("content_hash_ms", 0.0)),
            "snapshot_materialize_ms": float(snapshot_metrics.get("materialize_ms", 0.0)),
            "snapshot_db_commit_ms": float(snapshot_metrics.get("db_commit_ms", 0.0)),
            "snapshot_publish_ms": float(snapshot_metrics.get("publish_ms", 0.0)),
            "gates_ms": gates_ms,
            "score_ms": score_ms,
            "total_ms": (time.perf_counter() - started_at) * 1000.0,
        }
        timing_payload.update(self._snapshot_subphase_timing_payload(snapshot_metrics))

        return PromotionCandidate(
            proposals=valid_proposals,
            score=final_score,
            evidence=evidence,
            gate_results=gate_results,
            snapshot_id=snapshot.id,
            timings=timing_payload,
            is_rebased=is_rebased,
        )

    async def evaluate_cohort(
        self,
        proposals: list[Proposal],
        document_store: DocumentStore,
    ) -> PromotionCandidate:
        """
        Evaluate a batch of proposals as an atomic cohort.
        Does NOT perform sequential degradation if evaluation or gates fail.
        """
        if not proposals:
            raise ValueError("Empty proposal cohort")

        started_at = time.perf_counter()
        repo_id = proposals[0].repo_id

        # Helper to check overlap
        def check_overlap(p1: Proposal, p2: Proposal) -> bool:
            p1_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p1.affected_paths, []))
            p2_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p2.affected_paths, []))
            p1_symbols = self.proposal_service._decode_affected_symbols(p1.affected_symbols)
            p2_symbols = self.proposal_service._decode_affected_symbols(p2.affected_symbols)
            return self.proposal_service._has_semantic_conflict(p1_paths, p1_symbols, p2_paths, p2_symbols)

        # 1. Proposal-Local Validation Checks
        valid_proposals = []
        diff_payloads = []  # list of (diff_text, proposal_id)
        
        for proposal in proposals:
            try:
                # Malformed diff parse check
                if proposal.change_type == "unified_diff":
                    from ..crdt.document_store import parse_unified_diff
                    parsed = parse_unified_diff(proposal.change_content)
                    for path, hunks in parsed.items():
                        for hunk in hunks:
                            if hunk.old_start <= 0:
                                if not (hunk.old_start == 0 and hunk.old_count == 0):
                                    raise ValueError("invalid hunk old_start")
                    diff_payloads.append((proposal.change_content, proposal.id))
                elif proposal.change_type == "full_file":
                    pass
                valid_proposals.append(proposal)
            except Exception as e:
                logger.exception("proposal_local_validation_failed_rejecting_cohort", proposal_id=proposal.id, error=str(e))
                # If a single proposal is invalid, the entire cohort fails
                return PromotionCandidate(
                    proposals=[],
                    score=0.0,
                    evidence={},
                    gate_results=[],
                    snapshot_id="",
                    timings={},
                )

        if len(valid_proposals) != len(proposals):
            return PromotionCandidate(
                proposals=[],
                score=0.0,
                evidence={},
                gate_results=[],
                snapshot_id="",
                timings={},
            )

        # Record BATCH_SIZE_K metric event
        self.proposal_service._record_metric_event(
            event_type=SystemMetricEventType.BATCH_SIZE_K,
            repo_id=repo_id,
            proposals=valid_proposals,
        )

        # Clone and apply all proposals together (Aggregated Fold)
        clone_started = time.perf_counter()
        candidate_store = document_store.clone()
        clone_ms = (time.perf_counter() - clone_started) * 1000.0
        clone_metrics = document_store.get_last_clone_metrics()

        apply_started = time.perf_counter()
        batch_changed_paths: set[str] = set()
        try:
            from .structural_merge import attempt_structural_merge
            for proposal in valid_proposals:
                paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(proposal.affected_paths, []))
                for path in paths:
                    base_content = self.snapshot_service.get_file_content(proposal.base_head_id, path) or ""
                    current_content = candidate_store.get_file_content(path) or ""
                    
                    if current_content.strip() != base_content.strip() and base_content.strip() != "":
                        if self.semantic_merge_enabled:
                            content_b = self._get_proposal_content(proposal, path, document_store)
                            merged, failure_reason = attempt_structural_merge(base_content, current_content, content_b)
                            if merged is not None:
                                logger.info("structural_merge_applied", proposal_id=proposal.id, path=path)
                                candidate_store.apply_full_file(path, merged, proposal.id)
                                batch_changed_paths.add(path)
                                self.proposal_service._record_metric_event(
                                    event_type=SystemMetricEventType.STRUCTURAL_MERGE,
                                    repo_id=repo_id,
                                    proposal_id=proposal.id,
                                    reason="structural_merge_applied",
                                    path=path,
                                )
                                continue
                            
                            if self.synthesis_worker_enabled:
                                merged_synth = await self.attempt_semantic_merge_synthesis(
                                    repo_id=repo_id,
                                    path=path,
                                    proposal=proposal,
                                    base_content=base_content,
                                    current_content=current_content,
                                    content_b=content_b,
                                )
                                if merged_synth is not None:
                                    logger.info("synthesis_merge_applied", proposal_id=proposal.id, path=path)
                                    candidate_store.apply_full_file(path, merged_synth, proposal.id)
                                    batch_changed_paths.add(path)
                                    self.proposal_service._record_metric_event(
                                        event_type=SystemMetricEventType.SYNTHESIS_MERGE,
                                        repo_id=repo_id,
                                        proposal_id=proposal.id,
                                        reason="synthesis_merge_applied",
                                        path=path,
                                        synthesis_engine="deterministic_simulation",
                                    )
                                    continue
                        raise ValueError(f"Conflict on path {path} for proposal {proposal.id}")
                    
                    if proposal.change_type == "unified_diff":
                        candidate_store.apply_diff_batch([(proposal.change_content, proposal.id)])
                    elif proposal.change_type == "full_file":
                        candidate_store.apply_full_file(path, proposal.change_content, proposal.id)
                    batch_changed_paths.add(path)
        except Exception as e:
            logger.exception("cohort_fold_failed", error=str(e))
            # Aggregated fold failed: cohort cannot be promoted
            return PromotionCandidate(
                proposals=[],
                score=0.0,
                evidence={},
                gate_results=[],
                snapshot_id="",
                timings={},
            )

        gates_passed = False
        gate_results = []
        gates = self.get_repo_gates(repo_id)
        snapshot = None

        snapshot_started = time.perf_counter()
        proposal_ids = [p.id for p in valid_proposals]
        is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"
        snapshot = await self.snapshot_service.create_snapshot(
            repo_id,
            candidate_store,
            included_proposals=proposal_ids,
            created_by="cohort_evaluation",
            changed_paths_override=batch_changed_paths,
            commit=not is_pg,
        )
        snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
        snapshot_metrics = self.snapshot_service.get_last_create_metrics()

        # Run gates
        gate_workspace = self.snapshot_service.materialize_gate_workspace(snapshot.id)
        gates_started = time.perf_counter()
        try:
            if gate_workspace:
                gate_results = await self.gate_runner.run_all_gates(
                    snapshot.id,
                    gates,
                    gate_workspace,
                    execution_mode=self.gate_execution_mode,
                    max_parallel=self.gate_max_parallel,
                )
            else:
                gate_results = []
        finally:
            if gate_workspace:
                self.snapshot_service.cleanup_gate_workspace(gate_workspace)
        gates_ms = (time.perf_counter() - gates_started) * 1000.0
        
        timed_out_gate = self._find_timed_out_gate(gate_results)
        if timed_out_gate:
            raise GateTimeoutError(f"gate_timeout:{timed_out_gate}")

        gates_passed = self.gate_runner.all_required_gates_pass(gate_results, gates)

        if not gates_passed:
            logger.info("cohort_gate_failed", repo_id=repo_id, batch_size=len(proposals))
            return PromotionCandidate(
                proposals=[],
                score=0.0,
                evidence={},
                gate_results=[],
                snapshot_id="",
                timings={},
            )

        # Successful cohort evaluation
        apply_ms = (time.perf_counter() - apply_started) * 1000.0
        avg_confidence = sum(p.confidence_score for p in valid_proposals) / len(valid_proposals)

        # Create a synthetic proposal for scoring
        from dataclasses import dataclass
        @dataclass
        class BatchProposal:
            agent_id: str
            confidence_score: float

        batch = BatchProposal(agent_id="batch", confidence_score=avg_confidence)
        score_started = time.perf_counter()
        final_score, policy_score, _ = self.compute_score(batch, gate_results, gates)
        score_ms = (time.perf_counter() - score_started) * 1000.0

        evidence = self.gate_runner.compute_evidence(gate_results)
        timing_payload = {
            "clone_ms": clone_ms,
            "clone_file_count": float(clone_metrics.get("file_count", 0.0)),
            "clone_total_bytes": float(clone_metrics.get("total_bytes", 0.0)),
            "apply_ms": apply_ms,
            "snapshot_ms": snapshot_ms,
            "snapshot_hash_ms": float(snapshot_metrics.get("content_hash_ms", 0.0)),
            "snapshot_materialize_ms": float(snapshot_metrics.get("materialize_ms", 0.0)),
            "snapshot_db_commit_ms": float(snapshot_metrics.get("db_commit_ms", 0.0)),
            "snapshot_publish_ms": float(snapshot_metrics.get("publish_ms", 0.0)),
            "gates_ms": gates_ms,
            "score_ms": score_ms,
            "total_ms": (time.perf_counter() - started_at) * 1000.0,
        }
        timing_payload.update(self._snapshot_subphase_timing_payload(snapshot_metrics))

        return PromotionCandidate(
            proposals=valid_proposals,
            score=final_score,
            evidence=evidence,
            gate_results=gate_results,
            snapshot_id=snapshot.id,
            timings=timing_payload,
        )

    async def _evaluate_single_proposal(
        self,
        proposal: Proposal,
        document_store: DocumentStore,
    ) -> EvaluationResult:
        """Evaluate a single proposal, returning result wrapper for parallel execution."""
        try:
            candidate = await self.evaluate_proposal(proposal, document_store)
            return EvaluationResult(
                proposal=proposal,
                candidate=candidate,
                success=True,
            )
        except GateTimeoutError as e:
            self.session.rollback()
            try:
                self.session.refresh(proposal)
                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                is_semantic_merge = (
                    self.semantic_merge_enabled
                    and cont_meta is not None
                    and cont_meta.contention_policy == "semantic_merge"
                )
                
                if is_semantic_merge:
                    repo = self.session.get(Repository, proposal.repo_id)
                    max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                    
                    proposal.rebase_attempts = (proposal.rebase_attempts or 0) + 1
                    self.session.add(proposal)
                    
                    if proposal.rebase_attempts <= max_rebase:
                        await self.proposal_service.update_status(
                            proposal.id,
                            ProposalStatus.REBASING,
                            reason=f"gate_timeout_requeue: {str(e)[:160]}",
                        )
                    else:
                        reason_json = json.dumps({
                            "event_reason": "SUPERSEDED_GATE_TIMEOUT",
                            "error": str(e)
                        })
                        await self.proposal_service.update_status(
                            proposal.id,
                            ProposalStatus.SUPERSEDED,
                            reason=reason_json,
                        )
                        self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                else:
                    await self.proposal_service.update_status(
                        proposal.id,
                        ProposalStatus.PENDING,
                        reason=f"gate_timeout_requeue: {str(e)[:160]}",
                    )
                self.session.commit()
            except Exception:
                pass
            logger.warning(
                "proposal_gate_timeout_requeued",
                proposal_id=proposal.id,
                error=str(e),
            )
            return EvaluationResult(
                proposal=proposal,
                candidate=None,
                error=str(e),
                success=False,
            )
        except Exception as e:
            self.session.rollback()
            try:
                self.session.refresh(proposal)
                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                is_semantic_merge = (
                    self.semantic_merge_enabled
                    and cont_meta is not None
                    and cont_meta.contention_policy == "semantic_merge"
                )
                
                if is_semantic_merge:
                    if isinstance(e, ValueError) and "conflict" in str(e).lower():
                        reason_json = json.dumps({
                            "event_reason": "SUPERSEDED_SEMANTIC_CONFLICT",
                            "error": str(e)
                        })
                        await self.proposal_service.update_status(
                            proposal.id,
                            ProposalStatus.SUPERSEDED,
                            reason=reason_json,
                        )
                        self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                    else:
                        repo = self.session.get(Repository, proposal.repo_id)
                        max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                        
                        proposal.rebase_attempts = (proposal.rebase_attempts or 0) + 1
                        self.session.add(proposal)
                        
                        if proposal.rebase_attempts <= max_rebase:
                            await self.proposal_service.update_status(
                                proposal.id,
                                ProposalStatus.REBASING,
                                reason=f"evaluation_error_requeue: {str(e)[:160]}",
                            )
                        else:
                            reason_json = json.dumps({
                                "event_reason": "SUPERSEDED_EVALUATION_ERROR",
                                "error": str(e)
                            })
                            await self.proposal_service.update_status(
                                proposal.id,
                                ProposalStatus.SUPERSEDED,
                                reason=reason_json,
                            )
                            self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                else:
                    await self.proposal_service.update_status(
                        proposal.id,
                        ProposalStatus.QUARANTINED,
                        reason=self._format_exception_reason(e, prefix="evaluation_error"),
                    )
                self.session.commit()
            except Exception:
                pass
            logger.error(
                "proposal_evaluation_failed",
                proposal_id=proposal.id,
                error=str(e),
            )
            return EvaluationResult(
                proposal=proposal,
                candidate=None,
                error=str(e),
                success=False,
            )

    async def select_best_candidate(
        self,
        conflict_set: ConflictSet,
        document_store: DocumentStore,
        parallel: bool = True,
    ) -> Optional[PromotionCandidate]:
        """
        Evaluate proposals in a conflict set and select the best one.

        Phase 1 Enhancement: Parallel evaluation of top-k candidates.

        Args:
            conflict_set: The conflict set to resolve
            document_store: Current document state
            parallel: Whether to evaluate candidates in parallel (default: True)

        Returns:
            Best candidate, or None if no viable candidates
        """
        proposal_ids = json.loads(conflict_set.proposal_ids)
        proposals = self.proposal_service.get_proposals_by_ids(
            proposal_ids,
            repo_id=conflict_set.repo_id,
        )

        if not proposals:
            return None

        # Filter to only pending proposals
        pending_proposals = [p for p in proposals if p.status == ProposalStatus.PENDING]
        if not pending_proposals:
            return None

        # Get repo config for max alternatives
        repo = self.session.get(Repository, conflict_set.repo_id)
        max_k = repo.max_alternatives_per_conflict if repo else 5

        # Sort by trust-adjusted confidence and take top-k
        # Combined score = logit(confidence) + logit(trust)
        sorted_proposals = sorted(
            pending_proposals,
            key=lambda p: (
                self.trust_service.get_combined_agent_score(p.agent_id, p.confidence_score),
                -p.submitted_at.timestamp()
            ),
            reverse=True
        )
        top_k = sorted_proposals[:max_k]
        excluded = sorted_proposals[max_k:]

        logger.info(
            "evaluating_conflict_set",
            conflict_set_id=conflict_set.id,
            total_proposals=len(proposals),
            pending_proposals=len(pending_proposals),
            evaluating_count=len(top_k),
            excluded_count=len(excluded),
        )

        # Evaluate candidates
        if parallel and len(top_k) > 1:
            # Phase 1: Parallel evaluation using asyncio.gather
            eval_tasks = [
                self._evaluate_single_proposal(p, document_store)
                for p in top_k
            ]
            results = await asyncio.gather(*eval_tasks, return_exceptions=False)
        else:
            # Sequential evaluation
            results = []
            for proposal in top_k:
                result = await self._evaluate_single_proposal(proposal, document_store)
                results.append(result)

        # Collect successful candidates
        candidates = []
        failed_proposals = []
        for result in results:
            if result.success and result.candidate:
                # Add excluded proposal tracking
                result.candidate.excluded_proposal_ids = [p.id for p in excluded]
                candidates.append(result.candidate)
            else:
                failed_proposals.append(result.proposal.id)

        if not candidates:
            logger.warning(
                "no_viable_candidates",
                conflict_set_id=conflict_set.id,
                failed_proposals=failed_proposals,
            )
            return None

        # Select best by score, with tie-breaking by earliest submission time
        def candidate_sort_key(c: PromotionCandidate) -> tuple:
            # Higher score is better, earlier submission is better (for ties)
            submission_ts = c.earliest_submission.timestamp() if c.earliest_submission else 0
            return (c.score, -submission_ts)

        candidates.sort(key=candidate_sort_key, reverse=True)
        best = candidates[0]

        # Add other evaluated candidates' proposals to excluded list and record neutral loss
        for other in candidates[1:]:
            for p in other.proposals:
                if p.id not in best.excluded_proposal_ids:
                    best.excluded_proposal_ids.append(p.id)
                # Record neutral loss for valid candidates that just didn't win
                self.trust_service.record_neutral_loss(p.agent_id, commit=False)

        logger.info(
            "best_candidate_selected",
            conflict_set_id=conflict_set.id,
            winning_proposal=best.proposals[0].id if best.proposals else None,
            score=best.score,
            evaluated_count=len(candidates),
            excluded_count=len(best.excluded_proposal_ids),
        )

        return best

    async def promote(
        self,
        repo_id: str,
        candidate: PromotionCandidate,
        document_store: DocumentStore,
        triggered_by: str = "auto",
    ) -> Promotion:
        """Promote a candidate using process-level serialization lock."""
        async with self._promotion_lock:
            try:
                return await self._promote_internal(
                    repo_id=repo_id,
                    candidate=candidate,
                    document_store=document_store,
                    triggered_by=triggered_by,
                )
            except (ContentionWinnerExistsError, OracleSufficiencyError, PromotionPhaseError, ExternalCommitAbortedError) as e:
                orig_err = e.original if isinstance(e, PromotionPhaseError) else e
                if isinstance(orig_err, ContentionWinnerExistsError):
                    self.session.rollback()
                    try:
                        is_semantic_merge = (
                            self.semantic_merge_enabled
                            and orig_err.contention_policy == "semantic_merge"
                        )
                        
                        updates: list[tuple[str, ProposalStatus, Optional[str]]] = []
                        if is_semantic_merge:
                            repo = self.session.get(Repository, repo_id)
                            max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                            
                            # Gather all proposals involved (candidate proposals + conflict siblings)
                            proposals_to_update = list(candidate.proposals)
                            if candidate.proposals and candidate.proposals[0].conflict_set_id:
                                cs_id = candidate.proposals[0].conflict_set_id
                                cs = self.proposal_service.get_conflict_set(cs_id)
                                if cs:
                                    cs_proposal_ids = json.loads(cs.proposal_ids)
                                    cs_proposals = self.proposal_service.get_proposals_by_ids(cs_proposal_ids)
                                    for p in cs_proposals:
                                        if p.id not in [x.id for x in candidate.proposals] and p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING, ProposalStatus.REBASING):
                                            proposals_to_update.append(p)
                                            
                            for p in proposals_to_update:
                                self.session.refresh(p)
                                p.rebase_attempts = (p.rebase_attempts or 0) + 1
                                self.session.add(p)
                                
                                if p.rebase_attempts <= max_rebase:
                                    reason_json = json.dumps({
                                        "event_reason": "STALE_BASE",
                                        "contention_group_id": orig_err.contention_group_id,
                                        "contention_policy": orig_err.contention_policy,
                                        "winner_proposal_id": orig_err.winner_id,
                                        "target_paths": orig_err.target_paths
                                    })
                                    updates.append((p.id, ProposalStatus.REBASING, reason_json))
                                else:
                                    reason_json = json.dumps({
                                        "event_reason": "rebase_budget_exhaustion",
                                        "contention_group_id": orig_err.contention_group_id,
                                        "contention_policy": orig_err.contention_policy,
                                        "winner_proposal_id": orig_err.winner_id,
                                        "target_paths": orig_err.target_paths
                                    })
                                    updates.append((p.id, ProposalStatus.SUPERSEDED, reason_json))
                                    self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                        else:
                            reason_json = json.dumps({
                                "event_reason": "temporal_semantic_conflict",
                                "contention_group_id": orig_err.contention_group_id,
                                "contention_policy": orig_err.contention_policy,
                                "winner_proposal_id": orig_err.winner_id,
                                "target_paths": orig_err.target_paths
                            })
                            updates: list[tuple[str, ProposalStatus, Optional[str]]] = [
                                (p.id, ProposalStatus.SUPERSEDED, reason_json)
                                for p in candidate.proposals
                            ]
                            if candidate.proposals and candidate.proposals[0].conflict_set_id:
                                cs_id = candidate.proposals[0].conflict_set_id
                                cs = self.proposal_service.get_conflict_set(cs_id)
                                if cs:
                                    cs_proposal_ids = json.loads(cs.proposal_ids)
                                    cs_proposals = self.proposal_service.get_proposals_by_ids(cs_proposal_ids)
                                    for p in cs_proposals:
                                        if p.id not in [x.id for x in candidate.proposals] and p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING):
                                            updates.append((p.id, ProposalStatus.SUPERSEDED, reason_json))
                                            
                        await self.proposal_service.update_status_many(updates)
                        self.session.commit()
                    except Exception as ex:
                        logger.error("failed_to_update_status_contention_loser", error=str(ex))
                elif isinstance(orig_err, OracleSufficiencyError):
                    self.session.rollback()
                    try:
                        to_quarantine = [(p.id, ProposalStatus.QUARANTINED, str(orig_err)) for p in candidate.proposals]
                        if candidate.proposals and candidate.proposals[0].conflict_set_id:
                            cs_id = candidate.proposals[0].conflict_set_id
                            cs = self.proposal_service.get_conflict_set(cs_id)
                            if cs:
                                cs_proposal_ids = json.loads(cs.proposal_ids)
                                cs_proposals = self.proposal_service.get_proposals_by_ids(cs_proposal_ids)
                                for p in cs_proposals:
                                    if p.id not in [x.id for x in candidate.proposals] and p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING):
                                        to_quarantine.append((p.id, ProposalStatus.QUARANTINED, f"conflict_sibling_failed_oracle_sufficiency: {str(orig_err)}"))
                        await self.proposal_service.update_status_many(to_quarantine)
                        self.session.commit()
                    except Exception as ex:
                        logger.error("failed_to_quarantine_oracle_insufficiency", error=str(ex))
                raise orig_err
    async def _promote_internal(
        self,
        repo_id: str,
        candidate: PromotionCandidate,
        document_store: DocumentStore,
        triggered_by: str = "auto",
    ) -> Promotion:
        """
        Promote a candidate to become the new canonical head.

        1. Verify gates pass (hard requirement)
        2. Create promotion record
        3. Update head
        4. Mark proposals as promoted
        5. Publish events
        """
        promote_started = time.perf_counter()
        self._last_promotion_phase_timing = {}
        phase = "begin_immediate_transaction"
        try:
            from sqlmodel import text
            if self.session.bind and self.session.bind.dialect.name == "sqlite":
                try:
                    self.session.commit()
                except Exception:
                    pass
                self.session.exec(text("BEGIN IMMEDIATE"))
            phase = "repo_lookup"
            repo = self.session.get(Repository, repo_id)
            if not repo:
                raise ValueError(f"Repository not found: {repo_id}")
            if any(proposal.repo_id != repo_id for proposal in candidate.proposals):
                raise ValueError("Cannot promote: candidate proposals do not belong to target repository")

            # Verify that all candidate proposals are still pending/evaluating in database to prevent races
            self.session.expire_all()
            for proposal in candidate.proposals:
                self.session.refresh(proposal)
                if proposal.status not in (ProposalStatus.PENDING, ProposalStatus.EVALUATING, ProposalStatus.REBASING):
                    raise ExternalCommitAbortedError(f"Proposal {proposal.id} is no longer PENDING, EVALUATING, or REBASING (status: {proposal.status}) at promotion start")

            # 1.5 Check Temporal Contention Exclusivity
            phase = "contention_exclusivity_check"
            is_contention = False
            contention_group_id = None
            contention_policy = None
            target_paths = []
            
            for proposal in candidate.proposals:
                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                if cont_meta and cont_meta.contention_mode:
                    is_contention = True
                    contention_group_id = cont_meta.contention_group_id
                    contention_policy = cont_meta.contention_policy or "exactly_one_winner"
                    target_paths = json.loads(cont_meta.target_paths)
                    break
                    
            previous_winner_id = None
            if is_contention:
                is_rebased_promotion = False
                existing_winner = None
                
                # Primary check: check by contention_group_id
                if contention_group_id:
                    claim = self.session.get(ContentionWinner, (repo_id, contention_group_id))
                    if claim:
                        existing_winner = self.proposal_service.get_proposal(claim.winner_proposal_id)
                        
                # Fallback check: check path/symbol overlap for contention-mode proposals
                if not existing_winner:
                    for proposal in candidate.proposals:
                        overlap_p = self._check_fallback_temporal_contention(repo_id, proposal)
                        if overlap_p:
                            existing_winner = overlap_p
                            if not contention_group_id:
                                # Deterministic normalized key for the group ID based on the sorted target paths
                                contention_group_id = f"fallback:{':'.join(sorted(json.loads(proposal.affected_paths)))}"
                            break
                            
                if existing_winner and existing_winner.id not in [p.id for p in candidate.proposals]:
                    # Winner exists from a different candidate.
                    # Check if this is a rebased promotion (only allowed for semantic_merge)
                    if contention_policy == "semantic_merge":
                        repo = self.session.get(Repository, repo_id)
                        # For semantic_merge, it is a valid rebased promotion if the candidate was evaluated
                        # as rebased and its base_head_id matches current repo head
                        if candidate.is_rebased and all(p.base_head_id == repo.head_id for p in candidate.proposals):
                            is_rebased_promotion = True
                            
                    if not is_rebased_promotion:
                        logger.info("temporal_contention_winner_exists_aborting_promotion", proposal_ids=[p.id for p in candidate.proposals], winner_id=existing_winner.id)
                        raise ContentionWinnerExistsError(
                            message=f"Contention winner already exists: {existing_winner.id}",
                            winner_id=existing_winner.id,
                            contention_group_id=contention_group_id or "unknown",
                            contention_policy=contention_policy or "exactly_one_winner",
                            target_paths=target_paths
                        )
                        
                # Claim winner registry inside transaction
                if contention_group_id:
                    claim = self.session.get(ContentionWinner, (repo_id, contention_group_id))
                    if claim and existing_winner and existing_winner.id not in [p.id for p in candidate.proposals] and not is_rebased_promotion:
                        raise ContentionWinnerExistsError(
                            message=f"Contention winner already exists: {claim.winner_proposal_id}",
                            winner_id=claim.winner_proposal_id,
                            contention_group_id=contention_group_id,
                            contention_policy=contention_policy or "exactly_one_winner",
                            target_paths=target_paths
                        )
                    
                    winner_prop_id = candidate.proposals[0].id if candidate.proposals else "unknown"
                    if claim:
                        previous_winner_id = claim.winner_proposal_id
                        claim.winner_proposal_id = winner_prop_id
                        self.session.add(claim)
                    else:
                        new_claim = ContentionWinner(
                            repo_id=repo_id,
                            contention_group_id=contention_group_id,
                            winner_proposal_id=winner_prop_id
                        )
                        self.session.add(new_claim)
                    self.session.flush()


            phase = "gate_config"
            gates = self.get_repo_gates(repo_id)

            # Hard gate check
            phase = "gate_validation"
            if not self.gate_runner.all_required_gates_pass(candidate.gate_results, gates):
                raise ValueError("Cannot promote: required gates did not pass")

            # Golden assertions check
            phase = "golden_assertions_validation"
            golden_assertions = self.get_repo_golden_assertions(repo_id)
            if golden_assertions:
                assertions_passed = True
                failed_reasons = []
                for path, assertions in golden_assertions.items():
                    content = self.snapshot_service.get_file_content(candidate.snapshot_id, path) or ""
                    # Enforce must_not_contain at engine level (e.g. security/structural failures)
                    # leaving must_contain to post-hoc validation by the harness.
                    for unexpected in assertions.get("must_not_contain", []):
                        if unexpected in content:
                            assertions_passed = False
                            failed_reasons.append(f"Found forbidden content in {path}: {unexpected}")
                if not assertions_passed:
                    raise OracleSufficiencyError("oracle_insufficiency_escalated: " + "; ".join(failed_reasons))

            # Get score components
            phase = "score_components"
            if candidate.proposals:
                first_proposal = candidate.proposals[0]
                _, policy_score, confidence_score = self.compute_score(
                    first_proposal, candidate.gate_results, gates
                )
            else:
                policy_score = 1.0
                confidence_score = 0.5

            # Create promotion record with excluded proposal tracking
            phase = "prepare_evidence"
            excluded_ids = getattr(candidate, 'excluded_proposal_ids', [])
            from_content_hash = document_store.get_state_hash()
            snapshot = self.snapshot_service.get_snapshot(candidate.snapshot_id)
            to_content_hash = snapshot.content_hash if snapshot else ""
            promotion_evidence = dict(candidate.evidence)
            promotion_evidence.update(
                self._build_provenance_evidence(
                    repo_id=repo_id,
                    candidate=candidate,
                    from_head_id=repo.head_id,
                    from_head_sequence=repo.head_sequence,
                    to_head_sequence=repo.head_sequence + 1,
                    from_content_hash=from_content_hash,
                    to_content_hash=to_content_hash,
                    triggered_by=triggered_by,
                )
            )

            phase = "promotion_record_commit"
            promotion = Promotion(
                repo_id=repo_id,
                from_head_id=repo.head_id,
                from_head_sequence=repo.head_sequence,
                to_head_id=generate_id("head"),  # Will be updated
                to_head_sequence=repo.head_sequence + 1,
                included_proposals=json.dumps([p.id for p in candidate.proposals]),
                excluded_proposals=json.dumps(excluded_ids),
                evidence=json.dumps(promotion_evidence),
                policy_score=policy_score,
                confidence_score=confidence_score,
                final_score=candidate.score,
                triggered_by=triggered_by,
            )
            is_pg = self.session.bind and self.session.bind.dialect.name == "postgresql"
            record_commit_started = time.perf_counter()
            self.session.add(promotion)
            if is_pg:
                self.session.flush()
            else:
                self.session.commit()
                self.session.commit()
                if self.session.bind and self.session.bind.dialect.name == "sqlite":
                    self.session.exec(text("BEGIN IMMEDIATE"))
            record_commit_ms = (time.perf_counter() - record_commit_started) * 1000.0

            # Import repository service to update head
            from .repository_service import RepositoryService
            phase = "head_update"
            head_commit_started = time.perf_counter()
            repo_service = RepositoryService(self.session, self.storage_path)
            new_head = repo_service.update_head(
                repo_id,
                candidate.snapshot_id,
                promotion_id=promotion.id,
                commit=not is_pg,
            )

            # Update promotion with actual head ID
            promotion.to_head_id = new_head.id

            phase = "operation_graph_prepare"
            parent_node = self._resolve_parent_operation_node(
                repo_id=repo_id,
                from_head_id=promotion.from_head_id,
                from_head_sequence=promotion.from_head_sequence,
            )
            operation_node = OperationGraphNode(
                repo_id=repo_id,
                promotion_id=promotion.id,
                parent_node_id=parent_node.id if parent_node else None,
                parent_head_id=promotion.from_head_id,
                parent_head_sequence=promotion.from_head_sequence,
                to_head_id=new_head.id,
                to_head_sequence=new_head.sequence,
                snapshot_id=candidate.snapshot_id,
                operation_type=str(promotion_evidence.get("_op_graph_kind", "promotion")),
                node_hash=str(promotion_evidence.get("_op_graph_node", "")),
                payload_sha256=str(promotion_evidence.get("_op_graph_payload_sha256", "")),
                parent_ref=str(promotion_evidence.get("_op_graph_parent", "")),
                replay_from_hash=str(promotion_evidence.get("_replay_from_hash", "")),
                replay_to_hash=str(promotion_evidence.get("_replay_to_hash", "")),
                replay_gate_vector=str(promotion_evidence.get("_replay_gate_vector", "")),
                included_proposals=promotion.included_proposals,
                excluded_proposals=promotion.excluded_proposals,
            )
            phase = "operation_graph_commit"
            self.session.add(promotion)
            self.session.add(operation_node)
            if is_pg:
                self.session.flush()
            else:
                self.session.commit()
                if self.session.bind and self.session.bind.dialect.name == "sqlite":
                    self.session.exec(text("BEGIN IMMEDIATE"))
            head_commit_ms = (time.perf_counter() - head_commit_started) * 1000.0

            # Mark promoted proposals in one transaction.
            phase = "status_updates"
            status_phase_started = time.perf_counter()
            status_updates = []
            for proposal in candidate.proposals:
                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                reason_json = None
                if cont_meta and cont_meta.contention_mode:
                    reason_json = json.dumps({
                        "event_reason": "contention_winner_promoted" if previous_winner_id is None else "rebased_winner_promoted",
                        "contention_group_id": cont_meta.contention_group_id,
                        "contention_policy": cont_meta.contention_policy or "exactly_one_winner",
                        "previous_winner_proposal_id": previous_winner_id,
                        "target_paths": json.loads(cont_meta.target_paths)
                    })
                status_updates.append((proposal.id, ProposalStatus.PROMOTED, reason_json))

            await self.proposal_service.update_status_many(
                status_updates,
                commit=not is_pg,
            )

            # Resolve any conflict sets
            phase = "trust_and_conflict_resolution"
            for proposal in candidate.proposals:
                if proposal.conflict_set_id:
                    # This chosen proposal is the winner
                    self.trust_service.record_promotion(proposal.agent_id, commit=False)

                    await self.proposal_service.resolve_conflict_set(
                        proposal.conflict_set_id,
                        proposal.id,
                        commit=not is_pg,
                        semantic_merge_enabled=self.semantic_merge_enabled,
                    )
                else:
                    # Promotion for non-conflicting proposal
                    self.trust_service.record_promotion(proposal.agent_id, commit=False)
            status_phase_ms = (time.perf_counter() - status_phase_started) * 1000.0

            # Persist trust metric updates as one transaction.
            phase = "trust_commit"
            trust_commit_started = time.perf_counter()
            self.session.commit()
            trust_commit_ms = (time.perf_counter() - trust_commit_started) * 1000.0

            # Publish head update event
            phase = "publish"
            publish_started = time.perf_counter()
            await self.event_bus.publish(
                create_head_event(
                    EventType.HEAD_UPDATED,
                    repo_id,
                    promotion.from_head_id,
                    new_head.id,
                    promotion.from_head_sequence,
                    new_head.sequence,
                    candidate.snapshot_id,
                    promotion_id=promotion.id,
                    included_proposals=[p.id for p in candidate.proposals],
                )
            )
            publish_ms = (time.perf_counter() - publish_started) * 1000.0
        except ExternalCommitAbortedError:
            raise
        except Exception as exc:
            raise PromotionPhaseError(phase, exc) from exc

        phase_timing = {
            "record_commit_ms": record_commit_ms,
            "head_commit_ms": head_commit_ms,
            "status_phase_ms": status_phase_ms,
            "trust_commit_ms": trust_commit_ms,
            "publish_ms": publish_ms,
            "commit_total_ms": (
                record_commit_ms + head_commit_ms + status_phase_ms + trust_commit_ms
            ),
            "total_ms": (time.perf_counter() - promote_started) * 1000.0,
        }
        self._last_promotion_phase_timing = phase_timing

        logger.info(
            "promotion_completed",
            promotion_id=promotion.id,
            repo_id=repo_id,
            new_head_id=new_head.id,
            new_sequence=new_head.sequence,
            proposals=[p.id for p in candidate.proposals],
            score=candidate.score,
            phase_timing=phase_timing,
        )

        return promotion

    async def process_pending_proposals(
        self,
        repo_id: str,
        document_store: DocumentStore,
        batch_size: int = 10,
        proposal_scan_limit: Optional[int] = None,
        max_conflict_sets: Optional[int] = 1,
        max_non_conflicting_batches: int = 4,
        max_processing_seconds: Optional[float] = None,
        recover_stuck_after_seconds: Optional[float] = 60.0,
        prioritize_non_conflicting: bool = True,
    ) -> list[Promotion]:
        """Process pending proposals with process-level serialization lock."""
        async with self._promotion_lock:
            return await self._process_pending_proposals_internal(
                repo_id=repo_id,
                document_store=document_store,
                batch_size=batch_size,
                proposal_scan_limit=proposal_scan_limit,
                max_conflict_sets=max_conflict_sets,
                max_non_conflicting_batches=max_non_conflicting_batches,
                max_processing_seconds=max_processing_seconds,
                recover_stuck_after_seconds=recover_stuck_after_seconds,
                prioritize_non_conflicting=prioritize_non_conflicting,
            )

    async def _process_pending_proposals_internal(
        self,
        repo_id: str,
        document_store: DocumentStore,
        batch_size: int = 10,
        proposal_scan_limit: Optional[int] = None,
        max_conflict_sets: Optional[int] = 1,
        max_non_conflicting_batches: int = 4,
        max_processing_seconds: Optional[float] = None,
        recover_stuck_after_seconds: Optional[float] = 60.0,
        prioritize_non_conflicting: bool = True,
    ) -> list[Promotion]:
        """
        Process pending proposals and promote viable ones.

        This is the main entry point for the promotion loop. Optional limits
        keep one iteration bounded so promotion can run continuously under load.
        """
        self._last_iteration_timing = {}
        promotions = []
        started_at = time.perf_counter()
        phase_timing = {
            "select_ms": 0.0,
            "evaluate_ms": 0.0,
            "clone_ms": 0.0,
            "snapshot_ms": 0.0,
            "snapshot_hash_ms": 0.0,
            "snapshot_materialize_ms": 0.0,
            "snapshot_db_commit_ms": 0.0,
            "snapshot_publish_ms": 0.0,
            "gates_ms": 0.0,
            "commit_ms": 0.0,
            "promote_ms": 0.0,
            "processed_conflict_sets": 0.0,
            "processed_non_conflicting_batches": 0.0,
            "processed_non_conflicting_proposals": 0.0,
            "processed_conflicting_promotions": 0.0,
            "proposals_scanned_count": 0.0,
            "candidates_built_count": 0.0,
            "promotions_created_count": 0.0,
            "proposals_promoted_count": 0.0,
            "hit_time_cap_count": 0.0,
            "hit_batch_cap_count": 0.0,
            "conflict_sets_seen_count": 0.0,
            "conflict_sets_no_candidate_count": 0.0,
            "external_commit_abort_count": 0.0,
        }
        stop_iteration = False

        if batch_size <= 0:
            return promotions
        if max_non_conflicting_batches <= 0:
            return promotions

        if recover_stuck_after_seconds and recover_stuck_after_seconds > 0:
            recovered = self._recover_stale_evaluating(
                repo_id=repo_id,
                stale_after_seconds=recover_stuck_after_seconds,
            )
            if recovered:
                logger.warning(
                    "recovered_stale_evaluating",
                    repo_id=repo_id,
                    recovered_count=recovered,
                )

        scan_limit = proposal_scan_limit or max(batch_size * max_non_conflicting_batches * 8, 200)

        # Select pending proposals + conflict sets.
        select_started = time.perf_counter()
        pending = self.proposal_service.get_pending_queue(repo_id, limit=scan_limit, for_update=False)
        conflict_sets = self.proposal_service.list_conflict_sets(
            repo_id,
            resolved=False,
            limit=scan_limit,
            newest_first=False,
        )
        phase_timing["select_ms"] = (time.perf_counter() - select_started) * 1000.0
        
        self._last_scanned_proposal_ids = [p.id for p in pending]
        self._last_scanned_conflict_set_ids = [cs.id for cs in conflict_sets]
        phase_timing["proposals_scanned_count"] = float(len(pending))

        if not pending:
            phase_timing["total_ms"] = (time.perf_counter() - started_at) * 1000.0
            self._last_iteration_timing = phase_timing
            return promotions

        logger.info(
            "processing_pending_proposals",
            repo_id=repo_id,
            pending_count=len(pending),
            scan_limit=scan_limit,
        )

        # Helper to check overlap
        def check_overlap(p1: Proposal, p2: Proposal) -> bool:
            p1_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p1.affected_paths, []))
            p2_paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p2.affected_paths, []))
            p1_symbols = self.proposal_service._decode_affected_symbols(p1.affected_symbols)
            p2_symbols = self.proposal_service._decode_affected_symbols(p2.affected_symbols)
            has_conflict = self.proposal_service._has_semantic_conflict(p1_paths, p1_symbols, p2_paths, p2_symbols)
            if has_conflict and self.semantic_merge_enabled:
                if self._is_structurally_mergeable(p1, p2):
                    return False
            return has_conflict

        effective_conflict_cap = 1 if max_conflict_sets is None else max(0, max_conflict_sets)
        conflict_proposal_ids = set()
        for cs in conflict_sets:
            conflict_proposal_ids.update(json.loads(cs.proposal_ids))

        non_conflicting = [p for p in pending if p.id not in conflict_proposal_ids]

        async def _process_non_conflicting() -> None:
            nonlocal stop_iteration
            if not non_conflicting:
                return
            batch_index = 0
            
            # Sort non-conflicting pending by priority: score desc, submitted_at asc, id asc
            active_pending = list(non_conflicting)
            active_pending.sort(key=lambda p: (-p.confidence_score, p.submitted_at or 0, p.id))
            
            # Group updates to commit together at the end of the iteration
            status_updates = []
            
            while len(active_pending) > 0 and batch_index < max_non_conflicting_batches:
                if self._is_time_budget_exceeded(started_at, max_processing_seconds):
                    phase_timing["hit_time_cap_count"] = 1.0
                    break

                # Assemble batch from pending list based on spatial eligibility
                batch = []
                remaining = []
                for p in active_pending:
                    if p.status not in (ProposalStatus.PENDING, ProposalStatus.REBASING):
                        continue
                    if len(batch) >= batch_size:
                        remaining.append(p)
                        continue

                    # Check temporal conflicts with recently promoted proposals
                    temporal_conflicts = self._find_temporal_conflicts(repo_id, p)
                    if temporal_conflicts:
                        can_merge = False
                        if self.semantic_merge_enabled:
                            can_merge = True
                            for tc in temporal_conflicts:
                                if self._is_structurally_mergeable(p, tc):
                                    continue
                                if self.synthesis_worker_enabled:
                                    p_decoded = self.proposal_service._decode_affected_symbols(p.affected_symbols)
                                    tc_decoded = self.proposal_service._decode_affected_symbols(tc.affected_symbols)
                                    if p_decoded and tc_decoded:
                                        common_paths = set(p_decoded.keys()).intersection(tc_decoded.keys())
                                        if len(common_paths) == 1:
                                            path = list(common_paths)[0]
                                            p_symbols = set(p_decoded[path])
                                            tc_symbols = set(tc_decoded[path])
                                            common_symbols = p_symbols.intersection(tc_symbols)
                                            method_symbols = {sym for sym in common_symbols if not sym.startswith("CLASS:") or "::" in sym}
                                            from .structural_merge import is_class_member_node
                                            if len(method_symbols) == 1 and all(is_class_member_node(sym) for sym in method_symbols):
                                                continue
                                can_merge = False
                                break
                        if not can_merge:
                            logger.info("proposal_superseded_by_temporal_semantic_conflict", proposal_id=p.id)
                            status_updates.append((p.id, ProposalStatus.SUPERSEDED, "temporal_semantic_conflict"))
                            self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                            continue

                    # Check spatial overlap with already admitted proposals in batch
                    overlaps = False
                    for active_p in batch:
                        if check_overlap(p, active_p):
                            overlaps = True
                            break
                    if overlaps:
                        # Overlapping: mark SUPERSEDED immediately (neutral loss)
                        logger.info("proposal_superseded_by_spatial_exclusion", proposal_id=p.id)
                        status_updates.append((p.id, ProposalStatus.SUPERSEDED, "spatial_batching_exclusion"))
                        self.trust_service.record_neutral_loss(p.agent_id, commit=False)
                    else:
                        batch.append(p)

                active_pending = remaining

                if status_updates:
                    await self.proposal_service.update_status_many(status_updates)
                    status_updates = []

                if not batch:
                    continue

                batch_index += 1
                phase_timing["processed_non_conflicting_batches"] += 1

                candidate: Optional[PromotionCandidate] = None
                max_pg_retries = 5
                for pg_attempt in range(max_pg_retries):
                    try:
                        await self._acquire_contention_lock(batch)
                        self.session.expire_all()
                        for p in batch:
                            self.session.refresh(p)
                        if any(p.status not in (ProposalStatus.PENDING, ProposalStatus.REBASING) for p in batch):
                            logger.info("batch_proposals_no_longer_pending_skipping", proposal_ids=[p.id for p in batch])
                            break
                        evaluate_started = time.perf_counter()
                        try:
                            candidate = await self.evaluate_batch(batch, document_store)
                        except GateTimeoutError as e:
                            self.session.rollback()
                            updates: list[tuple[str, ProposalStatus, Optional[str]]] = []
                            for proposal in batch:
                                self.session.refresh(proposal)
                                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                                is_semantic_merge = (
                                    self.semantic_merge_enabled
                                    and cont_meta is not None
                                    and cont_meta.contention_policy == "semantic_merge"
                                )
                                if is_semantic_merge:
                                    repo = self.session.get(Repository, proposal.repo_id)
                                    max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                                    
                                    proposal.rebase_attempts = (proposal.rebase_attempts or 0) + 1
                                    self.session.add(proposal)
                                    
                                    if proposal.rebase_attempts <= max_rebase:
                                        updates.append((proposal.id, ProposalStatus.REBASING, f"gate_timeout_requeue: {str(e)[:160]}"))
                                    else:
                                        reason_json = json.dumps({
                                            "event_reason": "SUPERSEDED_GATE_TIMEOUT",
                                            "error": str(e)
                                        })
                                        updates.append((proposal.id, ProposalStatus.SUPERSEDED, reason_json))
                                        self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                                else:
                                    updates.append((proposal.id, ProposalStatus.PENDING, f"gate_timeout_requeue: {str(e)[:160]}"))
                            await self.proposal_service.update_status_many(updates)
                            self.session.commit()
                            break
                        except Exception as e:
                            self.session.rollback()
                            updates = []
                            for proposal in batch:
                                self.session.refresh(proposal)
                                cont_meta = self.proposal_service.get_contention_metadata(proposal.id)
                                is_semantic_merge = (
                                    self.semantic_merge_enabled
                                    and cont_meta is not None
                                    and cont_meta.contention_policy == "semantic_merge"
                                )
                                if is_semantic_merge:
                                    if isinstance(e, ValueError) and "conflict" in str(e).lower():
                                        reason_json = json.dumps({
                                            "event_reason": "SUPERSEDED_SEMANTIC_CONFLICT",
                                            "error": str(e)
                                        })
                                        updates.append((proposal.id, ProposalStatus.SUPERSEDED, reason_json))
                                        self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                                    else:
                                        repo = self.session.get(Repository, proposal.repo_id)
                                        max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
                                        
                                        proposal.rebase_attempts = (proposal.rebase_attempts or 0) + 1
                                        self.session.add(proposal)
                                        
                                        if proposal.rebase_attempts <= max_rebase:
                                            updates.append((proposal.id, ProposalStatus.REBASING, f"evaluation_error_requeue: {str(e)[:160]}"))
                                        else:
                                            reason_json = json.dumps({
                                                "event_reason": "SUPERSEDED_EVALUATION_ERROR",
                                                "error": str(e)
                                            })
                                            updates.append((proposal.id, ProposalStatus.SUPERSEDED, reason_json))
                                            self.trust_service.record_neutral_loss(proposal.agent_id, commit=False)
                                else:
                                    updates.append((proposal.id, ProposalStatus.QUARANTINED, self._format_exception_reason(e, prefix="evaluation_error")))
                            await self.proposal_service.update_status_many(updates)
                            self.session.commit()
                            break
                        phase_timing["candidates_built_count"] += 1.0
                        phase_timing["evaluate_ms"] += (time.perf_counter() - evaluate_started) * 1000.0
                        if candidate.timings:
                            phase_timing["clone_ms"] += candidate.timings.get("clone_ms", 0.0)
                            phase_timing["snapshot_ms"] += candidate.timings.get("snapshot_ms", 0.0)
                            phase_timing["snapshot_hash_ms"] += candidate.timings.get("snapshot_hash_ms", 0.0)
                            phase_timing["snapshot_materialize_ms"] += candidate.timings.get("snapshot_materialize_ms", 0.0)
                            phase_timing["snapshot_db_commit_ms"] += candidate.timings.get("snapshot_db_commit_ms", 0.0)
                            phase_timing["snapshot_publish_ms"] += candidate.timings.get("snapshot_publish_ms", 0.0)
                            phase_timing["gates_ms"] += candidate.timings.get("gates_ms", 0.0)
                            self._accumulate_snapshot_subphase_timings(phase_timing, candidate.timings)

                        if candidate.score > 0:
                            promote_started = time.perf_counter()
                            try:
                                promotion = await self.promote(
                                    repo_id, candidate, document_store, triggered_by="batch"
                                )
                                promotions.append(promotion)
                            except ContentionWinnerExistsError:
                                logger.info("contention_winner_exists_skipping_promotion_batch")
                                break
                            phase_timing["promote_ms"] += (time.perf_counter() - promote_started) * 1000.0
                            phase_timing["commit_ms"] += self.get_last_promotion_phase_timing().get("commit_total_ms", 0.0)
                            phase_timing["processed_non_conflicting_proposals"] += len(candidate.proposals)
                            phase_timing["promotions_created_count"] += 1.0
                            phase_timing["proposals_promoted_count"] += float(len(candidate.proposals))

                            # Update document store with promoted changes
                            # Since a proposal can be merged semantically, we apply the final file content from the snapshot.
                            all_changed_paths = set()
                            for proposal in candidate.proposals:
                                all_changed_paths.update(self._parse_affected_paths(proposal))
                            for path in all_changed_paths:
                                final_content = self.snapshot_service.get_file_content(candidate.snapshot_id, path)
                                if final_content is not None:
                                    document_store.apply_full_file(path, final_content, f"promoted_{candidate.snapshot_id}")
                        break # Success: exit retry loop
                    except ExternalCommitAbortedError:
                        phase_timing["external_commit_abort_count"] += 1.0
                        await self._recover_external_commit_abort(
                            candidate.proposals if candidate is not None else []
                        )
                        stop_iteration = True
                        logger.warning(
                            "external_commit_aborted_batch",
                            repo_id=repo_id,
                            proposal_ids=[p.id for p in candidate.proposals] if candidate is not None else [],
                        )
                        break
                    except Exception as e:
                        self.session.rollback()
                        from sqlalchemy.exc import DBAPIError
                        is_pg_retryable = False
                        if isinstance(e, DBAPIError) and e.orig:
                            pgcode = getattr(e.orig, "pgcode", None)
                            if pgcode in ("40001", "40P01"):
                                is_pg_retryable = True
                        
                        if is_pg_retryable and pg_attempt < max_pg_retries - 1:
                            logger.warning("pg_serialization_retry", attempt=pg_attempt, error=str(e))
                            import random
                            await asyncio.sleep(0.05 * (2 ** pg_attempt) * (0.5 + random.random()))
                            continue # Retry the loop!

                        if is_pg_retryable and pg_attempt == max_pg_retries - 1:
                            logger.error("serialization_retry_exhausted_escalation", error=str(e))
                            try:
                                await self.proposal_service.update_status_many(
                                    [(p.id, ProposalStatus.QUARANTINED, "serialization_retry_exhausted") for p in batch]
                                )
                                self.session.commit()
                            except Exception as q_err:
                                logger.error("failed_to_quarantine_on_serialization_retry_exhausted", error=str(q_err))

                        if "lock_acquisition_timeout:" in str(e):
                            logger.error("lock_timeout_escalation", error=str(e))
                            try:
                                await self.proposal_service.update_status_many(
                                    [(p.id, ProposalStatus.QUARANTINED, "lock_wait_timeout") for p in batch]
                                )
                                self.session.commit()
                            except Exception as q_err:
                                logger.error("failed_to_quarantine_on_lock_timeout", error=str(q_err))
                        else:
                            logger.error(
                                "batch_promotion_failed",
                                error=str(e),
                            )
                        break
                    finally:
                        await self._release_contention_lock()

            if len(active_pending) > 0:
                phase_timing["hit_batch_cap_count"] = 1.0

            if status_updates:
                await self.proposal_service.update_status_many(status_updates)

        async def _process_conflict_sets() -> None:
            nonlocal stop_iteration
            processed = 0
            successful_conflict_promotions = 0
            for conflict_set in conflict_sets:
                if stop_iteration:
                    break
                if effective_conflict_cap and successful_conflict_promotions >= effective_conflict_cap:
                    break
                if self._is_time_budget_exceeded(started_at, max_processing_seconds):
                    phase_timing["hit_time_cap_count"] = 1.0
                    break
                
                phase_timing["conflict_sets_seen_count"] += 1.0
                best: Optional[PromotionCandidate] = None
                max_pg_retries = 5
                for pg_attempt in range(max_pg_retries):
                    try:
                        # Fetch conflict set proposals to derive lock key and acquire lock
                        cs_proposal_ids = json.loads(conflict_set.proposal_ids)
                        cs_proposals = self.proposal_service.get_proposals_by_ids(cs_proposal_ids)
                        await self._acquire_contention_lock(cs_proposals)
                        self.session.expire_all()
                        self.session.refresh(conflict_set)
                        if conflict_set.resolved:
                            logger.info("conflict_set_already_resolved_skipping", conflict_set_id=conflict_set.id)
                            break

                        for p in cs_proposals:
                            self.session.refresh(p)
                        if all(p.status != ProposalStatus.PENDING for p in cs_proposals):
                            logger.info("conflict_set_proposals_no_longer_pending_skipping", conflict_set_id=conflict_set.id)
                            break

                        evaluate_started = time.perf_counter()
                        best = await self.select_best_candidate(conflict_set, document_store)
                        phase_timing["evaluate_ms"] += (time.perf_counter() - evaluate_started) * 1000.0
                        if not best:
                            phase_timing["conflict_sets_no_candidate_count"] += 1.0
                            break # No candidate found: exit retry loop
                        phase_timing["candidates_built_count"] += 1.0

                        # Accumulate timings...
                        phase_timing["clone_ms"] += best.timings.get("clone_ms", 0.0)
                        phase_timing["snapshot_ms"] += best.timings.get("snapshot_ms", 0.0)
                        phase_timing["snapshot_hash_ms"] += best.timings.get("snapshot_hash_ms", 0.0)
                        phase_timing["snapshot_materialize_ms"] += best.timings.get("snapshot_materialize_ms", 0.0)
                        phase_timing["snapshot_db_commit_ms"] += best.timings.get("snapshot_db_commit_ms", 0.0)
                        phase_timing["snapshot_publish_ms"] += best.timings.get("snapshot_publish_ms", 0.0)
                        phase_timing["gates_ms"] += best.timings.get("gates_ms", 0.0)
                        self._accumulate_snapshot_subphase_timings(phase_timing, best.timings)
                        phase_timing["processed_conflict_sets"] += 1

                        if best.score > 0:
                            promote_started = time.perf_counter()
                            try:
                                promotion = await self.promote(
                                    repo_id, best, document_store, triggered_by="conflict_resolution"
                                )
                                promotions.append(promotion)
                            except ContentionWinnerExistsError:
                                logger.info("contention_winner_exists_skipping_promotion_conflict_set", conflict_set_id=conflict_set.id)
                                break
                            phase_timing["promote_ms"] += (time.perf_counter() - promote_started) * 1000.0
                            phase_timing["commit_ms"] += self.get_last_promotion_phase_timing().get("commit_total_ms", 0.0)
                            phase_timing["processed_conflicting_promotions"] += len(best.proposals)
                            phase_timing["promotions_created_count"] += 1.0
                            phase_timing["proposals_promoted_count"] += float(len(best.proposals))
                            successful_conflict_promotions += 1

                            # Update document store with promoted changes
                            for proposal in best.proposals:
                                await self._apply_proposal_change(document_store, proposal)
                        else:
                            to_reject = [(proposal.id, ProposalStatus.REJECTED, "non_positive_score") for proposal in best.proposals]
                            cs_proposal_ids = json.loads(conflict_set.proposal_ids)
                            cs_proposals = self.proposal_service.get_proposals_by_ids(cs_proposal_ids)
                            for p in cs_proposals:
                                if p.id not in [x.id for x in best.proposals] and p.status in (ProposalStatus.PENDING, ProposalStatus.EVALUATING):
                                    to_reject.append((p.id, ProposalStatus.REJECTED, "conflict_sibling_failed_gates"))
                            await self.proposal_service.update_status_many(to_reject)
                        break # Success: exit retry loop
                    except ExternalCommitAbortedError:
                        phase_timing["external_commit_abort_count"] += 1.0
                        await self._recover_external_commit_abort(
                            best.proposals if best is not None else []
                        )
                        stop_iteration = True
                        logger.warning(
                            "external_commit_aborted_conflict_set",
                            repo_id=repo_id,
                            conflict_set_id=conflict_set.id,
                            proposal_ids=[p.id for p in best.proposals] if best is not None else [],
                        )
                        break
                    except Exception as e:
                        self.session.rollback()
                        from sqlalchemy.exc import DBAPIError
                        is_pg_retryable = False
                        if isinstance(e, DBAPIError) and e.orig:
                            pgcode = getattr(e.orig, "pgcode", None)
                            if pgcode in ("40001", "40P01"):
                                is_pg_retryable = True
                        
                        if is_pg_retryable and pg_attempt < max_pg_retries - 1:
                            logger.warning("pg_serialization_retry_conflict_set", attempt=pg_attempt, error=str(e))
                            import random
                            await asyncio.sleep(0.05 * (2 ** pg_attempt) * (0.5 + random.random()))
                            continue # Retry the loop!

                        if is_pg_retryable and pg_attempt == max_pg_retries - 1:
                            logger.error("serialization_retry_exhausted_escalation", error=str(e), conflict_set_id=conflict_set.id)
                            try:
                                cs_proposal_ids = json.loads(conflict_set.proposal_ids)
                                await self.proposal_service.update_status_many(
                                    [(pid, ProposalStatus.QUARANTINED, "serialization_retry_exhausted") for pid in cs_proposal_ids]
                                )
                                self.session.commit()
                            except Exception as q_err:
                                logger.error("failed_to_quarantine_on_serialization_retry_exhausted", error=str(q_err))

                        if "lock_acquisition_timeout:" in str(e):
                            logger.error("lock_timeout_escalation", error=str(e), conflict_set_id=conflict_set.id)
                            try:
                                cs_proposal_ids = json.loads(conflict_set.proposal_ids)
                                await self.proposal_service.update_status_many(
                                    [(pid, ProposalStatus.QUARANTINED, "lock_wait_timeout") for pid in cs_proposal_ids]
                                )
                                self.session.commit()
                            except Exception as q_err:
                                logger.error("failed_to_quarantine_on_lock_timeout", error=str(q_err))
                        else:
                            logger.error(
                                "conflict_resolution_failed",
                                conflict_set_id=conflict_set.id,
                                error=str(e),
                            )
                        break
                    finally:
                        await self._release_contention_lock()
                processed += 1
            if processed < len(conflict_sets):
                phase_timing["hit_batch_cap_count"] = 1.0

        if prioritize_non_conflicting:
            if not stop_iteration and not self._is_time_budget_exceeded(started_at, max_processing_seconds):
                await _process_non_conflicting()
            if not stop_iteration and not self._is_time_budget_exceeded(started_at, max_processing_seconds):
                await _process_conflict_sets()
        else:
            if not stop_iteration and not self._is_time_budget_exceeded(started_at, max_processing_seconds):
                await _process_conflict_sets()
            if not stop_iteration and not self._is_time_budget_exceeded(started_at, max_processing_seconds):
                await _process_non_conflicting()

        phase_timing["total_ms"] = (time.perf_counter() - started_at) * 1000.0
        self._last_iteration_timing = phase_timing

        try:
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.exception("commit_final_status_updates_failed", error=str(e))

        logger.info(
            "promotion_iteration_timing",
            repo_id=repo_id,
            timing=phase_timing,
            pending_count=len(pending),
            conflict_sets_seen=len(conflict_sets),
            conflict_sets_processed=int(phase_timing.get("conflict_sets_seen_count", 0)),
            conflict_sets_with_candidate=int(phase_timing.get("processed_conflict_sets", 0)),
            promotions=len(promotions),
        )
        return promotions

    def get_promotion(self, promotion_id: str) -> Optional[Promotion]:
        """Get a promotion by ID."""
        return self.session.get(Promotion, promotion_id)

    def get_operation_node(self, operation_node_id: str) -> Optional[OperationGraphNode]:
        """Get an operation graph node by ID."""
        return self.session.get(OperationGraphNode, operation_node_id)

    def list_operation_nodes(self, repo_id: str, limit: int = 100) -> list[OperationGraphNode]:
        """List operation graph nodes for a repository."""
        statement = (
            select(OperationGraphNode)
            .where(OperationGraphNode.repo_id == repo_id)
            .order_by(OperationGraphNode.to_head_sequence.desc(), OperationGraphNode.created_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement))

    def verify_promotion_provenance(self, promotion_id: str) -> dict[str, object]:
        """
        Verify deterministic promotion provenance for a promotion record.

        Returns a structured verification result with pass/fail and diagnostics.
        """
        promotion = self.get_promotion(promotion_id)
        if not promotion:
            raise ValueError(f"Promotion not found: {promotion_id}")

        try:
            evidence = json.loads(promotion.evidence)
        except Exception:
            evidence = {}

        result: dict[str, object] = {
            "promotion_id": promotion.id,
            "repo_id": promotion.repo_id,
            "verified": False,
            "checks": {},
            "errors": [],
        }

        required_keys = (
            "_op_graph_node",
            "_op_graph_parent",
            "_replay_from_hash",
            "_replay_to_hash",
            "_replay_gate_vector",
        )
        missing = [key for key in required_keys if key not in evidence]
        if missing:
            result["errors"] = [f"missing evidence keys: {', '.join(missing)}"]
            return result

        try:
            included_ids = json.loads(promotion.included_proposals)
            if not isinstance(included_ids, list) or not all(isinstance(x, str) for x in included_ids):
                raise ValueError("Not a list of strings")
        except Exception:
            result["errors"] = ["malformed included_proposals JSON"]
            return result

        try:
            excluded_ids = json.loads(promotion.excluded_proposals)
            if not isinstance(excluded_ids, list) or not all(isinstance(x, str) for x in excluded_ids):
                raise ValueError("Not a list of strings")
        except Exception:
            result["errors"] = ["malformed excluded_proposals JSON"]
            return result

        proposals = self.proposal_service.get_proposals_by_ids(
            included_ids,
            repo_id=promotion.repo_id,
        )
        if len(proposals) != len(included_ids):
            result["errors"] = ["one or more included proposals are missing"]
            return result

        head_statement = (
            select(AetherHead)
            .where(AetherHead.promotion_id == promotion.id)
            .order_by(AetherHead.created_at.desc())
        )
        head = self.session.exec(head_statement).first()
        if not head:
            result["errors"] = ["no head record found for promotion"]
            return result

        operation_node = self.session.exec(
            select(OperationGraphNode)
            .where(OperationGraphNode.promotion_id == promotion.id)
            .order_by(OperationGraphNode.created_at.desc())
        ).first()
        if not operation_node:
            result["errors"] = ["no operation graph node found for promotion"]
            return result

        snapshot = self.snapshot_service.get_snapshot(head.snapshot_id)
        if not snapshot:
            result["errors"] = [f"snapshot not found: {head.snapshot_id}"]
            return result

        gate_results = list(
            self.session.exec(
                select(GateResult).where(GateResult.snapshot_id == head.snapshot_id)
            )
        )
        db_gate_vector = sorted(
            f"{gate.gate_name}:{gate.status.value}"
            for gate in gate_results
        )
        stored_gate_vector = sorted(
            token
            for token in str(evidence.get("_replay_gate_vector", "")).split("|")
            if token
        )
        gate_vector_for_hash = stored_gate_vector if stored_gate_vector else db_gate_vector

        replay_gate_results: list[GateResult] = []
        for entry in gate_vector_for_hash:
            if ":" not in entry:
                continue
            gate_name, status_str = entry.split(":", 1)
            try:
                status = GateStatus(status_str)
            except ValueError:
                continue
            replay_gate_results.append(
                GateResult(
                    id=generate_id("gate"),
                    repo_id=promotion.repo_id,
                    snapshot_id=head.snapshot_id,
                    gate_name=gate_name,
                    status=status,
                )
            )

        verification_evidence = self._build_provenance_evidence(
            repo_id=promotion.repo_id,
            candidate=PromotionCandidate(
                proposals=proposals,
                score=promotion.final_score,
                evidence={},
                gate_results=replay_gate_results,
                snapshot_id=head.snapshot_id,
                excluded_proposal_ids=excluded_ids,
            ),
            from_head_id=promotion.from_head_id,
            from_head_sequence=promotion.from_head_sequence,
            to_head_sequence=promotion.to_head_sequence,
            from_content_hash=str(evidence["_replay_from_hash"]),
            to_content_hash=snapshot.content_hash,
            triggered_by=promotion.triggered_by,
        )

        stored_node = str(evidence.get("_op_graph_node", ""))
        recomputed_node = verification_evidence["_op_graph_node"]
        node_match = stored_node == recomputed_node
        to_hash_match = str(evidence.get("_replay_to_hash", "")) == snapshot.content_hash
        gate_vector_match = stored_gate_vector == db_gate_vector if db_gate_vector else True
        op_node_hash_match = operation_node.node_hash == stored_node
        op_node_to_hash_match = operation_node.replay_to_hash == snapshot.content_hash
        op_node_gate_vector = sorted(
            token
            for token in operation_node.replay_gate_vector.split("|")
            if token
        )
        op_node_gate_vector_match = op_node_gate_vector == db_gate_vector if db_gate_vector else True

        result["checks"] = {
            "node_hash_match": node_match,
            "replay_to_hash_match": to_hash_match,
            "gate_vector_match": gate_vector_match,
            "operation_node_hash_match": op_node_hash_match,
            "operation_node_replay_to_hash_match": op_node_to_hash_match,
            "operation_node_gate_vector_match": op_node_gate_vector_match,
            "stored_op_graph_node": stored_node,
            "recomputed_op_graph_node": recomputed_node,
            "operation_node_id": operation_node.id,
            "snapshot_id": head.snapshot_id,
        }
        result["verified"] = (
            node_match
            and to_hash_match
            and gate_vector_match
            and op_node_hash_match
            and op_node_to_hash_match
            and op_node_gate_vector_match
        )
        if not result["verified"]:
            errors: list[str] = []
            if not node_match:
                errors.append("op graph node hash mismatch")
            if not to_hash_match:
                errors.append("replay target hash does not match snapshot")
            if not gate_vector_match:
                errors.append("gate vector does not match stored gate results")
            if not op_node_hash_match:
                errors.append("operation node hash does not match promotion evidence")
            if not op_node_to_hash_match:
                errors.append("operation node replay target hash mismatch")
            if not op_node_gate_vector_match:
                errors.append("operation node gate vector mismatch")
            result["errors"] = errors

        return result

    def get_promotion_history(
        self,
        repo_id: str,
        limit: int = 50,
    ) -> list[Promotion]:
        """Get promotion history for a repository."""
        statement = (
            select(Promotion)
            .where(Promotion.repo_id == repo_id)
            .order_by(Promotion.created_at.desc())
            .limit(limit)
        )
        return list(self.session.exec(statement))

    async def evaluate_and_promote_cohort(
        self,
        repo_id: str,
        proposal_ids: list[str],
        document_store: DocumentStore,
    ) -> CohortResult:
        """
        Evaluate and promote a cohort of deferred proposals together.
        Ensures same-base checks, structural merge folding, gates verification,
        clearing deferral, and bypassing conflict resolution.
        """
        async with self._promotion_lock:
            # 1. Fetch and validate all proposals
            # Check duplicate proposal IDs first
            if len(proposal_ids) != len(set(proposal_ids)):
                raise ValueError("duplicated ID validation error")

            # Check cohort size
            if len(proposal_ids) < 2:
                raise ValueError("single proposal validation error")

            proposals = []
            for pid in proposal_ids:
                p = self.session.get(Proposal, pid)
                if not p:
                    raise ValueError("Some proposal IDs in the cohort do not exist")
                if p.repo_id != repo_id:
                    raise ValueError("cross-repo validation error")
                proposals.append(p)

            # Check statuses and deferral: all must be PENDING and defer_evaluation=True
            for p in proposals:
                cont_meta = self.proposal_service.get_contention_metadata(p.id)
                if p.status != ProposalStatus.PENDING or not cont_meta or not cont_meta.defer_evaluation:
                    raise ValueError("non-deferred validation error: Proposal is not marked for deferred evaluation")

            # Assert that all proposals belong to the exact same repository and the exact same contention group
            contention_groups = set()
            for p in proposals:
                cont_meta = self.proposal_service.get_contention_metadata(p.id)
                if not cont_meta or not cont_meta.contention_group_id:
                    raise ValueError("proposal missing contention group")
                contention_groups.add(cont_meta.contention_group_id)
            if len(contention_groups) != 1:
                raise ValueError("multiple contention groups in cohort validation error")

            # Assert that the contention policy for all cohort proposals is exactly "semantic_merge"
            for p in proposals:
                cont_meta = self.proposal_service.get_contention_metadata(p.id)
                if not cont_meta or cont_meta.contention_policy != "semantic_merge":
                    raise ValueError("mixed policy validation error")

            # Assert that all cohort proposals share the same source_run_id (mixed runs must be atomically rejected)
            run_ids = {p.source_run_id for p in proposals}
            if len(run_ids) != 1:
                raise ValueError("mixed runs must be atomically rejected")

            # 2. Same-base and head sequence check
            repo = self.session.get(Repository, repo_id)
            if not repo:
                raise ValueError(f"Repository not found: {repo_id}")

            base_head_ids = {p.base_head_id for p in proposals}
            if len(base_head_ids) > 1:
                raise ValueError("stale base validation error")
                
            if repo.head_id not in base_head_ids:
                reason_json = json.dumps({"event_reason": "SUPERSEDED_SEMANTIC_CONFLICT"})
                status_updates = [(p.id, ProposalStatus.SUPERSEDED, reason_json) for p in proposals]
                await self.proposal_service.update_status_many(status_updates, commit=True)
                
                for p in proposals:
                    paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p.affected_paths, []))
                    for path in paths:
                        self.proposal_service._record_metric_event(
                            event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                            repo_id=repo_id,
                            proposal_id=p.id,
                            reason="STALE_BASE",
                            path=path,
                        )
                self.session.commit()
                outcomes = [{"proposal_id": p.id, "status": ProposalStatus.SUPERSEDED.value, "reason": "SUPERSEDED_SEMANTIC_CONFLICT"} for p in proposals]
                return CohortResult(promotion=None, proposal_outcomes=outcomes)

            # Keep original state in memory for cleanup/rollback if anything fails
            original_states = {}
            for p in proposals:
                cont_meta = self.proposal_service.get_contention_metadata(p.id)
                original_states[p.id] = {
                    "conflict_set_id": p.conflict_set_id,
                    "defer_evaluation": cont_meta.defer_evaluation if cont_meta else None,
                    "contention_policy": cont_meta.contention_policy if cont_meta else None,
                }

            try:
                # 3. Clear deferred flag
                for p in proposals:
                    cont_meta = self.proposal_service.get_contention_metadata(p.id)
                    if cont_meta:
                        cont_meta.defer_evaluation = False
                        self.session.add(cont_meta)
                self.session.flush()

                # 4. Structural merge folding
                candidate_store = document_store.clone()
                batch_changed_paths = set()
                merge_failed = False
                failed_reason = None
                failed_path = None
                failed_proposal = None

                # Sort proposals by submitted_at to apply them in submission order
                sorted_proposals = sorted(proposals, key=lambda p: p.submitted_at)
                
                from .structural_merge import attempt_structural_merge
                for p in sorted_proposals:
                    paths = self.proposal_service._normalize_paths(self.proposal_service._safe_json_loads(p.affected_paths, []))
                    for path in paths:
                        base_content = self.snapshot_service.get_file_content(p.base_head_id, path) or ""
                        current_content = candidate_store.get_file_content(path) or ""
                        content_b = self._get_proposal_content(p, path, document_store)
                        
                        if current_content.strip() != base_content.strip():
                            # Apply structural merge
                            merged, failure_reason = attempt_structural_merge(base_content, current_content, content_b)
                            if merged is not None:
                                logger.info("structural_merge_applied_cohort", proposal_id=p.id, path=path)
                                candidate_store.apply_full_file(path, merged, p.id)
                                batch_changed_paths.add(path)
                                self.proposal_service._record_metric_event(
                                    event_type=SystemMetricEventType.STRUCTURAL_MERGE,
                                    repo_id=repo_id,
                                    proposal_id=p.id,
                                    reason="structural_merge_applied",
                                    path=path,
                                )
                            else:
                                # Merge failed
                                merge_failed = True
                                failed_reason = failure_reason or "merge_conflict"
                                failed_path = path
                                failed_proposal = p
                                break
                        else:
                            # Apply directly
                            candidate_store.apply_full_file(path, content_b, p.id)
                            batch_changed_paths.add(path)
                            self.proposal_service._record_metric_event(
                                event_type=SystemMetricEventType.STRUCTURAL_MERGE,
                                repo_id=repo_id,
                                proposal_id=p.id,
                                reason="structural_merge_applied",
                                path=path,
                            )
                    if merge_failed:
                        break

                if merge_failed:
                    # Record SEMANTIC_FALLBACK with the specific failure reason
                    self.proposal_service._record_metric_event(
                        event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                        repo_id=repo_id,
                        proposal_id=failed_proposal.id,
                        reason=failed_reason,
                        path=failed_path,
                    )
                    self.session.commit()
                    outcomes = [{"proposal_id": p.id, "status": ProposalStatus.SUPERSEDED.value, "reason": "SUPERSEDED_SEMANTIC_CONFLICT"} for p in proposals]
                    return CohortResult(promotion=None, proposal_outcomes=outcomes)

                # 5. Snapshot + gates verification
                snapshot = await self.snapshot_service.create_snapshot(repo_id, candidate_store, proposal_ids)
                gates = self.get_repo_gates(repo_id)
                
                gate_workspace = self.snapshot_service.materialize_gate_workspace(snapshot.id)
                try:
                    if gate_workspace:
                        gate_results = await self.gate_runner.run_all_gates(
                            snapshot.id,
                            gates,
                            gate_workspace,
                            execution_mode=self.gate_execution_mode,
                            max_parallel=self.gate_max_parallel,
                        )
                    else:
                        gate_results = []
                finally:
                    if gate_workspace:
                        self.snapshot_service.cleanup_gate_workspace(gate_workspace)

                if not self.gate_runner.all_required_gates_pass(gate_results, gates):
                    # Emit BATCH_GATE_FAIL metric
                    self.proposal_service._record_metric_event(
                        event_type=SystemMetricEventType.BATCH_GATE_FAIL,
                        repo_id=repo_id,
                        proposals=proposals,
                        reason="cohort_gate_failure",
                    )
                    # Mark all as REJECTED
                    status_updates = [(p.id, ProposalStatus.REJECTED, "cohort_gate_failure") for p in proposals]
                    await self.proposal_service.update_status_many(status_updates, commit=True)
                    self.session.commit()
                    outcomes = [{"proposal_id": p.id, "status": ProposalStatus.REJECTED.value, "reason": "cohort_gate_failure"} for p in proposals]
                    return CohortResult(promotion=None, proposal_outcomes=outcomes)

                # 6. Build PromotionCandidate containing ALL proposals
                # Clear conflict_set_id on all proposals to bypass resolve_conflict_set() in _promote_internal
                for p in proposals:
                    p.conflict_set_id = None
                    self.session.add(p)
                self.session.flush()

                # Score the candidate using weights
                first_p = proposals[0]
                _, policy_score, confidence_score = self.compute_score(first_p, gate_results, gates)
                score = (policy_score * self.weights.tests) + (confidence_score * self.weights.confidence)
                
                candidate = PromotionCandidate(
                    proposals=proposals,
                    score=score,
                    evidence={"merged_paths": json.dumps(list(batch_changed_paths))},
                    gate_results=gate_results,
                    snapshot_id=snapshot.id,
                )

                # 7. Promote via _promote_internal
                promotion = await self._promote_internal(
                    repo_id=repo_id,
                    candidate=candidate,
                    document_store=document_store,
                    triggered_by="cohort",
                )

                # 8. Terminalize any lingering conflict sets referencing these proposal IDs
                all_sets = self.session.exec(
                    select(ConflictSet).where(ConflictSet.repo_id == repo_id, ConflictSet.resolved == False)
                ).all()
                for cs in all_sets:
                    try:
                        cs_proposal_ids = json.loads(cs.proposal_ids)
                        if any(pid in cs_proposal_ids for pid in proposal_ids):
                            cs.resolved = True
                            cs.resolved_at = datetime.now(UTC)
                            cs.active_choice_id = "cohort:" + ":".join(sorted(proposal_ids))
                            self.session.add(cs)
                    except Exception:
                        pass
                self.session.commit()

                outcomes = [{"proposal_id": p.id, "status": ProposalStatus.PROMOTED.value, "reason": None} for p in proposals]
                return CohortResult(promotion=promotion, proposal_outcomes=outcomes)

            except Exception as e:
                # Rollback changes made in this transaction
                try:
                    self.session.rollback()
                except Exception:
                    pass
                
                # Restore original states from memory and commit
                for p in proposals:
                    orig = original_states.get(p.id)
                    if orig:
                        p.conflict_set_id = orig["conflict_set_id"]
                        self.session.add(p)
                        cont_meta = self.proposal_service.get_contention_metadata(p.id)
                        if cont_meta:
                            cont_meta.defer_evaluation = orig["defer_evaluation"]
                            cont_meta.contention_policy = orig["contention_policy"]
                            self.session.add(cont_meta)
                try:
                    self.session.commit()
                except Exception:
                    pass
                raise e
