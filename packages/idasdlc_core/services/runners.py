"""Reference agent runners for demos and tests."""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ..models.entities import Task
from .orchestrator import ExecutionResult


class StaticPlanRunner:
    """Simple runner that records the plan without modifying the repo."""

    def __init__(self, *, default_validator: str | None = None) -> None:
        self.default_validator = default_validator

    def execute(self, task: Task, repo_path: Path) -> ExecutionResult:
        now = datetime.now(UTC).isoformat()
        message = {
            "role": "assistant",
            "content": f"Planned solution for task {task.external_id or task.id} at {now}",
        }
        metrics = {
            "diff_size": 0,
            "tool_calls": 0,
            "tokens": len(task.prompt.split()),
            "duration_sec": 0,
        }
        validators = [self.default_validator] if self.default_validator else []
        return ExecutionResult(
            messages=[message],
            tool_calls=[],
            diffs=[],
            artifacts_path=None,
            metrics=metrics,
            validator_names=validators,
        )


class CommandRunner:
    """Runner that executes shell commands inside the repo workspace."""

    def __init__(
        self,
        *,
        commands: Sequence[str],
        validators: Sequence[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        if not commands:
            raise ValueError("CommandRunner requires at least one command")
        self.commands = list(commands)
        self.validators = list(validators or [])
        self.extra_env = extra_env or {}

    def execute(self, task: Task, repo_path: Path) -> ExecutionResult:
        start = time.perf_counter()
        messages: list[dict] = []
        tool_calls: list[dict] = []
        env = os.environ | self.extra_env

        for command in self.commands:
            call_start = time.perf_counter()
            proc = subprocess.run(
                command,
                shell=True,
                cwd=repo_path,
                capture_output=True,
                text=True,
                env=env,
            )
            duration = time.perf_counter() - call_start
            content = (f"$ {command}\n{proc.stdout or ''}{proc.stderr or ''}").strip()
            messages.append({"role": "assistant", "content": content})
            tool_calls.append(
                {
                    "command": command,
                    "returncode": proc.returncode,
                    "duration_sec": round(duration, 4),
                }
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Command '{command}' failed with code {proc.returncode}")

        diffs, diff_size = self._collect_diff(repo_path)
        duration_total = time.perf_counter() - start
        metrics = {
            "diff_size": diff_size,
            "tool_calls": len(tool_calls),
            "duration_sec": round(duration_total, 4),
        }
        validators = self.validators or None
        return ExecutionResult(
            messages=messages,
            tool_calls=tool_calls,
            diffs=diffs,
            artifacts_path=None,
            metrics=metrics,
            validator_names=validators,
        )

    def _collect_diff(self, repo_path: Path) -> tuple[list[str], int]:
        if not (repo_path / ".git").exists():
            return [], 0
        diff_proc = subprocess.run(
            ["git", "diff"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        short_proc = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout.strip()
        diff_size = self._parse_shortstat(short_proc.stdout)
        diffs = [diff_text] if diff_text else []
        return diffs, diff_size

    @staticmethod
    def _parse_shortstat(output: str | None) -> int:
        if not output:
            return 0
        numbers = []
        for token in output.replace(",", "").split():
            if token.isdigit():
                numbers.append(int(token))
        return sum(numbers)


__all__ = ["StaticPlanRunner", "CommandRunner"]
