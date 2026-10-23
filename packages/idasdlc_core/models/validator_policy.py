"""Validator policy schemas."""
from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, Field


class ValidatorSeverity(str, Enum):
    CRITICAL = "critical"
    REQUIRED = "required"
    ADVISORY = "advisory"


class ValidatorRule(BaseModel):
    name: str
    severity: ValidatorSeverity = ValidatorSeverity.REQUIRED
    must_pass: bool | None = None
    min_score: float | None = None

    def requires_success(self) -> bool:
        if self.must_pass is not None:
            return self.must_pass
        return self.severity in {ValidatorSeverity.CRITICAL, ValidatorSeverity.REQUIRED}


class ValidatorPolicy(BaseModel):
    rules: list[ValidatorRule] = Field(default_factory=list)
    minimum_pass: int | None = None

    def validator_names(self) -> list[str]:
        return [rule.name for rule in self.rules]

    def ensure_rules(self, names: Iterable[str]) -> "ValidatorPolicy":
        existing = {rule.name for rule in self.rules}
        updated = list(self.rules)
        for name in names:
            if name not in existing:
                updated.append(ValidatorRule(name=name))
        return ValidatorPolicy(rules=updated, minimum_pass=self.minimum_pass)

    def to_blob(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_blob(cls, blob: dict[str, Any] | None) -> "ValidatorPolicy" | None:
        if not blob:
            return None
        return cls.model_validate(blob)

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "ValidatorPolicy":
        return cls(rules=[ValidatorRule(name=name) for name in names])