"""Reward card tests."""
from __future__ import annotations

import json

from packages.idasdlc_core.db import session_scope
from packages.idasdlc_core.models.base import PromotionStatus, RunStatus
from packages.idasdlc_core.models.entities import (
    CuratedExample,
    Generation,
    Run,
    Task,
    ValidationResult,
)
from packages.idasdlc_core.services.reward_card_service import RewardCardService


def seed_generation():
    with session_scope() as session:
        task = Task(
            repo="repo",
            prompt="Task",
            base_sha="sha",
            validators_required=["tests"],
            tags=[],
        )
        session.add(task)
        session.flush()

        run = Run(
            task_id=task.id,
            agent_id="agent",
            model_id="model",
            generation_label="g1",
            status=RunStatus.SUCCEEDED,
        )
        session.add(run)
        session.flush()

        generation = Generation(label="g1", promotion_status=PromotionStatus.DRAFT)
        session.add(generation)
        session.flush()

        curated = CuratedExample(
            task_id=task.id,
            chosen_run_id=run.id,
            generation_id=generation.id,
            rationale="best",
        )
        session.add(curated)

        validation = ValidationResult(
            run_id=run.id,
            validator_name="tests",
            passed=True,
            metadata_blob={},
        )
        session.add(validation)

        session.commit()


def test_reward_card_generation_creates_file(isolated_settings):
    seed_generation()
    service = RewardCardService()

    path = service.generate("g1")

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["generation"]["label"] == "g1"
    assert payload["validators"][0]["name"] == "tests"
    assert payload["runs"][0]["validators"] == ["tests"]
