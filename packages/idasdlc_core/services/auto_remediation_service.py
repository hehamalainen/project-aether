"""Automatic remediation helpers for validator failures."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, Sequence

from ..logging import get_logger
from ..validators.base import ValidationContext, ValidationOutcome
from ..validators.registry import REGISTRY, ValidatorRegistry

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .validator_service import PolicyEvaluation

log = get_logger(__name__)


@dataclass(slots=True)
class RemediationResult:
    """Outcome metadata for an auto-remediation attempt."""

    attempted: bool = False
    applied: bool = False
    retries: int = 0
    validators: list[str] = field(default_factory=list)
    fallback_mapping: dict[str, list[str]] = field(default_factory=dict)

    def to_metrics(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "applied": self.applied,
            "retries": self.retries,
            "validators": self.validators,
            "fallbacks": self.fallback_mapping,
        }


class AutoRemediationService:
    """Best-effort reruns for validators that failed or never executed."""

    def __init__(
        self,
        *,
        registry: ValidatorRegistry | None = None,
        fallback_map: dict[str, Sequence[str]] | None = None,
        max_retries: int = 1,
    ) -> None:
        self.registry = registry or REGISTRY
        self.fallback_map: dict[str, list[str]] = {k: list(v) for k, v in (fallback_map or {}).items()}
        self.max_retries = max_retries

    def attempt(
        self,
        *,
        context: ValidationContext,
        evaluation: "PolicyEvaluation",
    ) -> tuple[RemediationResult, list[ValidationOutcome]]:
        plan, fallback_mapping = self._build_plan(evaluation)
        if not plan or self.max_retries <= 0:
            return RemediationResult(validators=plan, fallback_mapping=fallback_mapping), []
        attempts = 0
        last_outcomes: list[ValidationOutcome] = []
        while attempts < self.max_retries:
            attempts += 1
            log.info(
                "auto_remediation_attempt",
                attempt=attempts,
                validators=plan,
            )
            last_outcomes = self.registry.run(plan, context)
            if last_outcomes:
                break
        result = RemediationResult(
            attempted=True,
            retries=attempts,
            validators=plan,
            fallback_mapping=fallback_mapping,
        )
        return result, last_outcomes

    def _build_plan(self, evaluation: "PolicyEvaluation") -> tuple[list[str], dict[str, list[str]]]:
        fallback_mapping: dict[str, list[str]] = {}
        validators: list[str] = []
        for name in evaluation.missing_validators:
            fallback = self.fallback_map.get(name)
            if fallback:
                fallback_mapping[name] = fallback
                validators.extend(fallback)
            else:
                validators.append(name)
        validators.extend(evaluation.required_failures)
        ordered = self._deduplicate(validators)
        return ordered, fallback_mapping

    @staticmethod
    def _deduplicate(names: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        return ordered


__all__ = ["AutoRemediationService", "RemediationResult"]
