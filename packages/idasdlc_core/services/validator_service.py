"""Validator execution entry points."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..logging import get_logger
from ..models.base import RunStatus
from ..models.validator_policy import ValidatorPolicy, ValidatorRule
from ..validators.base import ValidationContext, ValidationOutcome
from ..validators.registry import REGISTRY
from .auto_remediation_service import AutoRemediationService, RemediationResult
from .run_service import RunService

log = get_logger(__name__)


@dataclass(slots=True)
class PolicyEvaluation:
    passed: bool
    passed_count: int
    minimum_pass: int | None
    required_failures: list[str]
    advisory_failures: list[str]
    missing_validators: list[str]


class ValidatorService:
    def __init__(
        self,
        run_service: RunService | None = None,
        auto_remediator: AutoRemediationService | None = None,
    ) -> None:
        self.run_service = run_service or RunService()
        self.auto_remediator = auto_remediator or AutoRemediationService()

    def run_pipeline(
        self,
        *,
        run_id,
        repo_path: Path,
        validator_names: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
        policy: ValidatorPolicy | None = None,
    ) -> bool:
        context = ValidationContext(repo_path=repo_path, env=env or {})
        resolved_policy, names = self._select_policy(policy, validator_names)
        if not names:
            log.warning("no_validators_defined", run_id=str(run_id))
            self.run_service.update_status(run_id, status=RunStatus.SUCCEEDED, finished=True)
            return True
        outcomes = REGISTRY.run(names, context)
        evaluation = self._evaluate_policy(resolved_policy, outcomes)
        for outcome in outcomes:
            self.run_service.record_validation(
                run_id,
                validator_name=outcome.validator_name,
                passed=outcome.passed,
                score=outcome.score,
                logs_text=outcome.logs,
                metadata=outcome.metadata or {},
            )
        remediation_result = RemediationResult()
        if not evaluation.passed and self.auto_remediator:
            remediation_result, remediation_outcomes = self.auto_remediator.attempt(
                context=context,
                evaluation=evaluation,
            )
            if remediation_result.attempted and remediation_outcomes:
                for outcome in remediation_outcomes:
                    metadata = dict(outcome.metadata or {})
                    metadata["auto_remediation"] = True
                    self.run_service.record_validation(
                        run_id,
                        validator_name=outcome.validator_name,
                        passed=outcome.passed,
                        score=outcome.score,
                        logs_text=outcome.logs,
                        metadata=metadata,
                    )
                outcomes = self._merge_outcomes(outcomes, remediation_outcomes)
                evaluation = self._evaluate_policy(resolved_policy, outcomes)
        remediation_result.applied = evaluation.passed
        status = RunStatus.SUCCEEDED if evaluation.passed else RunStatus.VALIDATION_FAILED
        self.run_service.update_status(
            run_id,
            status=status,
            finished=True,
            metrics={
                "validator_policy": {
                    "passed": evaluation.passed,
                    "passed_count": evaluation.passed_count,
                    "minimum_pass": evaluation.minimum_pass,
                    "required_failures": evaluation.required_failures,
                    "advisory_failures": evaluation.advisory_failures,
                    "missing": evaluation.missing_validators,
                },
                "auto_remediation": remediation_result.to_metrics(),
            },
        )
        log.info(
            "validators_completed",
            run_id=str(run_id),
            passed=evaluation.passed,
            passed_count=evaluation.passed_count,
            required_failures=evaluation.required_failures,
            advisory_failures=evaluation.advisory_failures,
            missing=evaluation.missing_validators,
        )
        return evaluation.passed

    def _select_policy(
        self,
        policy: ValidatorPolicy | None,
        validator_names: Iterable[str] | None,
    ) -> tuple[ValidatorPolicy | None, list[str]]:
        incoming = list(validator_names or [])
        if policy:
            updated = policy.ensure_rules(incoming)
            final_names = updated.validator_names() if updated.rules else incoming
            return updated, final_names
        if incoming:
            generated = ValidatorPolicy.from_names(incoming)
            return generated, incoming
        return None, []

    def _evaluate_policy(
        self,
        policy: ValidatorPolicy | None,
        outcomes: list[ValidationOutcome],
    ) -> PolicyEvaluation:
        if not policy:
            failures = [outcome.validator_name for outcome in outcomes if not outcome.passed]
            passed_count = len(outcomes) - len(failures)
            passed = not failures
            return PolicyEvaluation(
                passed=passed,
                passed_count=passed_count,
                minimum_pass=None,
                required_failures=failures,
                advisory_failures=[],
                missing_validators=[],
            )

        rule_map: dict[str, ValidatorRule] = {rule.name: rule for rule in policy.rules}
        outcome_map: dict[str, ValidationOutcome] = {outcome.validator_name: outcome for outcome in outcomes}
        required_failures: list[str] = []
        advisory_failures: list[str] = []
        missing: list[str] = []
        passed_count = 0

        def _log_failure(name: str, rule: ValidatorRule) -> None:
            target = required_failures if rule.requires_success() else advisory_failures
            target.append(name)

        for name, rule in rule_map.items():
            outcome = outcome_map.get(name)
            if not outcome:
                missing.append(name)
                _log_failure(name, rule)
                continue
            meets_score = True
            if rule.min_score is not None:
                meets_score = outcome.score is not None and outcome.score >= rule.min_score
            is_success = outcome.passed and meets_score
            if is_success:
                passed_count += 1
            else:
                _log_failure(name, rule)

        for name, outcome in outcome_map.items():
            if name in rule_map:
                continue
            if outcome.passed:
                passed_count += 1
            else:
                required_failures.append(name)

        meets_minimum = True
        if policy.minimum_pass is not None:
            meets_minimum = passed_count >= policy.minimum_pass
        passed = not required_failures and meets_minimum
        return PolicyEvaluation(
            passed=passed,
            passed_count=passed_count,
            minimum_pass=policy.minimum_pass,
            required_failures=required_failures,
            advisory_failures=advisory_failures,
            missing_validators=missing,
        )

    @staticmethod
    def _merge_outcomes(
        primary: list[ValidationOutcome],
        remediation: list[ValidationOutcome],
    ) -> list[ValidationOutcome]:
        merged: dict[str, ValidationOutcome] = {item.validator_name: item for item in primary}
        for outcome in remediation:
            merged[outcome.validator_name] = outcome
        return list(merged.values())
