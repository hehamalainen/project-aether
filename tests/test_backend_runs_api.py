"""Backend run detail endpoint coverage."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from apps.backend.main import create_app
from packages.idasdlc_core.db import session_scope
from packages.idasdlc_core.models.base import RunStatus
from packages.idasdlc_core.models.entities import Task
from packages.idasdlc_core.models.validator_policy import (
    ValidatorPolicy,
    ValidatorRule,
    ValidatorSeverity,
)
from packages.idasdlc_core.services.run_service import RunService


def _create_task() -> uuid.UUID:
    with session_scope() as session:
        task = Task(repo="backend", prompt="observe", base_sha="HEAD")
        session.add(task)
        session.flush()
        session.refresh(task)
        return task.id


def _create_run_with_policy() -> uuid.UUID:
    task_id = _create_task()
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.REQUIRED),
            ValidatorRule(name="lint", severity=ValidatorSeverity.ADVISORY, must_pass=False),
        ],
        minimum_pass=1,
    )
    run_service = RunService()
    run = run_service.create_run(
        task_id=task_id,
        agent_id="agent",
        model_id="model",
        generation_label="backend",
        validator_policy=policy.to_blob(),
    )
    run_service.record_validation(
        run.id,
        validator_name="tests",
        passed=True,
        score=1.0,
        logs_text="tests ok",
        metadata={},
    )
    run_service.record_validation(
        run.id,
        validator_name="lint",
        passed=False,
        score=None,
        logs_text="lint warn",
        metadata={},
    )
    policy_metrics = {
        "passed": True,
        "passed_count": 1,
        "minimum_pass": 1,
        "required_failures": [],
        "advisory_failures": ["lint"],
        "missing": [],
    }
    run_service.update_status(
        run.id,
        status=RunStatus.SUCCEEDED,
        metrics={"validator_policy": policy_metrics},
        finished=True,
    )
    return run.id


def test_run_detail_includes_policy_and_metrics(isolated_settings):
    run_id = _create_run_with_policy()
    client = TestClient(create_app())

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["policy"]["minimum_pass"] == 1
    assert {rule["name"] for rule in payload["policy"]["rules"]} == {"tests", "lint"}
    assert payload["policy_metrics"]["passed"] is True
    assert payload["policy_metrics"]["advisory_failures"] == ["lint"]
    assert sorted(v["validator_name"] for v in payload["validations"]) == ["lint", "tests"]
    assert payload["policy_summary"] == "PASS (1/1)"
    assert payload["failure_summary"] == "adv: lint"


def test_list_runs_includes_policy_summary(isolated_settings):
    run_id = _create_run_with_policy()
    client = TestClient(create_app())

    response = client.get("/runs", params={"limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    item = payload[0]
    assert item["run"]["id"] == str(run_id)
    assert item["policy_summary"] == "PASS (1/1)"
    assert "adv: lint" in item["failure_summary"]
