"""Validator abstractions."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class ValidationContext:
    repo_path: Path
    env: dict[str, str]


@dataclass(slots=True)
class ValidationOutcome:
    validator_name: str
    passed: bool
    logs: str
    score: float | None = None
    metadata: dict[str, Any] | None = None


class BaseValidator(Protocol):
    name: str

    def run(self, context: ValidationContext) -> ValidationOutcome:
        ...


@dataclass(slots=True)
class CommandValidator:
    name: str
    command: str

    def run(self, context: ValidationContext) -> ValidationOutcome:
        proc = subprocess.run(
            shlex.split(self.command),
            cwd=context.repo_path,
            env={**context.env},
            capture_output=True,
            text=True,
        )
        logs = (proc.stdout or "") + (proc.stderr or "")
        return ValidationOutcome(
            validator_name=self.name,
            passed=proc.returncode == 0,
            logs=logs,
            metadata={"returncode": proc.returncode},
        )
