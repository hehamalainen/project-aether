"""Task service tests."""
from __future__ import annotations

from packages.idasdlc_core.models.task_pack import TaskPack, TaskPackMetadata, TaskSpec
from packages.idasdlc_core.models.validator_policy import (
    ValidatorPolicy,
    ValidatorRule,
    ValidatorSeverity,
)
from packages.idasdlc_core.services.task_service import TaskService


def test_import_task_pack_merges_metadata(isolated_settings):
    default_policy = ValidatorPolicy(
        rules=[ValidatorRule(name="tests", severity=ValidatorSeverity.CRITICAL)],
        minimum_pass=1,
    )
    pack = TaskPack(
        metadata=TaskPackMetadata(
            name="demo",
            repo="example/repo",
            base_sha="abc123",
            tags=["baseline"],
            validators=["tests"],
            holdout=False,
            validator_policy=default_policy,
        ),
        tasks=[
            TaskSpec(
                external_id="TASK-1",
                prompt="Fix bug",
                tags=["bug"],
                validators=["lint"],
                validator_policy=ValidatorPolicy(
                    rules=[
                        ValidatorRule(name="lint", severity=ValidatorSeverity.REQUIRED),
                        ValidatorRule(
                            name="typecheck",
                            severity=ValidatorSeverity.ADVISORY,
                            must_pass=False,
                        ),
                    ],
                    minimum_pass=1,
                ),
            ),
            TaskSpec(
                external_id="TASK-2",
                prompt="Add feature",
                holdout=True,
            ),
        ],
    )

    service = TaskService()
    created = service.import_task_pack(pack)

    assert len(created) == 2
    stored = service.list_tasks()
    by_ext = {task.external_id: task for task in stored}

    first = by_ext["TASK-1"]
    assert set(first.tags) == {"baseline", "bug"}
    assert set(first.validators_required) == {"tests", "lint"}
    assert first.holdout is False
    assert first.base_sha == "abc123"
    first_policy = ValidatorPolicy.from_blob(first.validator_policy)
    assert first_policy is not None
    assert first_policy.minimum_pass == 1
    assert {rule.name for rule in first_policy.rules} == {"lint", "typecheck"}
    advisory = next(rule for rule in first_policy.rules if rule.name == "typecheck")
    assert advisory.severity is ValidatorSeverity.ADVISORY
    assert advisory.must_pass is False

    second = by_ext["TASK-2"]
    assert second.holdout is True
    assert second.repo == "example/repo"
    second_policy = ValidatorPolicy.from_blob(second.validator_policy)
    assert second_policy is not None
    assert second_policy.minimum_pass == 1
    assert [rule.name for rule in second_policy.rules] == ["tests"]
