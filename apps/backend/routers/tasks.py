"""Task-related routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from packages.idasdlc_core.models import Task, TaskPack
from packages.idasdlc_core.services import TaskService

from ..api.dependencies import get_task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[Task])
def list_tasks(
    holdout: bool | None = None,
    task_service: TaskService = Depends(get_task_service),
):
    return task_service.list_tasks(holdout=holdout)


@router.post("/import", response_model=list[Task])
def import_tasks(
    pack: TaskPack,
    task_service: TaskService = Depends(get_task_service),
):
    return task_service.import_task_pack(pack)
