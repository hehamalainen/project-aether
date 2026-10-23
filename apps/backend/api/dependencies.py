"""Dependency providers for FastAPI."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends

from packages.idasdlc_core import get_settings, init_db
from packages.idasdlc_core.retrieval import TraceRetrievalService
from packages.idasdlc_core.services import (
    CurationService,
    EvaluationService,
    ExportService,
    OrchestratorService,
    RewardCardService,
    TaskService,
)
from packages.idasdlc_core.services.run_service import RunService
from packages.idasdlc_core.services.validator_service import ValidatorService


def get_task_service() -> TaskService:
    return TaskService()


def get_run_service() -> RunService:
    return RunService()


def get_validator_service() -> ValidatorService:
    return ValidatorService()


def get_orchestrator_service(
    validator_service: ValidatorService = Depends(get_validator_service),
) -> OrchestratorService:
    return OrchestratorService(validator_service=validator_service)


def get_curation_service() -> CurationService:
    return CurationService()


def get_eval_service() -> EvaluationService:
    return EvaluationService()


def get_export_service() -> ExportService:
    return ExportService()


def get_retrieval_service() -> TraceRetrievalService:
    return TraceRetrievalService()


def get_reward_card_service() -> RewardCardService:
    return RewardCardService()


def get_repo_path() -> Path:
    settings = get_settings()
    settings.ensure_directories()
    init_db()
    return Path.cwd()
