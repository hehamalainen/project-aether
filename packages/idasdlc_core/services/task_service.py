"""Task management services."""
from __future__ import annotations

from typing import Sequence

from sqlmodel import select

from ..db import session_scope
from ..logging import get_logger
from ..models.base import TaskSource
from ..models.entities import Task
from ..models.task_pack import TaskPack, TaskSpec
from ..models.validator_policy import ValidatorPolicy

log = get_logger(__name__)


class TaskService:
    """Service responsible for persisting and retrieving tasks."""

    def import_task_pack(self, pack: TaskPack) -> list[Task]:
        """Persist tasks defined inside a task pack."""
        created_tasks: list[Task] = []
        metadata = pack.metadata
        for spec in pack.tasks:
            created = self._upsert_task(
                spec=spec,
                default_tags=metadata.tags,
                default_validators=metadata.validators,
                default_holdout=metadata.holdout,
                default_repo=metadata.repo,
                default_base_sha=metadata.base_sha,
                default_policy=metadata.validator_policy,
            )
            if created:
                created_tasks.append(created)
        log.info(
            "task_pack_imported",
            name=metadata.name,
            imported=len(created_tasks),
        )
        return created_tasks

    def _upsert_task(
        self,
        spec: TaskSpec,
        default_tags: Sequence[str],
        default_validators: Sequence[str],
        default_holdout: bool,
        default_repo: str | None,
        default_base_sha: str | None,
        default_policy: ValidatorPolicy | None,
    ) -> Task | None:
        with session_scope() as session:
            repo = spec.repo or default_repo or "unknown"
            stmt = select(Task).where(Task.external_id == spec.external_id, Task.repo == repo)
            existing = session.exec(stmt).first() if spec.external_id else None
            if existing:
                log.info("task_exists", external_id=spec.external_id, repo=repo)
                return None

            policy = spec.validator_policy or default_policy
            task = Task(
                source=TaskSource.LOCAL,
                external_id=spec.external_id,
                repo=repo,
                base_sha=spec.base_sha or default_base_sha or pack_base_sha_placeholder(),
                prompt=spec.prompt,
                constraints=spec.constraints,
                tags=list({*default_tags, *spec.tags}),
                validators_required=list({*default_validators, *spec.validators}),
                validator_policy=policy.to_blob() if policy else {},
                holdout=spec.holdout if spec.holdout is not None else default_holdout,
            )
            session.add(task)
            log.debug("task_created", task_id=str(task.id))
            return task

    def list_tasks(self, holdout: bool | None = None) -> list[Task]:
        """Return tasks filtered by holdout flag if provided."""
        with session_scope() as session:
            stmt = select(Task)
            if holdout is not None:
                stmt = stmt.where(Task.holdout == holdout)
            return list(session.exec(stmt).all())


def pack_base_sha_placeholder() -> str:
    """Fallback base SHA when none provided."""
    return "HEAD"
