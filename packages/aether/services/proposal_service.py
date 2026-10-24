"""
Proposal service for handling agent submissions.

Handles:
- Proposal submission and validation
- Conflict detection and conflict set management
- Proposal status tracking
"""

import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional, Union
import structlog

from sqlalchemy import func
from sqlmodel import Session, select

from dataclasses import dataclass

@dataclass
class ProposalProvenance:
    proposal_id: str
    event_type: str
    event_reason: Optional[str]
    created_at: datetime
    contention_group_id: Optional[str] = None
    contention_policy: Optional[str] = None
    winner_proposal_id: Optional[str] = None
    previous_winner_proposal_id: Optional[str] = None
    target_paths: Optional[list[str]] = None
    synthesis_engine: Optional[str] = None


from ..models.entities import (
    Proposal,
    ProposalStatus,
    ProposalCreate,
    ProposalResponse,
    ProposalSource,
    ConflictSet,
    Repository,
    SystemMetricEvent,
    SystemMetricEventType,
    ProposalContention,
)
from ..models.aip import extract_aip_paths_and_symbols
from ..models.events import AetherEvent, EventType
from ..crdt.document_store import (
    DocumentStore,
    parse_unified_diff,
    validate_repo_relative_path,
)
from .repository_service import RepositoryService
from .event_bus import EventBus, create_proposal_event
from .symbol_adapters import extract_symbol_spans, find_symbol_at_line
from .trust_service import TrustService

logger = structlog.get_logger()


class ProposalService:
    """Service for managing proposals."""
    _SUPPORTED_CHANGE_TYPES = {"unified_diff", "full_file", "aip.v0", "aip_mutation"}
    _submit_executor: ThreadPoolExecutor | None = None
    _submit_executor_lock = threading.Lock()

    def __init__(
        self,
        session: Session,
        event_bus: EventBus,
        repo_service: RepositoryService | dict[str, DocumentStore],
        submit_in_worker: bool = False,
        session_factory: Optional[Callable[[], Session]] = None,
        worker_threads: int = 4,
        defer_conflict_detection: bool = False,
    ):
        self.session = session
        self.event_bus = event_bus
        self.repo_service = repo_service
        self.trust_service = TrustService(session)
        self._session_factory = session_factory
        self._submit_in_worker = bool(submit_in_worker and session_factory is not None)
        self._worker_threads = max(1, worker_threads)
        self._defer_conflict_detection = defer_conflict_detection
        self._last_submit_timings: dict[str, float] = {}
        if self._submit_in_worker:
            self._ensure_submit_executor(self._worker_threads)

    @classmethod
    def _ensure_submit_executor(cls, worker_threads: int) -> None:
        """Initialize the global submit worker pool once."""
        if cls._submit_executor is not None:
            return
        with cls._submit_executor_lock:
            if cls._submit_executor is None:
                cls._submit_executor = ThreadPoolExecutor(
                    max_workers=max(1, worker_threads),
                    thread_name_prefix="aether-submit",
                )

    def get_document_store(self, repo_id: str) -> Optional[DocumentStore]:
        """Get document store for a repository."""
        if isinstance(self.repo_service, dict):
            return self.repo_service.get(repo_id)
        return self.repo_service.get_document_store(repo_id)

    def set_document_store(self, repo_id: str, store: DocumentStore) -> None:
        """Set document store for a repository."""
        if isinstance(self.repo_service, dict):
            self.repo_service[repo_id] = store
            return
        self.repo_service._document_stores[repo_id] = store

    def _record_metric_event(
        self,
        *,
        event_type: SystemMetricEventType,
        repo_id: str,
        proposal_id: Optional[str] = None,
        reason: Optional[str] = None,
        path: Optional[str] = None,
        synthesis_engine: Optional[str] = None,
        source_run_id: Optional[str] = None,
        proposals: Optional[list] = None,
    ) -> None:
        """Record queue-physics telemetry in the same transaction as proposal state."""
        from packages.aether.models.entities import Proposal, SystemMetricEvent
        import json
        
        if proposals and event_type in (SystemMetricEventType.BATCH_SIZE_K, SystemMetricEventType.BATCH_GATE_FAIL):
            run_ids = [p.source_run_id for p in proposals if getattr(p, "source_run_id", None)]
            unique_run_ids = set(run_ids)
            total_batch_size = len(proposals)
            if len(unique_run_ids) <= 1:
                if source_run_id is None:
                    source_run_id = list(unique_run_ids)[0] if unique_run_ids else None
                if reason is None:
                    reason = json.dumps({
                        "total_batch_size": total_batch_size,
                        "mixed_run": False
                    })
                self.session.add(
                    SystemMetricEvent(
                        repo_id=repo_id,
                        proposal_id=proposal_id,
                        event_type=event_type,
                        event_reason=reason,
                        path=path,
                        synthesis_engine=synthesis_engine,
                        source_run_id=source_run_id,
                    )
                )
            else:
                from collections import Counter
                run_member_count = dict(Counter(run_ids))
                for run_id in unique_run_ids:
                    reason_json = json.dumps({
                        "run_member_count": run_member_count,
                        "total_batch_size": total_batch_size,
                        "mixed_run": True
                    })
                    self.session.add(
                        SystemMetricEvent(
                            repo_id=repo_id,
                            proposal_id=proposal_id,
                            event_type=event_type,
                            event_reason=reason_json,
                            path=path,
                            synthesis_engine=synthesis_engine,
                            source_run_id=run_id,
                        )
                    )
            return
                    
        if not source_run_id and proposal_id:
            proposal = self.session.get(Proposal, proposal_id)
            if proposal:
                source_run_id = proposal.source_run_id
                
        self.session.add(
            SystemMetricEvent(
                repo_id=repo_id,
                proposal_id=proposal_id,
                event_type=event_type,
                event_reason=reason,
                path=path,
                synthesis_engine=synthesis_engine,
                source_run_id=source_run_id,
            )
        )

    @staticmethod
    def _normalize_source(source: Optional[str]) -> str:
        """Normalize proposal source labels to the supported contract."""
        if not source:
            return ProposalSource.MANUAL.value
        try:
            return ProposalSource(str(source)).value
        except ValueError:
            return ProposalSource.OTHER.value

    @staticmethod
    def _language_bucket_for_path(path: Optional[str]) -> str:
        """Map file path to a coarse language bucket for fallback telemetry."""
        if not path or "." not in path:
            return "unknown"
        ext = path.rsplit(".", 1)[-1].lower()
        if ext == "py":
            return "python"
        if ext in {"js", "jsx", "mjs", "cjs"}:
            return "javascript"
        if ext in {"ts", "tsx"}:
            return "typescript"
        if ext == "go":
            return "go"
        if ext == "java":
            return "java"
        if ext == "rs":
            return "rust"
        return ext

    @classmethod
    def get_system_physics_snapshot(
        cls,
        session: Session,
        *,
        window_seconds: int = 60,
    ) -> dict[str, Any]:
        """Return current queue-physics metrics from DB-backed telemetry events."""
        window = max(1, int(window_seconds))
        cutoff = datetime.now(UTC) - timedelta(seconds=window)
        query_started = time.perf_counter()

        from sqlmodel import exists
        deferred_subq = select(ProposalContention.proposal_id).where(
            ProposalContention.proposal_id == Proposal.id,
            ProposalContention.defer_evaluation == True,
        )

        deferred_count = int(session.exec(
            select(func.count())
            .select_from(ProposalContention)
            .where(ProposalContention.defer_evaluation == True)
        ).one())

        status_rows = list(
            session.exec(
                select(Proposal.status, func.count())
                .where(
                    Proposal.status.in_(
                        [
                            ProposalStatus.INGESTED,
                            ProposalStatus.PENDING,
                            ProposalStatus.EVALUATING,
                            ProposalStatus.PROMOTED,
                            ProposalStatus.REJECTED,
                        ]
                    ),
                    ~exists(deferred_subq)
                )
                .group_by(Proposal.status)
            )
        )
        status_counts: dict[str, int] = {
            (status.value if isinstance(status, ProposalStatus) else str(status)): int(count)
            for status, count in status_rows
        }
        source_status_rows = list(
            session.exec(
                select(
                    func.coalesce(Proposal.source, ProposalSource.MANUAL.value),
                    Proposal.status,
                    func.count(),
                )
                .where(
                    Proposal.status.in_(
                        [
                            ProposalStatus.INGESTED,
                            ProposalStatus.PENDING,
                            ProposalStatus.EVALUATING,
                            ProposalStatus.PROMOTED,
                            ProposalStatus.REJECTED,
                            ProposalStatus.QUARANTINED,
                            ProposalStatus.SUPERSEDED,
                        ]
                    ),
                    ~exists(deferred_subq)
                )
                .group_by(func.coalesce(Proposal.source, ProposalSource.MANUAL.value), Proposal.status)
            )
        )

        metric_rows = list(
            session.exec(
                select(SystemMetricEvent.event_type, func.count())
                .where(SystemMetricEvent.created_at >= cutoff)
                .group_by(SystemMetricEvent.event_type)
            )
        )
        metric_counts: dict[str, int] = {
            (event_type.value if isinstance(event_type, SystemMetricEventType) else str(event_type)): int(count)
            for event_type, count in metric_rows
        }
        source_metric_rows = list(
            session.exec(
                select(
                    func.coalesce(Proposal.source, ProposalSource.MANUAL.value),
                    SystemMetricEvent.event_type,
                    func.count(),
                )
                .select_from(SystemMetricEvent)
                .join(Proposal, Proposal.id == SystemMetricEvent.proposal_id)
                .where(SystemMetricEvent.created_at >= cutoff)
                .group_by(
                    func.coalesce(Proposal.source, ProposalSource.MANUAL.value),
                    SystemMetricEvent.event_type,
                )
            )
        )
        fallback_path_rows = list(
            session.exec(
                select(SystemMetricEvent.path, func.count())
                .where(
                    SystemMetricEvent.created_at >= cutoff,
                    SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK,
                )
                .group_by(SystemMetricEvent.path)
            )
        )
        fallback_reason_rows = list(
            session.exec(
                select(SystemMetricEvent.event_reason, func.count())
                .where(
                    SystemMetricEvent.created_at >= cutoff,
                    SystemMetricEvent.event_type == SystemMetricEventType.SEMANTIC_FALLBACK,
                )
                .group_by(SystemMetricEvent.event_reason)
            )
        )

        fallback_by_path: dict[str, int] = {}
        for path, count in fallback_path_rows:
            fallback_by_path[path or "__unknown__"] = int(count)

        fallback_by_reason: dict[str, int] = {}
        for reason, count in fallback_reason_rows:
            fallback_by_reason[reason or "unknown"] = int(count)

        fallback_by_language: dict[str, int] = {}
        for path, count in fallback_by_path.items():
            language = cls._language_bucket_for_path(None if path == "__unknown__" else path)
            fallback_by_language[language] = fallback_by_language.get(language, 0) + int(count)

        query_latency_ms = (time.perf_counter() - query_started) * 1000.0
        submit_accepted = metric_counts.get(SystemMetricEventType.SUBMIT_ACCEPTED.value, 0)
        status_promoted = metric_counts.get(SystemMetricEventType.STATUS_PROMOTED.value, 0)
        status_rejected = metric_counts.get(SystemMetricEventType.STATUS_REJECTED.value, 0)
        status_quarantined = metric_counts.get(SystemMetricEventType.STATUS_QUARANTINED.value, 0)
        status_superseded = metric_counts.get(SystemMetricEventType.STATUS_SUPERSEDED.value, 0)
        semantic_fallback = metric_counts.get(SystemMetricEventType.SEMANTIC_FALLBACK.value, 0)
        mu_events = status_promoted + status_rejected + status_quarantined + status_superseded

        lambda_tps = submit_accepted / float(window)
        mu_tps = mu_events / float(window)
        rho = (lambda_tps / mu_tps) if mu_tps > 0 else None

        ingested = status_counts.get(ProposalStatus.INGESTED.value, 0)
        pending = status_counts.get(ProposalStatus.PENDING.value, 0)
        evaluating = status_counts.get(ProposalStatus.EVALUATING.value, 0)
        promoted = status_counts.get(ProposalStatus.PROMOTED.value, 0)
        rejected = status_counts.get(ProposalStatus.REJECTED.value, 0)

        by_source: dict[str, dict[str, Any]] = {}
        for source, status, count in source_status_rows:
            source_key = cls._normalize_source(source)
            source_bucket = by_source.setdefault(
                source_key,
                {
                    "events": {
                        SystemMetricEventType.SUBMIT_ACCEPTED.value: 0,
                        SystemMetricEventType.STATUS_PROMOTED.value: 0,
                        SystemMetricEventType.STATUS_REJECTED.value: 0,
                        SystemMetricEventType.STATUS_QUARANTINED.value: 0,
                        SystemMetricEventType.STATUS_SUPERSEDED.value: 0,
                        SystemMetricEventType.SEMANTIC_FALLBACK.value: 0,
                        SystemMetricEventType.BATCH_GATE_FAIL.value: 0,
                        SystemMetricEventType.BATCH_SIZE_K.value: 0,
                    },
                    "status_counts": {
                        ProposalStatus.INGESTED.value: 0,
                        ProposalStatus.PENDING.value: 0,
                        ProposalStatus.EVALUATING.value: 0,
                        ProposalStatus.PROMOTED.value: 0,
                        ProposalStatus.REJECTED.value: 0,
                        ProposalStatus.QUARANTINED.value: 0,
                        ProposalStatus.SUPERSEDED.value: 0,
                    },
                },
            )
            status_key = status.value if isinstance(status, ProposalStatus) else str(status)
            source_bucket["status_counts"][status_key] = int(count)

        for source, event_type, count in source_metric_rows:
            source_key = cls._normalize_source(source)
            source_bucket = by_source.setdefault(
                source_key,
                {
                    "events": {
                        SystemMetricEventType.SUBMIT_ACCEPTED.value: 0,
                        SystemMetricEventType.STATUS_PROMOTED.value: 0,
                        SystemMetricEventType.STATUS_REJECTED.value: 0,
                        SystemMetricEventType.STATUS_QUARANTINED.value: 0,
                        SystemMetricEventType.STATUS_SUPERSEDED.value: 0,
                        SystemMetricEventType.SEMANTIC_FALLBACK.value: 0,
                        SystemMetricEventType.BATCH_GATE_FAIL.value: 0,
                        SystemMetricEventType.BATCH_SIZE_K.value: 0,
                    },
                    "status_counts": {
                        ProposalStatus.INGESTED.value: 0,
                        ProposalStatus.PENDING.value: 0,
                        ProposalStatus.EVALUATING.value: 0,
                        ProposalStatus.PROMOTED.value: 0,
                        ProposalStatus.REJECTED.value: 0,
                        ProposalStatus.QUARANTINED.value: 0,
                        ProposalStatus.SUPERSEDED.value: 0,
                    },
                },
            )
            event_key = event_type.value if isinstance(event_type, SystemMetricEventType) else str(event_type)
            source_bucket["events"][event_key] = int(count)

        return {
            "window_seconds": window,
            "query_latency_ms": query_latency_ms,
            "lambda_tps": lambda_tps,
            "mu_tps": mu_tps,
            "rho": rho,
            "deferred_count": deferred_count,
            "events": {
                "submit_accepted": submit_accepted,
                "status_promoted": status_promoted,
                "status_rejected": status_rejected,
                "status_quarantined": status_quarantined,
                "status_superseded": status_superseded,
                "semantic_fallback_hits": semantic_fallback,
            },
            "semantic_fallback": {
                "hits": semantic_fallback,
                "by_language": dict(sorted(fallback_by_language.items())),
                "by_path": dict(sorted(fallback_by_path.items())),
                "by_reason": dict(sorted(fallback_by_reason.items())),
            },
            "fallback_rate": (
                semantic_fallback / float(submit_accepted)
                if submit_accepted > 0
                else 0.0
            ),
            "queue_depth": {
                "ingested": ingested,
                "pending": pending,
                "evaluating": evaluating,
                "ingested_pending": ingested + pending,
                "active": ingested + pending + evaluating,
            },
            "status_counts": {
                "promoted": promoted,
                "rejected": rejected,
                "ingested": ingested,
                "pending": pending,
                "evaluating": evaluating,
            },
            "by_source": dict(sorted(by_source.items())),
        }

    @classmethod
    def get_active_path_counts(
        cls,
        session: Session,
        *,
        repo_id: str,
        paths: list[str],
    ) -> dict[str, int]:
        """Return active proposal counts for each requested path."""
        normalized_paths = sorted({path for path in paths if isinstance(path, str) and path})
        if not normalized_paths:
            return {}

        active_statuses = [
            ProposalStatus.INGESTED,
            ProposalStatus.PENDING,
            ProposalStatus.EVALUATING,
            ProposalStatus.REBASING,
        ]
        from sqlmodel import exists
        deferred_subq = select(ProposalContention.proposal_id).where(
            ProposalContention.proposal_id == Proposal.id,
            ProposalContention.defer_evaluation == True,
        )

        counts: dict[str, int] = {}
        for path in normalized_paths:
            escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f'%"{escaped}"%'
            value = session.exec(
                select(func.count())
                .select_from(Proposal)
                .where(
                    Proposal.repo_id == repo_id,
                    Proposal.status.in_(active_statuses),
                    Proposal.affected_paths.like(pattern, escape="\\"),
                    ~exists(deferred_subq),
                )
            ).one()
            if isinstance(value, tuple):
                counts[path] = int(value[0])
            else:
                counts[path] = int(value)
        return counts

    def _submit_proposal_on_worker(
        self,
        repo_id: str,
        proposal_payload: dict[str, Any],
    ) -> tuple[ProposalResponse, list[AetherEvent], dict[str, Any], dict[str, float]]:
        """Run submission on a worker thread with a fresh DB session."""
        if self._session_factory is None:
            raise RuntimeError("session_factory is required for worker submit mode")

        proposal_data = ProposalCreate.model_validate(proposal_payload)
        with self._session_factory() as worker_session:
            if isinstance(self.repo_service, dict):
                worker_repo_service: RepositoryService | dict[str, DocumentStore] = self.repo_service
            else:
                worker_repo_service = RepositoryService(worker_session, self.repo_service.storage_path)

            worker_service = ProposalService(
                worker_session,
                self.event_bus,
                worker_repo_service,
                defer_conflict_detection=self._defer_conflict_detection,
            )
            return worker_service._submit_proposal_sync(repo_id, proposal_data)

    def _build_idempotent_response(
        self,
        repo_id: str,
        existing: Proposal,
    ) -> tuple[ProposalResponse, list[AetherEvent], dict[str, Any], dict[str, float]]:
        """Build the standard idempotent submission response payload."""
        return (
            ProposalResponse(
                proposal_id=existing.id,
                status=existing.status,
                source=self._normalize_source(existing.source),
                conflict_set_id=existing.conflict_set_id,
                message="Proposal already exists (idempotent)",
            ),
            [],
            {
                "proposal_id": existing.id,
                "repo_id": repo_id,
                "agent_id": existing.agent_id,
                "source": self._normalize_source(existing.source),
                "paths": json.loads(existing.affected_paths),
                "confidence": existing.confidence_score,
                "idempotent": True,
            },
            {},
        )

    def get_last_submit_timings(self) -> dict[str, float]:
        """Return timing breakdown from the most recent submit on this service instance."""
        return dict(self._last_submit_timings)

    def _create_proposal_row(
        self,
        repo_id: str,
        proposal_data: ProposalCreate,
        *,
        initial_status: ProposalStatus,
    ) -> Proposal:
        """Create and flush a proposal row with the requested initial status."""
        repo = self.session.get(Repository, repo_id)
        if not repo:
            raise ValueError(f"Repository not found: {repo_id}")

        proposal = Proposal(
            repo_id=repo_id,
            agent_id=proposal_data.agent_id,
            task_id=proposal_data.task_id,
            source=self._normalize_source(proposal_data.source),
            source_run_id=proposal_data.source_run_id,
            source_task_id=proposal_data.source_task_id,
            source_agent_id=proposal_data.source_agent_id,
            source_attempt_id=proposal_data.source_attempt_id,
            intent_type=proposal_data.intent_type,
            base_head_id=proposal_data.base_head_id if proposal_data.base_head_id is not None else repo.head_id,
            base_head_sequence=proposal_data.base_head_sequence if proposal_data.base_head_sequence is not None else repo.head_sequence,
            change_type=proposal_data.change_type,
            change_content=proposal_data.change_content,
            affected_paths=json.dumps(proposal_data.affected_paths),
            affected_symbols=json.dumps(proposal_data.affected_symbols) if proposal_data.affected_symbols else None,
            risk_tier=proposal_data.risk_tier,
            confidence_score=proposal_data.confidence_score,
            model_id=proposal_data.model_id,
            calibration_profile=proposal_data.calibration_profile,
            rationale=proposal_data.rationale,
            dependencies=json.dumps(proposal_data.dependencies),
            requires_tests=proposal_data.requires_tests,
            requires_security_scan=proposal_data.requires_security_scan,
            idempotency_key=proposal_data.idempotency_key,
            status=initial_status,
        )
        self.session.add(proposal)
        self.session.flush()

        if proposal_data.contention_mode or proposal_data.defer_evaluation:
            contention = ProposalContention(
                proposal_id=proposal.id,
                contention_mode=True,
                contention_group_id=proposal_data.contention_group_id,
                contention_policy=proposal_data.contention_policy,
                target_paths=json.dumps(proposal_data.target_paths or proposal_data.affected_paths),
                defer_evaluation=bool(proposal_data.defer_evaluation),
            )
            self.session.add(contention)
            self.session.flush()

        # Record submission metrics without per-call commit amplification.
        self.trust_service.record_submission(proposal.agent_id, commit=False)
        return proposal

    @staticmethod
    def _safe_json_loads(raw: Optional[str], fallback: Any) -> Any:
        """Decode JSON payloads from persisted proposal fields."""
        if raw is None:
            return fallback
        try:
            return json.loads(raw)
        except Exception:
            return fallback

    def _proposal_to_create(self, proposal: Proposal) -> ProposalCreate:
        """Convert persisted proposal row into ProposalCreate shape."""
        cont_meta = self.get_contention_metadata(proposal.id)
        return ProposalCreate(
            agent_id=proposal.agent_id,
            task_id=proposal.task_id,
            source=self._normalize_source(proposal.source),
            source_run_id=proposal.source_run_id,
            source_task_id=proposal.source_task_id,
            source_agent_id=proposal.source_agent_id,
            source_attempt_id=proposal.source_attempt_id,
            intent_type=proposal.intent_type,
            change_type=proposal.change_type,
            change_content=proposal.change_content,
            affected_paths=self._safe_json_loads(proposal.affected_paths, []),
            affected_symbols=self._safe_json_loads(proposal.affected_symbols, None),
            risk_tier=proposal.risk_tier,
            confidence_score=proposal.confidence_score,
            model_id=proposal.model_id,
            calibration_profile=proposal.calibration_profile,
            rationale=proposal.rationale,
            dependencies=self._safe_json_loads(proposal.dependencies, []),
            requires_tests=proposal.requires_tests,
            requires_security_scan=proposal.requires_security_scan,
            idempotency_key=proposal.idempotency_key,
            defer_evaluation=cont_meta.defer_evaluation if cont_meta else None,
        )

    def _normalize_change_payload(self, proposal_data: ProposalCreate) -> ProposalCreate:
        """Validate/normalize change payloads before persistence."""
        normalized_affected = [
            validate_repo_relative_path(path)
            for path in (proposal_data.affected_paths or [])
        ]
        updates: dict[str, Any] = {
            "source": self._normalize_source(proposal_data.source),
            "affected_paths": normalized_affected,
        }
        if proposal_data.target_paths is not None:
            updates["target_paths"] = [
                validate_repo_relative_path(path)
                for path in proposal_data.target_paths
            ]
        proposal_data = proposal_data.model_copy(update=updates)

        if proposal_data.change_type not in self._SUPPORTED_CHANGE_TYPES:
            raise ValueError(f"Unsupported change_type: {proposal_data.change_type}")

        if proposal_data.change_type in {"aip.v0", "aip_mutation"}:
            mutation_paths, _ = extract_aip_paths_and_symbols(proposal_data.change_content)
            validated_mutations = {validate_repo_relative_path(p) for p in mutation_paths}
            merged_paths = sorted(set(normalized_affected) | validated_mutations)
            if not merged_paths:
                raise ValueError("AIP payload must include at least one mutated path")
            return proposal_data.model_copy(update={"affected_paths": merged_paths})

        if proposal_data.change_type == "unified_diff":
            parsed_diff = parse_unified_diff(proposal_data.change_content)
            diff_paths = {validate_repo_relative_path(p) for p in parsed_diff.keys()}
            merged_paths = sorted(set(normalized_affected) | diff_paths)
            return proposal_data.model_copy(update={"affected_paths": merged_paths})

        if proposal_data.change_type == "full_file":
            if not normalized_affected:
                raise ValueError("full_file proposal must specify at least one affected_path")
            return proposal_data

        return proposal_data

    @staticmethod
    def _normalize_paths(raw_paths: Any) -> set[str]:
        """Normalize serialized path lists into a set for conflict checks."""
        if not isinstance(raw_paths, list):
            return set()
        return {
            path
            for path in raw_paths
            if isinstance(path, str) and path
        }

    def _has_semantic_conflict(
        self,
        proposal_paths: set[str],
        proposal_symbols: dict[str, set[str]] | None,
        other_paths: set[str],
        other_symbols: dict[str, set[str]] | None,
    ) -> bool:
        """Evaluate semantic conflict between two proposals."""
        common_paths = proposal_paths & other_paths
        if not common_paths:
            return False

        for path in common_paths:
            if proposal_symbols is None or other_symbols is None:
                return True

            if path not in proposal_symbols or path not in other_symbols:
                return True

            if {"__file__", "__raw__"} & proposal_symbols[path] or {"__file__", "__raw__"} & other_symbols[path]:
                return True

            if proposal_symbols[path] & other_symbols[path]:
                return True

        return False

    def _decode_affected_symbols(self, raw_symbols: Optional[str]) -> dict[str, set[str]] | None:
        """Decode serialized semantic symbols into {path: {node_id}}."""
        if not raw_symbols:
            return None

        try:
            values = json.loads(raw_symbols)
        except Exception:
            return None

        if not isinstance(values, list):
            return None

        by_path: dict[str, set[str]] = {}
        for value in values:
            if not isinstance(value, str) or "::" not in value:
                continue
            path, node_id = value.split("::", 1)
            if not path or not node_id:
                continue
            by_path.setdefault(path, set()).add(node_id)

        return by_path

    def _compute_affected_symbols(
        self,
        repo_id: str,
        proposal: Proposal,
        proposal_data: ProposalCreate,
    ) -> Optional[list[str]]:
        """
        Compute semantic touch metadata from persisted/canonical context.

        This keeps conflict detection deterministic without mutating canonical state.
        """
        if proposal_data.affected_symbols:
            return list(proposal_data.affected_symbols)

        affected_paths = list(proposal_data.affected_paths)
        if not affected_paths:
            return None

        doc_store = self.get_document_store(repo_id)
        if not doc_store:
            return None

        symbols: set[str] = set()
        wildcard_paths: set[str] = set()
        wildcard_reasons: dict[str, set[str]] = {}

        def mark_wildcard(path: str, reason: str) -> None:
            wildcard_paths.add(path)
            wildcard_reasons.setdefault(path, set()).add(reason)

        if proposal.change_type == "unified_diff":
            file_hunks = parse_unified_diff(proposal.change_content)
            for path in affected_paths:
                hunks = file_hunks.get(path, [])
                if not hunks:
                    mark_wildcard(path, "missing_hunks")
                    continue

                if path.endswith(".py"):
                    file_doc = doc_store.get_document(path)
                    if not file_doc.structured:
                        mark_wildcard(path, "python_unstructured")
                        continue

                    # Helper to get symbol ID at line
                    def get_symbol_at(line: int) -> Optional[str]:
                        return file_doc.structured.get_node_at_line(line)

                else:
                    file_doc = doc_store.get_document(path)
                    spans = extract_symbol_spans(path, file_doc.get_content())
                    if not spans:
                        mark_wildcard(path, "non_python_symbol_parse_failed")
                        continue

                    def get_symbol_at(line: int) -> Optional[str]:
                        return find_symbol_at_line(spans, line)

                for hunk in hunks:
                    if hunk.old_start <= 0:
                        if not (hunk.old_start == 0 and hunk.old_count == 0):
                            mark_wildcard(path, "invalid_hunk_start")
                            break

                    # Range-aware symbol extraction:
                    # Identify the exact deleted/modified lines and insertion points in the old file.
                    current_old_line = hunk.old_start
                    affected_old_lines = []
                    for hline in hunk.lines:
                        if hline.startswith(" "):
                            current_old_line += 1
                        elif hline.startswith("-"):
                            affected_old_lines.append(current_old_line)
                            current_old_line += 1
                        elif hline.startswith("+"):
                            affected_old_lines.append(current_old_line - 1 if current_old_line > 0 else 0)

                    if not affected_old_lines:
                        affected_old_lines = list(range(hunk.old_start, hunk.old_start + max(1, hunk.old_count)))

                    # Map affected old lines to their symbols
                    hunk_symbols = set()
                    for line in affected_old_lines:
                        sym = get_symbol_at(line)
                        if sym:
                            hunk_symbols.add(sym)

                    if not hunk_symbols:
                        mark_wildcard(path, "symbol_not_found")
                        break
                    elif len(hunk_symbols) > 1:
                        mark_wildcard(path, "multi_node_span")
                        break
                    else:
                        symbols.add(f"{path}::{list(hunk_symbols)[0]}")
        elif proposal.change_type == "full_file":
            for path in affected_paths:
                mark_wildcard(path, "full_file_lock")
        elif proposal.change_type in {"aip.v0", "aip_mutation"}:
            try:
                mutation_paths, mutation_symbols = extract_aip_paths_and_symbols(
                    proposal.change_content
                )
            except ValueError:
                for path in affected_paths:
                    mark_wildcard(path, "aip_payload_invalid")
            else:
                all_paths = set(affected_paths) | mutation_paths
                symbols.update(mutation_symbols)
                for path in all_paths:
                    has_path_symbol = any(sym.startswith(f"{path}::") for sym in mutation_symbols)
                    if not has_path_symbol:
                        mark_wildcard(path, "aip_path_without_symbols")
        else:
            return None

        for path in wildcard_paths:
            symbols.add(f"{path}::__file__")
            symbols.add(f"{path}::__raw__")
            self._record_metric_event(
                event_type=SystemMetricEventType.SEMANTIC_FALLBACK,
                repo_id=repo_id,
                proposal_id=proposal.id,
                reason=",".join(sorted(wildcard_reasons.get(path, {"unknown"}))),
                path=path,
            )

        return sorted(symbols) if symbols else []

    async def submit_proposal(
        self,
        repo_id: str,
        proposal_data: ProposalCreate,
    ) -> ProposalResponse:
        """
        Submit a new proposal.

        Returns proposal ID and initial status.
        """
        if self._submit_in_worker:
            if self._submit_executor is None:
                self._ensure_submit_executor(self._worker_threads)
            if self._submit_executor is None:
                raise RuntimeError("submit executor is unavailable")
            loop = asyncio.get_running_loop()
            response, events, log_context, submit_timings = await loop.run_in_executor(
                self._submit_executor,
                self._submit_proposal_on_worker,
                repo_id,
                proposal_data.model_dump(),
            )
        else:
            response, events, log_context, submit_timings = self._submit_proposal_sync(
                repo_id,
                proposal_data,
            )

        for event in events:
            await self.event_bus.publish(event)

        self._last_submit_timings = submit_timings
        logger.info("proposal_submitted", **log_context)
        return response

    def _ingest_proposal_sync(
        self,
        repo_id: str,
        proposal_data: ProposalCreate,
    ) -> tuple[ProposalResponse, list[AetherEvent], dict[str, Any], dict[str, float]]:
        """Fast append-only ingest path used for async conflict materialization."""
        started = time.perf_counter()
        proposal_data = self._normalize_change_payload(proposal_data)
        try:
            if proposal_data.idempotency_key:
                existing = self._get_by_idempotency_key(proposal_data.idempotency_key)
                if existing:
                    return self._build_idempotent_response(repo_id, existing)

            proposal = self._create_proposal_row(
                repo_id,
                proposal_data,
                initial_status=ProposalStatus.INGESTED,
            )
            self._record_metric_event(
                event_type=SystemMetricEventType.SUBMIT_ACCEPTED,
                repo_id=repo_id,
                proposal_id=proposal.id,
            )
            db_started = time.perf_counter()
            self.session.add(proposal)
            self.session.commit()
            self.session.refresh(proposal)
            db_write_ms = (time.perf_counter() - db_started) * 1000.0

            enqueue_started = time.perf_counter()
            events = [
                create_proposal_event(
                    EventType.PROPOSAL_SUBMITTED,
                    repo_id,
                    proposal.id,
                    proposal.agent_id,
                    proposal_data.affected_paths,
                    confidence=proposal.confidence_score,
                    has_conflict=False,
                    queued_for_materialization=True,
                )
            ]
            enqueue_ms = (time.perf_counter() - enqueue_started) * 1000.0

            return (
                ProposalResponse(
                    proposal_id=proposal.id,
                    status=proposal.status,
                    source=proposal.source,
                    conflict_set_id=None,
                    message="Proposal ingested; async conflict detection queued",
                ),
                events,
                {
                    "proposal_id": proposal.id,
                    "repo_id": repo_id,
                    "agent_id": proposal.agent_id,
                    "source": proposal.source,
                    "paths": proposal_data.affected_paths,
                    "confidence": proposal.confidence_score,
                    "queued_for_materialization": True,
                },
                {
                    "service_total_ms": (time.perf_counter() - started) * 1000.0,
                    "db_write_ms": db_write_ms,
                    "conflict_or_enqueue_ms": enqueue_ms,
                },
            )
        except Exception:
            self.session.rollback()
            raise

    def _materialize_ingested_sync(
        self,
        repo_id: str,
        limit: int,
    ) -> tuple[int, list[AetherEvent]]:
        """
        Convert INGESTED proposals into PENDING by running semantic/conflict detection.

        This is intentionally run off the API intake path.
        """
        if limit <= 0:
            return 0, []

        statement = (
            select(Proposal)
            .where(
                Proposal.repo_id == repo_id,
                Proposal.status == ProposalStatus.INGESTED,
            )
            .order_by(Proposal.submitted_at.asc())
            .limit(limit)
        )
        queued = list(self.session.exec(statement))
        if not queued:
            return 0, []

        pending_statement = select(Proposal).where(
            Proposal.repo_id == repo_id,
            Proposal.status == ProposalStatus.PENDING,
        )
        pending = list(self.session.exec(pending_statement))

        open_conflict_sets = list(
            self.session.exec(
                select(ConflictSet)
                .where(
                    ConflictSet.repo_id == repo_id,
                    ConflictSet.resolved == False,
                )
                .order_by(ConflictSet.created_at.asc())
            )
        )
        conflict_set_by_id: dict[str, ConflictSet] = {
            conflict_set.id: conflict_set
            for conflict_set in open_conflict_sets
        }
        conflict_set_by_path: dict[str, ConflictSet] = {}
        for conflict_set in open_conflict_sets:
            if conflict_set.path not in conflict_set_by_path:
                conflict_set_by_path[conflict_set.path] = conflict_set

        proposal_meta: dict[str, tuple[Proposal, set[str], dict[str, set[str]] | None]] = {}
        proposals_by_path: dict[str, set[str]] = defaultdict(set)

        for proposal in pending:
            paths = self._normalize_paths(self._safe_json_loads(proposal.affected_paths, []))
            symbols = self._decode_affected_symbols(proposal.affected_symbols)
            proposal_meta[proposal.id] = (proposal, paths, symbols)
            for path in paths:
                proposals_by_path[path].add(proposal.id)

        events: list[AetherEvent] = []
        materialized = 0
        try:
            for proposal in queued:
                proposal_data = self._proposal_to_create(proposal)
                affected_symbols = self._compute_affected_symbols(repo_id, proposal, proposal_data)
                if affected_symbols is not None:
                    proposal.affected_symbols = json.dumps(affected_symbols)

                proposal_paths = self._normalize_paths(self._safe_json_loads(proposal.affected_paths, []))
                proposal_symbols = self._decode_affected_symbols(proposal.affected_symbols)
                candidate_ids: set[str] = set()
                for path in proposal_paths:
                    candidate_ids.update(proposals_by_path.get(path, set()))

                conflicting_proposals: list[Proposal] = []
                for candidate_id in candidate_ids:
                    candidate_meta = proposal_meta.get(candidate_id)
                    if candidate_meta is None:
                        continue
                    other_proposal, other_paths, other_symbols = candidate_meta
                    if self._has_semantic_conflict(
                        proposal_paths,
                        proposal_symbols,
                        other_paths,
                        other_symbols,
                    ):
                        conflicting_proposals.append(other_proposal)

                if conflicting_proposals and not proposal_data.defer_evaluation:
                    existing_set: ConflictSet | None = None
                    for other in conflicting_proposals:
                        if not other.conflict_set_id:
                            continue
                        candidate_set = conflict_set_by_id.get(other.conflict_set_id)
                        if candidate_set is not None and not candidate_set.resolved:
                            existing_set = candidate_set
                            break

                    if existing_set is None:
                        for path in proposal_paths:
                            candidate_set = conflict_set_by_path.get(path)
                            if candidate_set is not None and not candidate_set.resolved:
                                existing_set = candidate_set
                                break

                    if existing_set:
                        proposal_ids = self._safe_json_loads(existing_set.proposal_ids, [])
                        if not isinstance(proposal_ids, list):
                            proposal_ids = []
                        if proposal.id not in proposal_ids:
                            proposal_ids.append(proposal.id)
                            existing_set.proposal_ids = json.dumps(proposal_ids)
                            self.session.add(existing_set)

                        proposal.conflict_set_id = existing_set.id
                        logger.info(
                            "proposal_added_to_conflict_set",
                            proposal_id=proposal.id,
                            conflict_set_id=existing_set.id,
                            path=existing_set.path,
                        )
                    else:
                        ordered_conflicts = sorted(
                            conflicting_proposals,
                            key=lambda item: item.submitted_at,
                        )
                        all_proposal_ids = [proposal.id] + [other.id for other in ordered_conflicts]
                        conflict_path = next(iter(proposal_paths), "")
                        conflict_set = ConflictSet(
                            repo_id=proposal.repo_id,
                            path=conflict_path,
                            region_type="file",
                            proposal_ids=json.dumps(all_proposal_ids),
                        )
                        self.session.add(conflict_set)
                        self.session.flush()

                        for other in ordered_conflicts:
                            other.conflict_set_id = conflict_set.id
                            self.session.add(other)

                        proposal.conflict_set_id = conflict_set.id
                        conflict_set_by_id[conflict_set.id] = conflict_set
                        if conflict_set.path and conflict_set.path not in conflict_set_by_path:
                            conflict_set_by_path[conflict_set.path] = conflict_set
                        conflict_event = create_proposal_event(
                            EventType.CONFLICT_DETECTED,
                            proposal.repo_id,
                            proposal.id,
                            proposal.agent_id,
                            list(proposal_paths),
                            conflict_set_id=conflict_set.id,
                            conflicting_proposals=[other.id for other in ordered_conflicts],
                        )
                        events.append(conflict_event)
                        logger.info(
                            "conflict_set_created",
                            conflict_set_id=conflict_set.id,
                            proposal_ids=all_proposal_ids,
                            path=conflict_set.path,
                        )

                proposal.status = ProposalStatus.PENDING
                self.session.add(proposal)
                materialized += 1

                proposal_meta[proposal.id] = (proposal, proposal_paths, proposal_symbols)
                for path in proposal_paths:
                    proposals_by_path[path].add(proposal.id)

            self.session.commit()
            return materialized, events
        except Exception:
            self.session.rollback()
            raise

    async def materialize_ingested_queue(self, repo_id: str, limit: int = 200) -> int:
        """Asynchronously materialize queued INGESTED proposals for promotion processing."""
        materialized, events = self._materialize_ingested_sync(repo_id, limit=limit)
        for event in events:
            await self.event_bus.publish(event)
        if materialized:
            logger.info(
                "proposal_ingest_materialized",
                repo_id=repo_id,
                count=materialized,
                conflicts=len(events),
            )
        return materialized

    def _submit_proposal_sync(
        self,
        repo_id: str,
        proposal_data: ProposalCreate,
    ) -> tuple[ProposalResponse, list[AetherEvent], dict[str, Any], dict[str, float]]:
        """Blocking proposal submission core that performs all DB work."""
        started = time.perf_counter()
        proposal_data = self._normalize_change_payload(proposal_data)
        try:
            if self._defer_conflict_detection:
                return self._ingest_proposal_sync(repo_id, proposal_data)

            # Check for idempotency
            if proposal_data.idempotency_key:
                existing = self._get_by_idempotency_key(proposal_data.idempotency_key)
                if existing:
                    return self._build_idempotent_response(repo_id, existing)

            proposal = self._create_proposal_row(
                repo_id,
                proposal_data,
                initial_status=ProposalStatus.PENDING,
            )
            self._record_metric_event(
                event_type=SystemMetricEventType.SUBMIT_ACCEPTED,
                repo_id=repo_id,
                proposal_id=proposal.id,
            )

            conflict_started = time.perf_counter()
            affected_symbols = self._compute_affected_symbols(repo_id, proposal, proposal_data)
            if affected_symbols is not None:
                proposal.affected_symbols = json.dumps(affected_symbols)

            conflict_set_id = None
            conflict_event = None
            if not proposal_data.defer_evaluation:
                conflict_set_id, conflict_event = self._detect_conflicts(proposal)
                if conflict_set_id:
                    proposal.conflict_set_id = conflict_set_id
            conflict_or_enqueue_ms = (time.perf_counter() - conflict_started) * 1000.0

            db_started = time.perf_counter()
            self.session.add(proposal)
            self.session.commit()
            self.session.refresh(proposal)
            db_write_ms = (time.perf_counter() - db_started) * 1000.0

            events: list[AetherEvent] = []
            if conflict_event:
                events.append(conflict_event)
            events.append(
                create_proposal_event(
                    EventType.PROPOSAL_SUBMITTED,
                    repo_id,
                    proposal.id,
                    proposal.agent_id,
                    proposal_data.affected_paths,
                    confidence=proposal.confidence_score,
                    has_conflict=conflict_set_id is not None,
                )
            )

            return (
                ProposalResponse(
                    proposal_id=proposal.id,
                    status=proposal.status,
                    source=proposal.source,
                    conflict_set_id=conflict_set_id,
                    message="Proposal submitted successfully",
                ),
                events,
                {
                    "proposal_id": proposal.id,
                    "repo_id": repo_id,
                    "agent_id": proposal.agent_id,
                    "source": proposal.source,
                    "paths": proposal_data.affected_paths,
                    "confidence": proposal.confidence_score,
                },
                {
                    "service_total_ms": (time.perf_counter() - started) * 1000.0,
                    "db_write_ms": db_write_ms,
                    "conflict_or_enqueue_ms": conflict_or_enqueue_ms,
                },
            )
        except Exception:
            self.session.rollback()
            raise

    def _detect_conflicts(
        self,
        proposal: Proposal,
    ) -> tuple[Optional[str], Optional[AetherEvent]]:
        """
        Detect if this proposal conflicts with existing pending proposals.

        Creates or updates conflict sets as needed.
        Returns conflict_set_id if conflicts detected.
        """
        affected_paths = json.loads(proposal.affected_paths)
        if not affected_paths:
            return None, None
        proposal_paths = self._normalize_paths(affected_paths)

        # Find pending proposals that affect the same files and are not deferred
        from sqlmodel import exists
        deferred_subq = select(ProposalContention.proposal_id).where(
            ProposalContention.proposal_id == Proposal.id,
            ProposalContention.defer_evaluation == True,
        )
        statement = select(Proposal).where(
            Proposal.repo_id == proposal.repo_id,
            Proposal.status == ProposalStatus.PENDING,
            Proposal.id != proposal.id,
            ~exists(deferred_subq),
        )
        pending_proposals = list(self.session.exec(statement))

        conflicting_proposals = []
        proposal_symbols = self._decode_affected_symbols(proposal.affected_symbols)
        decoded_cache: dict[str, dict[str, set[str]] | None] = {}
        path_cache: dict[str, set[str]] = {}

        for other in pending_proposals:
            other_paths = path_cache.get(other.id)
            if other_paths is None:
                other_paths = self._normalize_paths(self._safe_json_loads(other.affected_paths, []))
                path_cache[other.id] = other_paths
            
            if not (proposal_paths & other_paths):
                continue
                
            # Semantic symbol check for all file types (with file-level fallback).
            other_symbols = decoded_cache.get(other.id)
            if other.id not in decoded_cache:
                other_symbols = self._decode_affected_symbols(other.affected_symbols)
                decoded_cache[other.id] = other_symbols

            if self._has_semantic_conflict(
                proposal_paths,
                proposal_symbols,
                other_paths,
                other_symbols,
            ):
                conflicting_proposals.append(other)

        if not conflicting_proposals:
            return None, None

        # Check if there's an existing conflict set for these files
        for path in affected_paths:
            statement = select(ConflictSet).where(
                ConflictSet.repo_id == proposal.repo_id,
                ConflictSet.path == path,
                ConflictSet.resolved == False,
            )
            existing_set = self.session.exec(statement).first()

            if existing_set:
                # Add to existing conflict set
                proposal_ids = json.loads(existing_set.proposal_ids)
                if proposal.id not in proposal_ids:
                    proposal_ids.append(proposal.id)
                    existing_set.proposal_ids = json.dumps(proposal_ids)
                    self.session.add(existing_set)

                logger.info(
                    "proposal_added_to_conflict_set",
                    proposal_id=proposal.id,
                    conflict_set_id=existing_set.id,
                    path=path,
                )
                return existing_set.id, None

        # Create new conflict set
        all_proposal_ids = [proposal.id] + [p.id for p in conflicting_proposals]
        conflict_set = ConflictSet(
            repo_id=proposal.repo_id,
            path=affected_paths[0],  # Use first path as primary
            region_type="file",
            proposal_ids=json.dumps(all_proposal_ids),
        )
        self.session.add(conflict_set)
        self.session.flush()

        # Update other proposals to reference this conflict set
        for other in conflicting_proposals:
            other.conflict_set_id = conflict_set.id
            self.session.add(other)

        conflict_event = create_proposal_event(
            EventType.CONFLICT_DETECTED,
            proposal.repo_id,
            proposal.id,
            proposal.agent_id,
            affected_paths,
            conflict_set_id=conflict_set.id,
            conflicting_proposals=[p.id for p in conflicting_proposals],
        )

        logger.info(
            "conflict_set_created",
            conflict_set_id=conflict_set.id,
            proposal_ids=all_proposal_ids,
            path=affected_paths[0],
        )

        return conflict_set.id, conflict_event

    def _get_by_idempotency_key(self, key: str) -> Optional[Proposal]:
        """Get proposal by idempotency key."""
        statement = select(Proposal).where(Proposal.idempotency_key == key)
        return self.session.exec(statement).first()

    def get_contention_metadata(self, proposal_id: str) -> Optional[ProposalContention]:
        """Get contention metadata for a proposal."""
        return self.session.get(ProposalContention, proposal_id)

    def get_proposal(self, proposal_id: str) -> Optional[Proposal]:
        """Get a proposal by ID."""
        return self.session.get(Proposal, proposal_id)

    def list_proposals(
        self,
        repo_id: str,
        status: Optional[Union[ProposalStatus, list[ProposalStatus]]] = None,
        agent_id: Optional[str] = None,
        limit: Optional[int] = 100,
        newest_first: bool = True,
        for_update: bool = False,
        exclude_deferred: bool = False,
    ) -> list[Proposal]:
        """List proposals with optional filters."""
        statement = select(Proposal).where(Proposal.repo_id == repo_id)

        if status:
            if isinstance(status, (list, tuple, set)):
                status_vals = [s.value if hasattr(s, "value") else s for s in status]
                statement = statement.where(Proposal.status.in_(status_vals))
            else:
                status_val = status.value if hasattr(status, "value") else status
                statement = statement.where(Proposal.status == status_val)
        if agent_id:
            statement = statement.where(Proposal.agent_id == agent_id)

        if exclude_deferred:
            from sqlmodel import exists
            deferred_subq = select(ProposalContention.proposal_id).where(
                ProposalContention.proposal_id == Proposal.id,
                ProposalContention.defer_evaluation == True,
            )
            statement = statement.where(~exists(deferred_subq))

        if newest_first:
            statement = statement.order_by(Proposal.submitted_at.desc())
        else:
            statement = statement.order_by(Proposal.submitted_at.asc())
        if limit is not None:
            statement = statement.limit(limit)

        if for_update:
            dialect_name = self.session.bind.dialect.name if self.session.bind else None
            if dialect_name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)

        return list(self.session.exec(statement))

    def get_proposal_provenance(self, proposal_id: str) -> Optional[ProposalProvenance]:
        """
        Retrieve the structured provenance details from SystemMetricEvent for terminal statuses.
        Uses a join on SystemMetricEvent to fetch the details without schema migrations.
        """
        from packages.aether.models.entities import Proposal, SystemMetricEvent, SystemMetricEventType, ProposalContention
        import json
        
        # Query SystemMetricEvent for the last STRUCTURAL_MERGE or SYNTHESIS_MERGE event associated with the proposal
        merge_stmt = (
            select(SystemMetricEvent)
            .join(Proposal, Proposal.id == SystemMetricEvent.proposal_id)
            .where(
                Proposal.id == proposal_id,
                SystemMetricEvent.event_type.in_([
                    SystemMetricEventType.STRUCTURAL_MERGE,
                    SystemMetricEventType.SYNTHESIS_MERGE
                ])
            )
            .order_by(SystemMetricEvent.created_at.desc(), SystemMetricEvent.id.desc())
            .limit(1)
        )
        merge_event = self.session.exec(merge_stmt).first()
        if merge_event:
            cont = self.session.exec(
                select(ProposalContention).where(ProposalContention.proposal_id == proposal_id)
            ).first()
            
            contention_group_id = cont.contention_group_id if cont else None
            contention_policy = cont.contention_policy if cont else None
            target_paths = None
            if cont and cont.target_paths:
                try:
                    target_paths = json.loads(cont.target_paths)
                except Exception:
                    pass
            return ProposalProvenance(
                proposal_id=proposal_id,
                event_type=merge_event.event_type.value,
                event_reason=merge_event.event_reason,
                created_at=merge_event.created_at,
                contention_group_id=contention_group_id,
                contention_policy=contention_policy,
                target_paths=target_paths,
                synthesis_engine=merge_event.synthesis_engine
            )


        # First check if there is a STATUS_PROMOTED event with contention winner details (JSON format)
        promoted_stmt = (
            select(SystemMetricEvent)
            .join(Proposal, Proposal.id == SystemMetricEvent.proposal_id)
            .where(
                Proposal.id == proposal_id,
                SystemMetricEvent.event_type == SystemMetricEventType.STATUS_PROMOTED
            )
            .order_by(SystemMetricEvent.created_at.desc())
            .limit(1)
        )
        prom_event = self.session.exec(promoted_stmt).first()
        if prom_event and prom_event.event_reason and prom_event.event_reason.startswith("{"):
            try:
                data = json.loads(prom_event.event_reason)
                if isinstance(data, dict):
                    return ProposalProvenance(
                        proposal_id=proposal_id,
                        event_type="promotion",
                        event_reason=data.get("event_reason", "contention_winner_promoted"),
                        created_at=prom_event.created_at,
                        contention_group_id=data.get("contention_group_id"),
                        contention_policy=data.get("contention_policy"),
                        winner_proposal_id=None,
                        previous_winner_proposal_id=data.get("previous_winner_proposal_id"),
                        target_paths=data.get("target_paths")
                    )
            except Exception:
                pass
                
        # Fall back to checking terminal failure events (STATUS_REJECTED, STATUS_QUARANTINED, STATUS_SUPERSEDED)
        statement = (
            select(SystemMetricEvent)
            .join(Proposal, Proposal.id == SystemMetricEvent.proposal_id)
            .where(
                Proposal.id == proposal_id,
                SystemMetricEvent.event_type.in_([
                    SystemMetricEventType.STATUS_REJECTED,
                    SystemMetricEventType.STATUS_QUARANTINED,
                    SystemMetricEventType.STATUS_SUPERSEDED,
                    SystemMetricEventType.STATUS_REBASING
                ])
            )
            .order_by(SystemMetricEvent.created_at.desc())
            .limit(1)
        )
        event = self.session.exec(statement).first()
        if not event:
            return None

        event_reason = event.event_reason
        contention_group_id = None
        contention_policy = None
        winner_proposal_id = None
        previous_winner_proposal_id = None
        target_paths = None

        if event.event_reason and event.event_reason.startswith("{"):
            try:
                data = json.loads(event.event_reason)
                if isinstance(data, dict):
                    event_reason = data.get("event_reason", event.event_reason)
                    contention_group_id = data.get("contention_group_id")
                    contention_policy = data.get("contention_policy")
                    winner_proposal_id = data.get("winner_proposal_id")
                    previous_winner_proposal_id = data.get("previous_winner_proposal_id")
                    target_paths = data.get("target_paths")
            except Exception:
                pass

        return ProposalProvenance(
            proposal_id=proposal_id,
            event_type=event.event_type.value,
            event_reason=event_reason,
            created_at=event.created_at,
            contention_group_id=contention_group_id,
            contention_policy=contention_policy,
            winner_proposal_id=winner_proposal_id,
            previous_winner_proposal_id=previous_winner_proposal_id,
            target_paths=target_paths
        )

    async def update_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        reason: Optional[str] = None,
        commit: bool = True,
    ) -> bool:
        """Update proposal status."""
        updated = await self.update_status_many([(proposal_id, status, reason)], commit=commit)
        return updated > 0

    def _apply_status_updates(
        self,
        updates: list[tuple[str, ProposalStatus, Optional[str]]],
        commit: bool = True,
    ) -> tuple[list[tuple[str, str, str, Optional[str]]], list[AetherEvent]]:
        """
        Apply many status updates in one transaction.

        Returns:
            - log tuples: (proposal_id, old_status, new_status, reason)
            - events to publish after commit
        """
        if not updates:
            return [], []

        proposal_ids = [proposal_id for proposal_id, _, _ in updates]
        proposals = self.get_proposals_by_ids(proposal_ids)
        by_id = {proposal.id: proposal for proposal in proposals}
        now = datetime.now(UTC)
        logs: list[tuple[str, str, str, Optional[str]]] = []
        events: list[AetherEvent] = []

        event_type_map = {
            ProposalStatus.EVALUATING: EventType.PROPOSAL_EVALUATING,
            ProposalStatus.PROMOTED: EventType.PROPOSAL_PROMOTED,
            ProposalStatus.REJECTED: EventType.PROPOSAL_REJECTED,
            ProposalStatus.QUARANTINED: EventType.PROPOSAL_QUARANTINED,
        }

        for proposal_id, status, reason in updates:
            proposal = by_id.get(proposal_id)
            if proposal is None:
                continue
            old_status = proposal.status
            if old_status == status:
                continue

            proposal.status = status
            if status == ProposalStatus.EVALUATING:
                proposal.evaluated_at = now
            elif status == ProposalStatus.PROMOTED:
                proposal.promoted_at = now
            self.session.add(proposal)

            if status == ProposalStatus.PROMOTED:
                self._record_metric_event(
                    event_type=SystemMetricEventType.STATUS_PROMOTED,
                    repo_id=proposal.repo_id,
                    proposal_id=proposal.id,
                    reason=reason,
                )
            elif status == ProposalStatus.REJECTED:
                self._record_metric_event(
                    event_type=SystemMetricEventType.STATUS_REJECTED,
                    repo_id=proposal.repo_id,
                    proposal_id=proposal.id,
                    reason=reason,
                )
            elif status == ProposalStatus.QUARANTINED:
                self._record_metric_event(
                    event_type=SystemMetricEventType.STATUS_QUARANTINED,
                    repo_id=proposal.repo_id,
                    proposal_id=proposal.id,
                    reason=reason,
                )
            elif status == ProposalStatus.SUPERSEDED:
                self._record_metric_event(
                    event_type=SystemMetricEventType.STATUS_SUPERSEDED,
                    repo_id=proposal.repo_id,
                    proposal_id=proposal.id,
                    reason=reason,
                )
            elif status == ProposalStatus.REBASING:
                self._record_metric_event(
                    event_type=SystemMetricEventType.STATUS_REBASING,
                    repo_id=proposal.repo_id,
                    proposal_id=proposal.id,
                    reason=reason,
                )

            logs.append((proposal_id, old_status.value, status.value, reason))
            event_type = event_type_map.get(status)
            if event_type:
                events.append(
                    create_proposal_event(
                        event_type,
                        proposal.repo_id,
                        proposal.id,
                        proposal.agent_id,
                        json.loads(proposal.affected_paths),
                        reason=reason,
                        old_status=old_status.value,
                    )
                )

        if logs:
            if commit:
                self.session.commit()
            else:
                self.session.flush()

        return logs, events

    async def update_status_many(
        self,
        updates: list[tuple[str, ProposalStatus, Optional[str]]],
        commit: bool = True,
        publish_events: bool = True,
        deferred_events: Optional[list[AetherEvent]] = None,
        deferred_logs: Optional[list[tuple[str, str, str, Optional[str]]]] = None,
    ) -> int:
        """Update multiple proposal statuses with one commit and publish events."""
        logs, events = self._apply_status_updates(updates, commit=commit)
        if deferred_events is not None:
            deferred_events.extend(events)
        if deferred_logs is not None:
            deferred_logs.extend(logs)

        if publish_events:
            for event in events:
                await self.event_bus.publish(event)

            for proposal_id, old_status, new_status, reason in logs:
                logger.info(
                    "proposal_status_updated",
                    proposal_id=proposal_id,
                    old_status=old_status,
                    new_status=new_status,
                    reason=reason,
                )
        return len(logs)

    def get_conflict_set(self, conflict_set_id: str) -> Optional[ConflictSet]:
        """Get a conflict set by ID."""
        return self.session.get(ConflictSet, conflict_set_id)

    def list_conflict_sets(
        self,
        repo_id: str,
        resolved: Optional[bool] = None,
        limit: Optional[int] = None,
        newest_first: bool = True,
    ) -> list[ConflictSet]:
        """List conflict sets."""
        statement = select(ConflictSet).where(ConflictSet.repo_id == repo_id)

        if resolved is not None:
            statement = statement.where(ConflictSet.resolved == resolved)

        if newest_first:
            statement = statement.order_by(ConflictSet.created_at.desc())
        else:
            statement = statement.order_by(ConflictSet.created_at.asc())
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.exec(statement))

    async def resolve_conflict_set(
        self,
        conflict_set_id: str,
        chosen_proposal_id: str,
        commit: bool = True,
        semantic_merge_enabled: bool = False,
        publish_events: bool = True,
        deferred_events: Optional[list[AetherEvent]] = None,
        deferred_logs: Optional[list[tuple[str, str, str, Optional[str]]]] = None,
    ) -> bool:
        """
        Resolve a conflict set by choosing a winning proposal.
 
        Other proposals in the set are marked as SUPERSEDED or REBASING based on policy.
        """
        conflict_set = self.get_conflict_set(conflict_set_id)
        if not conflict_set:
            return False
 
        proposal_ids = json.loads(conflict_set.proposal_ids)
        if chosen_proposal_id not in proposal_ids:
            raise ValueError(f"Proposal {chosen_proposal_id} not in conflict set")
 
        # Fetch proposals to check their current status, preserving pre-existing superseded reasons
        statement = select(Proposal).where(Proposal.id.in_(proposal_ids))
        proposals = self.session.exec(statement).all()

        # Check contention policy of the chosen proposal
        cont_meta = self.session.get(ProposalContention, chosen_proposal_id)
        is_semantic_merge = (
            semantic_merge_enabled
            and cont_meta is not None
            and cont_meta.contention_policy == "semantic_merge"
        )

        updates = []
        if is_semantic_merge:
            repo = self.session.get(Repository, conflict_set.repo_id)
            max_rebase = repo.max_rebase_attempts if repo and repo.max_rebase_attempts is not None else 2
            
            for p in proposals:
                if p.id != chosen_proposal_id and p.status in {ProposalStatus.PENDING, ProposalStatus.EVALUATING, ProposalStatus.REBASING}:
                    p.rebase_attempts = (p.rebase_attempts or 0) + 1
                    self.session.add(p)
                    
                    if p.rebase_attempts <= max_rebase:
                        reason_json = json.dumps({
                            "event_reason": "STALE_BASE",
                            "contention_group_id": f"conflict_set:{conflict_set_id}",
                            "winner_proposal_id": chosen_proposal_id
                        })
                        updates.append((p.id, ProposalStatus.REBASING, reason_json))
                    else:
                        reason_json = json.dumps({
                            "event_reason": "rebase_budget_exhaustion",
                            "contention_group_id": f"conflict_set:{conflict_set_id}",
                            "winner_proposal_id": chosen_proposal_id
                        })
                        updates.append((p.id, ProposalStatus.SUPERSEDED, reason_json))
        else:
            updates = [
                (p.id, ProposalStatus.SUPERSEDED, f"Superseded by {chosen_proposal_id}")
                for p in proposals
                if p.id != chosen_proposal_id and p.status in {ProposalStatus.PENDING, ProposalStatus.EVALUATING}
            ]

        await self.update_status_many(
            updates,
            commit=commit,
            publish_events=publish_events,
            deferred_events=deferred_events,
            deferred_logs=deferred_logs,
        )

        # Update conflict set
        conflict_set.active_choice_id = chosen_proposal_id
        conflict_set.resolved = True
        conflict_set.resolved_at = datetime.now(UTC)
        self.session.add(conflict_set)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

        conflict_event = create_proposal_event(
            EventType.CONFLICT_RESOLVED,
            conflict_set.repo_id,
            chosen_proposal_id,
            "",  # agent_id not relevant here
            [conflict_set.path],
            conflict_set_id=conflict_set_id,
            superseded_proposals=[pid for pid in proposal_ids if pid != chosen_proposal_id],
        )
        if deferred_events is not None:
            deferred_events.append(conflict_event)

        if publish_events:
            await self.event_bus.publish(conflict_event)
            logger.info(
                "conflict_set_resolved",
                conflict_set_id=conflict_set_id,
                chosen_proposal_id=chosen_proposal_id,
            )

        return True

    def get_pending_queue(self, repo_id: str, limit: Optional[int] = None, for_update: bool = False, exclude_deferred: bool = True) -> list[Proposal]:
        """Get pending proposals ordered oldest-first for fair promotion draining."""
        return self.list_proposals(
            repo_id,
            status=[ProposalStatus.PENDING, ProposalStatus.REBASING],
            limit=limit,
            newest_first=False,
            for_update=for_update,
            exclude_deferred=exclude_deferred,
        )

    def get_proposals_by_ids(
        self,
        proposal_ids: list[str],
        repo_id: Optional[str] = None,
    ) -> list[Proposal]:
        """Get multiple proposals by their IDs."""
        if not proposal_ids:
            return []

        statement = select(Proposal).where(Proposal.id.in_(proposal_ids))
        if repo_id:
            statement = statement.where(Proposal.repo_id == repo_id)
        return list(self.session.exec(statement))
