"""Built-in validator registry."""
from __future__ import annotations

import os
from typing import Dict

from .base import BaseValidator, CommandValidator, ValidationContext, ValidationOutcome


def default_validators() -> Dict[str, BaseValidator]:
    return {
        "tests": CommandValidator(name="tests", command="pytest"),
        "lint": CommandValidator(name="lint", command="ruff check"),
        "typecheck": CommandValidator(name="typecheck", command="mypy ."),
    }


class ValidatorRegistry:
    def __init__(self) -> None:
        self._validators: Dict[str, BaseValidator] = default_validators()

    def register(self, validator: BaseValidator) -> None:
        self._validators[validator.name] = validator

    def get(self, name: str) -> BaseValidator:
        if name not in self._validators:  # pragma: no cover - defensive guard
            raise KeyError(f"Validator not found: {name}")
        return self._validators[name]

    def run(self, names: list[str], context: ValidationContext) -> list[ValidationOutcome]:
        outcomes: list[ValidationOutcome] = []
        for name in names:
            validator = self.get(name)
            env = os.environ | context.env
            outcome = validator.run(ValidationContext(repo_path=context.repo_path, env=env))
            outcomes.append(outcome)
        return outcomes


REGISTRY = ValidatorRegistry()
