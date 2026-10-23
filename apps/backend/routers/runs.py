"""Run orchestration routes."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from packages.idasdlc_core.models import Run, Trace, ValidationResult
from packages.idasdlc_core.models.validator_policy import ValidatorPolicy
from packages.idasdlc_core.services import OrchestratorService
from packages.idasdlc_core.services.run_service import RunService
from packages.idasdlc_core.utils.policy_summary import (
    summarize_policy_failures,
    summarize_policy_status,
)

from ..api.dependencies import get_orchestrator_service, get_repo_path, get_run_service
from ..services.runner_factory import build_runner

router = APIRouter(prefix="/runs", tags=["runs"])


class RunRequest(BaseModel):
    task_id: uuid.UUID
    agent_id: str = Field(default="static")
    model_id: str = Field(default="demo")
    generation_label: str = Field(default="baseline")
    repo_path: Path | None = None
    commands: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)


class RunResponse(BaseModel):
    run_id: uuid.UUID


class RunSummary(BaseModel):
    run: Run
    policy_summary: str
    failure_summary: str
    auto_remediation: dict[str, Any] = Field(default_factory=dict)


class RunDetail(BaseModel):
    run: Run
    trace: Trace | None = None
    validations: list[ValidationResult] = Field(default_factory=list)
    policy: ValidatorPolicy | None = None
    policy_metrics: dict[str, Any] = Field(default_factory=dict)
    policy_summary: str = "--"
    failure_summary: str = "--"
    auto_remediation: dict[str, Any] = Field(default_factory=dict)


@router.post("", response_model=RunResponse)
def create_run(
    request: RunRequest,
    orchestrator: OrchestratorService = Depends(get_orchestrator_service),
    default_repo: Path = Depends(get_repo_path),
):
    repo = request.repo_path or default_repo
    runner = build_runner(
        request.agent_id,
        commands=request.commands,
        validators=request.validators,
    )
    run_id = orchestrator.run_task(
        task_id=request.task_id,
        agent_id=request.agent_id,
        model_id=request.model_id,
        generation_label=request.generation_label,
        runner=runner,
        repo_path=repo,
    )
    return RunResponse(run_id=run_id)


@router.get("", response_model=list[RunSummary])
def list_runs(
    limit: int = 20,
    run_service: RunService = Depends(get_run_service),
):
    runs = run_service.recent_runs(limit=limit)
    summaries: list[RunSummary] = []
    for run in runs:
        run_metrics = run.metrics or {}
        policy_metrics = run_metrics.get("validator_policy")
        summaries.append(
            RunSummary(
                run=run,
                policy_summary=summarize_policy_status(policy_metrics),
                failure_summary=summarize_policy_failures(policy_metrics),
                auto_remediation=run_metrics.get("auto_remediation", {}),
            )
        )
    return summaries


@router.get("/{run_id}", response_model=RunDetail)
def get_run(
    run_id: uuid.UUID,
    run_service: RunService = Depends(get_run_service),
):
    details = run_service.get_run_details(run_id)
    if not details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    run, trace, validations = details
    policy = ValidatorPolicy.from_blob(run.validator_policy_snapshot)
    run_metrics = run.metrics or {}
    policy_metrics = run_metrics.get("validator_policy", {})
    return RunDetail(
        run=run,
        trace=trace,
        validations=validations,
        policy=policy,
        policy_metrics=policy_metrics,
        policy_summary=summarize_policy_status(policy_metrics),
        failure_summary=summarize_policy_failures(policy_metrics),
        auto_remediation=run_metrics.get("auto_remediation", {}),
    )
