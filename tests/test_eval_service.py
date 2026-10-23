"""Evaluation service tests."""
from __future__ import annotations

from datetime import UTC, datetime

from packages.idasdlc_core.db import session_scope
from packages.idasdlc_core.models.base import RunStatus
from packages.idasdlc_core.models.entities import Run, Task
from packages.idasdlc_core.services.eval_service import EvaluationService


def _create_task(prompt: str) -> Task:
    with session_scope() as session:
        task = Task(repo="repo", prompt=prompt, base_sha="sha", tags=[], validators_required=[])
        session.add(task)
        session.flush()
        return task


def _create_run(task_id, status):
    with session_scope() as session:
        run = Run(
            task_id=task_id,
            agent_id="agent",
            model_id="model",
            generation_label="g",
            status=status,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        return run


def test_solved_at_k_counts_success(isolated_settings):
    task = _create_task("Solve it")
    _create_run(task.id, RunStatus.SUCCEEDED)
    service = EvaluationService()

    score = service.solved_at_k([task.id], k=1)

    assert score == 1.0


def test_solved_at_k_partial_success(isolated_settings):
    task = _create_task("Partial")
    _create_run(task.id, RunStatus.FAILED)
    _create_run(task.id, RunStatus.SUCCEEDED)
    service = EvaluationService()

    score = service.solved_at_k([task.id], k=1)
    assert score == 0.0

    score = service.solved_at_k([task.id], k=2)
    assert score == 1.0
