"""Agent orchestration loop."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..db import session_scope
from ..logging import get_logger
from ..models.base import RunStatus
from ..models.entities import Task
from ..models.validator_policy import ValidatorPolicy
from .run_service import RunService
from .validator_service import ValidatorService

log = get_logger(__name__)


@dataclass(slots=True)
class ExecutionResult:
    messages: list[dict]
    tool_calls: list[dict]
    diffs: list[str]
    artifacts_path: str | None
    metrics: dict
    validator_names: list[str] | None = None


class AgentRunner(Protocol):
    def execute(self, task: Task, repo_path: Path) -> ExecutionResult:
        ...


class OrchestratorService:
    def __init__(self, *, run_service: RunService | None = None, validator_service: ValidatorService | None = None):
        self.run_service = run_service or RunService()
        self.validator_service = validator_service or ValidatorService(self.run_service)

    def run_task(
        self,
        *,
        task_id: uuid.UUID,
        agent_id: str,
        model_id: str,
        generation_label: str,
        runner: AgentRunner,
        repo_path: Path,
    ) -> uuid.UUID:
        task = self._fetch_task(task_id)
        policy = self._resolve_policy(task)
        run = self.run_service.create_run(
            task_id=task.id,
            agent_id=agent_id,
            model_id=model_id,
            generation_label=generation_label,
            validator_policy=policy.to_blob() if policy else {},
        )
        self.run_service.update_status(run.id, status=RunStatus.RUNNING)
        try:
            result = runner.execute(task, repo_path)
        except Exception as exc:  # pragma: no cover - bubble up after bookkeeping
            self.run_service.update_status(
                run.id,
                status=RunStatus.FAILED,
                metrics={"error": str(exc)},
                finished=True,
            )
            log.exception("runner_failed", task_id=str(task.id), run_id=str(run.id))
            raise
        self.run_service.record_trace(
            run.id,
            messages=result.messages,
            tool_calls=result.tool_calls,
            diffs=result.diffs,
            artifacts_path=result.artifacts_path,
            redaction_report={},
        )
        self.run_service.update_status(run.id, status=RunStatus.RUNNING, metrics=result.metrics)
        policy = self._augment_policy(policy, result.validator_names)
        validator_names = policy.validator_names() if policy else (result.validator_names or task.validators_required)
        if not validator_names:
            log.warning("no_validators_specified", task_id=str(task.id), run_id=str(run.id))
        self.validator_service.run_pipeline(
            run_id=run.id,
            repo_path=repo_path,
            validator_names=validator_names,
            policy=policy,
        )
        log.info("task_run_complete", task_id=str(task.id), run_id=str(run.id))
        return run.id

    def _fetch_task(self, task_id: uuid.UUID) -> Task:
        with session_scope() as session:
            task = session.get(Task, task_id)
            if not task:  # pragma: no cover - guard
                raise ValueError(f"Task not found: {task_id}")
            return task

    def _resolve_policy(self, task: Task) -> ValidatorPolicy | None:
        if task.validator_policy:
            return ValidatorPolicy.from_blob(task.validator_policy)
        if task.validators_required:
            return ValidatorPolicy.from_names(task.validators_required)
        return None

    def _augment_policy(
        self,
        policy: ValidatorPolicy | None,
        validator_names: list[str] | None,
    ) -> ValidatorPolicy | None:
        if not validator_names:
            return policy
        base = policy or ValidatorPolicy.from_names(validator_names)
        return base.ensure_rules(validator_names)
