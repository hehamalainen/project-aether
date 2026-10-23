"""Evaluation routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from packages.idasdlc_core.services import EvaluationService, TaskService

from ..api.dependencies import get_eval_service, get_task_service

router = APIRouter(prefix="/evals", tags=["evals"])


@router.get("/solved")
def solved_counts(
    holdout: bool | None = None,
    eval_service: EvaluationService = Depends(get_eval_service),
) -> dict[str, int]:
    return eval_service.solved_counts(holdout=holdout)


@router.get("/solved-at-k")
def solved_at_k(
    k: int = 3,
    holdout: bool | None = None,
    eval_service: EvaluationService = Depends(get_eval_service),
    task_service: TaskService = Depends(get_task_service),
) -> dict[str, float | int]:
    tasks = task_service.list_tasks(holdout=holdout)
    task_ids = [task.id for task in tasks]
    score = eval_service.solved_at_k(task_ids, k=k)
    return {
        "k": k,
        "task_count": len(task_ids),
        "solved_at_k": score,
    }
