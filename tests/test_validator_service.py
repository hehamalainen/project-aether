"""Validator policy enforcement tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from packages.idasdlc_core.db import session_scope
from packages.idasdlc_core.models.base import RunStatus
from packages.idasdlc_core.models.entities import Task
from packages.idasdlc_core.models.validator_policy import (
    ValidatorPolicy,
    ValidatorRule,
    ValidatorSeverity,
)
from packages.idasdlc_core.services.auto_remediation_service import RemediationResult
from packages.idasdlc_core.services.run_service import RunService
from packages.idasdlc_core.services.validator_service import ValidatorService
from packages.idasdlc_core.validators.base import ValidationOutcome


@pytest.fixture()
def sample_task() -> Task:
    with session_scope() as session:
        task = Task(repo="repo", prompt="demo", base_sha="HEAD")
        session.add(task)
        session.flush()
        session.refresh(task)
        return task


def _patch_registry(monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, ValidationOutcome]) -> None:
    from packages.idasdlc_core.services import validator_service as module

    def fake_run(names, context):
        return [outcomes[name] for name in names]

    monkeypatch.setattr(module.REGISTRY, "run", fake_run)


def _patch_registry_sequence(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, ValidationOutcome]],
) -> None:
    from packages.idasdlc_core.services import validator_service as module

    iterator = iter(calls)

    def fake_run(names, context):
        mapping = next(iterator)
        return [mapping[name] for name in names if name in mapping]

    monkeypatch.setattr(module.REGISTRY, "run", fake_run)


def test_policy_failure_on_critical(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_task: Task):
    run = RunService().create_run(
        task_id=sample_task.id,
        agent_id="agent",
        model_id="model",
        generation_label="baseline",
    )
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.CRITICAL),
            ValidatorRule(name="lint", severity=ValidatorSeverity.ADVISORY, must_pass=False),
        ],
        minimum_pass=1,
    )
    outcomes = {
        "tests": ValidationOutcome(validator_name="tests", passed=False, logs="boom"),
        "lint": ValidationOutcome(validator_name="lint", passed=True, logs="ok"),
    }
    _patch_registry(monkeypatch, outcomes)

    service = ValidatorService()
    passed = service.run_pipeline(run_id=run.id, repo_path=tmp_path, policy=policy)

    assert passed is False
    stored = RunService().get_run(run.id)
    assert stored is not None
    assert stored.status is RunStatus.VALIDATION_FAILED
    metrics = stored.metrics["validator_policy"]
    assert metrics["minimum_pass"] == 1
    assert metrics["required_failures"] == ["tests"]


def test_policy_respects_advisory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_task: Task):
    run = RunService().create_run(
        task_id=sample_task.id,
        agent_id="agent",
        model_id="model",
        generation_label="baseline",
    )
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.REQUIRED),
            ValidatorRule(name="lint", severity=ValidatorSeverity.ADVISORY, must_pass=False),
        ],
        minimum_pass=1,
    )
    outcomes = {
        "tests": ValidationOutcome(validator_name="tests", passed=True, logs="ok"),
        "lint": ValidationOutcome(validator_name="lint", passed=False, logs="warn"),
    }
    _patch_registry(monkeypatch, outcomes)

    passed = ValidatorService().run_pipeline(run_id=run.id, repo_path=tmp_path, policy=policy)

    assert passed is True
    stored = RunService().get_run(run.id)
    assert stored is not None
    assert stored.status is RunStatus.SUCCEEDED
    metrics = stored.metrics["validator_policy"]
    assert metrics["advisory_failures"] == ["lint"]
    assert metrics["required_failures"] == []


def test_auto_remediation_retries_failed_validators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_task: Task,
):
    run = RunService().create_run(
        task_id=sample_task.id,
        agent_id="agent",
        model_id="model",
        generation_label="baseline",
    )
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.CRITICAL),
        ],
        minimum_pass=1,
    )
    failure = ValidationOutcome(validator_name="tests", passed=False, logs="boom")
    success = ValidationOutcome(validator_name="tests", passed=True, logs="rerun ok")
    _patch_registry_sequence(monkeypatch, [{"tests": failure}, {"tests": success}])

    service = ValidatorService()
    passed = service.run_pipeline(run_id=run.id, repo_path=tmp_path, policy=policy)

    assert passed is True
    stored = RunService().get_run(run.id)
    assert stored is not None
    assert stored.status is RunStatus.SUCCEEDED
    remediation = stored.metrics["auto_remediation"]
    assert remediation["attempted"] is True
    assert remediation["applied"] is True
    assert remediation["validators"] == ["tests"]


def test_auto_remediation_handles_missing_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_task: Task,
):
    run = RunService().create_run(
        task_id=sample_task.id,
        agent_id="agent",
        model_id="model",
        generation_label="baseline",
    )
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.REQUIRED),
            ValidatorRule(name="lint", severity=ValidatorSeverity.REQUIRED),
        ],
        minimum_pass=2,
    )

    def fake_run(names, context):
        results = []
        mapping = {
            "tests": ValidationOutcome(validator_name="tests", passed=True, logs="ok"),
        }
        for name in names:
            if name in mapping:
                results.append(mapping[name])
        return results

    from packages.idasdlc_core.services import validator_service as module

    monkeypatch.setattr(module.REGISTRY, "run", fake_run)

    class StubRemediator:
        def attempt(self, *, context, evaluation):
            assert evaluation.missing_validators == ["lint"]
            outcome = ValidationOutcome(validator_name="lint", passed=True, logs="auto lint")
            return RemediationResult(
                attempted=True,
                retries=1,
                validators=["lint"],
                fallback_mapping={},
            ), [outcome]

    service = ValidatorService(auto_remediator=StubRemediator())
    passed = service.run_pipeline(run_id=run.id, repo_path=tmp_path, policy=policy)

    assert passed is True
    stored = RunService().get_run(run.id)
    assert stored is not None
    assert stored.status is RunStatus.SUCCEEDED
    remediation = stored.metrics["auto_remediation"]
    assert remediation["attempted"] is True
    assert remediation["applied"] is True
    assert remediation["validators"] == ["lint"]