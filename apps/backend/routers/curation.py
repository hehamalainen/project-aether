"""Curation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from packages.idasdlc_core.services import CurationService, RewardCardService, TaskService

from ..api.dependencies import (
    get_curation_service,
    get_reward_card_service,
    get_task_service,
)

router = APIRouter(prefix="/generations", tags=["curation"])


@router.post("/{label}/curate")
def curate_generation(
    label: str,
    holdout: bool = False,
    curator: CurationService = Depends(get_curation_service),
    task_service: TaskService = Depends(get_task_service),
) -> dict:
    tasks = task_service.list_tasks(holdout=holdout)
    curated = 0
    for task in tasks:
        if curator.curate_task(task.id, generation_label=label):
            curated += 1
    return {"curated": curated}


@router.post("/{label}/reward-card")
def generate_reward_card(
    label: str,
    reward_service: RewardCardService = Depends(get_reward_card_service),
) -> dict:
    path = reward_service.generate(label)
    return {"path": str(path)}
