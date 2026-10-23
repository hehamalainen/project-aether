"""CLI run coverage."""
from __future__ import annotations

import json
import uuid

from typer.testing import CliRunner

from apps.cli.main import app
from packages.idasdlc_core.db import session_scope
from packages.idasdlc_core.models.base import RunStatus
from packages.idasdlc_core.models.entities import Task
from packages.idasdlc_core.models.validator_policy import (
    ValidatorPolicy,
    ValidatorRule,
    ValidatorSeverity,
)
from packages.idasdlc_core.services.run_service import RunService

runner = CliRunner()


def _create_task() -> uuid.UUID:
    with session_scope() as session:
        task = Task(repo="repo", prompt="demo", base_sha="HEAD")
        session.add(task)
        session.flush()
        session.refresh(task)
        return task.id


def _seed_run_with_validations() -> tuple[uuid.UUID, ValidatorPolicy, dict, dict]:
    task_id = _create_task()
    policy = ValidatorPolicy(
        rules=[
            ValidatorRule(name="tests", severity=ValidatorSeverity.CRITICAL),
            ValidatorRule(name="lint", severity=ValidatorSeverity.ADVISORY, must_pass=False),
        ],
        minimum_pass=1,
    )
    run_service = RunService()
    run = run_service.create_run(
        task_id=task_id,
        agent_id="agent",
        model_id="model",
        generation_label="baseline",
        validator_policy=policy.to_blob(),
    )
    run_service.record_validation(
        run.id,
        validator_name="tests",
        passed=True,
        score=0.9,
        logs_text="ok",
        metadata={"elapsed": 1.0},
    )
    run_service.record_validation(
        run.id,
        validator_name="lint",
        passed=False,
        score=None,
        logs_text="lint failure",
        metadata={"elapsed": 0.5},
    )
    policy_metrics = {
        "passed": True,
        "passed_count": 1,
        "minimum_pass": 1,
        "required_failures": [],
        "advisory_failures": ["lint"],
        "missing": [],
    }
    auto_metrics = {
        "attempted": True,
        "applied": False,
        "retries": 1,
        "validators": ["lint"],
        "fallbacks": {"lint": ["lint_alt"]},
    }
    run_service.update_status(
        run.id,
        status=RunStatus.SUCCEEDED,
        metrics={
            "validator_policy": policy_metrics,
            "auto_remediation": auto_metrics,
        },
        finished=True,
    )

    return run.id, policy, policy_metrics, auto_metrics


def test_run_detail_outputs_policy_and_validations(isolated_settings):
    run_id, _, _, _ = _seed_run_with_validations()

    result = runner.invoke(app, ["run", "detail", str(run_id)])

    assert result.exit_code == 0
    assert "Policy evaluation" in result.stdout
    assert "tests" in result.stdout
    assert "lint" in result.stdout
    assert "advisory failures".lower() in result.stdout.lower()
    assert "Auto-remediation attempted" in result.stdout
    assert "Targeted validators: lint" in result.stdout


def test_run_detail_json_outputs_full_payload(isolated_settings):
    run_id, policy, policy_metrics, auto_metrics = _seed_run_with_validations()

    result = runner.invoke(app, ["run", "detail", str(run_id), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["id"] == str(run_id)
    assert payload["trace"] is None
    assert payload["policy"]["minimum_pass"] == policy.minimum_pass
    assert {rule["name"] for rule in payload["policy"]["rules"]} == {rule.name for rule in policy.rules}
    assert payload["policy_metrics"] == policy_metrics
    assert payload["auto_remediation"] == auto_metrics
    validations = payload["validations"]
    assert len(validations) == 2
    assert {item["validator_name"] for item in validations} == {"tests", "lint"}


def test_run_recent_outputs_policy_summary(isolated_settings):
    run_id, _, _, _ = _seed_run_with_validations()

    result = runner.invoke(app, ["run", "recent", "--limit", "1"])

    assert result.exit_code == 0
    assert str(run_id) in result.stdout
    assert "PASS (1/1)" in result.stdout
    assert "adv: lint" in result.stdout
    assert "Auto-Remediation" in result.stdout
    assert "attempted | 1x | lint" in result.stdout
    assert "Failure Summary" in result.stdout
