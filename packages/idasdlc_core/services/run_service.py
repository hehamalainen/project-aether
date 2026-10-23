"""Services for orchestrating runs and traces."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc
from sqlmodel import select

from ..db import session_scope
from ..logging import get_logger
from ..models.base import RunStatus
from ..models.entities import Run, Trace, ValidationResult

log = get_logger(__name__)


class RunService:
    """Persistence helpers for agent runs."""

    def create_run(
        self,
        *,
        task_id: uuid.UUID,
        agent_id: str,
        model_id: str,
        generation_label: str,
        parameters: dict[str, Any] | None = None,
        validator_policy: dict[str, Any] | None = None,
    ) -> Run:
        with session_scope() as session:
            run = Run(
                task_id=task_id,
                agent_id=agent_id,
                model_id=model_id,
                generation_label=generation_label,
                parameters=parameters or {},
                validator_policy_snapshot=validator_policy or {},
                status=RunStatus.PENDING,
            )
            session.add(run)
            session.flush()
            log.info("run_created", run_id=str(run.id), task_id=str(task_id))
            return run

    def update_status(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        metrics: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> Run:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if not run:  # pragma: no cover - defensive guard
                raise ValueError(f"Run not found: {run_id}")
            run.status = status
            if metrics:
                run.metrics = {**run.metrics, **metrics}
            now = datetime.now(UTC)
            if status == RunStatus.RUNNING and not run.started_at:
                run.started_at = now
            if finished:
                run.finished_at = now
            session.add(run)
            log.info("run_status_updated", run_id=str(run.id), status=status)
            return run

    def record_trace(
        self,
        run_id: uuid.UUID,
        *,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        diffs: list[str],
        artifacts_path: str | None,
        redaction_report: dict[str, Any] | None = None,
    ) -> Trace:
        with session_scope() as session:
            trace = Trace(
                run_id=run_id,
                messages=messages,
                tool_calls=tool_calls,
                diffs=diffs,
                artifacts_path=artifacts_path,
                redaction_report=redaction_report or {},
            )
            session.add(trace)
            log.info("trace_recorded", run_id=str(run_id))
            return trace

    def record_validation(
        self,
        run_id: uuid.UUID,
        *,
        validator_name: str,
        passed: bool,
        score: float | None = None,
        logs_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ValidationResult:
        with session_scope() as session:
            result = ValidationResult(
                run_id=run_id,
                validator_name=validator_name,
                passed=passed,
                score=score,
                logs=logs_text,
                metadata_blob=metadata or {},
            )
            session.add(result)
            log.info(
                "validation_recorded",
                run_id=str(run_id),
                validator=validator_name,
                passed=passed,
            )
            return result

    def recent_runs(self, limit: int = 20) -> list[Run]:
        with session_scope() as session:
            created_column = cast(Any, Run.created_at)
            stmt = select(Run).order_by(desc(created_column)).limit(limit)
            return list(session.exec(stmt))

    def get_run(self, run_id: uuid.UUID) -> Run | None:
        with session_scope() as session:
            return session.get(Run, run_id)

    def get_run_details(
        self, run_id: uuid.UUID
    ) -> tuple[Run, Trace | None, list[ValidationResult]] | None:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if not run:
                return None
            trace = session.exec(select(Trace).where(Trace.run_id == run_id)).first()
            validations = list(
                session.exec(select(ValidationResult).where(ValidationResult.run_id == run_id))
            )
            return run, trace, validations
