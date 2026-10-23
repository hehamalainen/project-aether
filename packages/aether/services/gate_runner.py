"""
Gate runner for executing verification gates on snapshots.

Handles:
- Running configurable shell commands (syntax, build, test, lint, security)
- Collecting results and artifacts
- Timeout management
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
import os
import re
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional, Literal
import structlog

from sqlmodel import Session, select

from ..models.entities import (
    GateResult,
    GateStatus,
    GateConfig,
    Snapshot,
)
from ..models.events import EventType
from .event_bus import EventBus, create_gate_event
from .gate_manifest import load_gate_manifest, ManifestLoadResult

logger = structlog.get_logger()



# Default gate configurations (explicit simulation/no-op mode unless real repository gates are configured)
DEFAULT_GATES = [
    GateConfig(
        name="syntax",
        command="SIMULATED: syntax check skipped - no-op default gate",
        required=False,
        timeout_seconds=60,
        order=0,
    ),
    GateConfig(
        name="build",
        command="SIMULATED: build skipped - no-op default gate",
        required=True,
        timeout_seconds=300,
        order=1,
    ),
    GateConfig(
        name="unit_tests",
        command="SIMULATED: unit_tests skipped - no-op default gate",
        required=True,
        timeout_seconds=600,
        order=2,
    ),
    GateConfig(
        name="lint",
        command="SIMULATED: lint skipped - no-op default gate",
        required=False,
        timeout_seconds=120,
        order=3,
    ),
]

_SHELL_METACHAR_PATTERN = re.compile(r"[;&|`$><\n\r\\()]")

SAFE_GATE_ENV_KEYS = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "SYSTEMROOT",
)


def is_safe_simulation_command(command: str) -> bool:
    """Return True only for built-in no-op/simulation gate commands with no shell metacharacters."""
    stripped = command.strip()
    if not stripped or _SHELL_METACHAR_PATTERN.search(stripped):
        return False
    if stripped in {"true", "false", "exit 0", "exit 1"}:
        return True
    if stripped.startswith("SIMULATED:"):
        return True
    if stripped.startswith("echo "):
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            return False
        return len(tokens) >= 1 and tokens[0] == "echo"
    return False


@dataclass
class _GateExecutionPayload:
    """Result payload produced by executing a gate command."""

    status: GateStatus
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class GateRunner:
    """Service for running verification gates."""

    def __init__(
        self,
        session: Session,
        event_bus: EventBus,
        *,
        allow_custom_commands: Optional[bool] = None,
    ):
        self.session = session
        self.event_bus = event_bus
        self._allow_custom_commands = allow_custom_commands
        self._gate_hard_timeout_seconds = self._load_hard_timeout_cap()

    def _is_custom_command_execution_allowed(self) -> bool:
        """Check whether non-simulated custom shell gate commands are permitted."""
        if self._allow_custom_commands is not None:
            return bool(self._allow_custom_commands)
        return os.getenv("AETHER_ALLOW_CUSTOM_GATE_COMMANDS", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _load_hard_timeout_cap() -> float | None:
        raw = os.getenv("AETHER_GATE_HARD_TIMEOUT_SECONDS")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("invalid_gate_hard_timeout_env", value=raw)
            return None
        if value <= 0:
            return None
        return value

    def _resolve_gate_timeout_seconds(self, configured_timeout_seconds: int) -> float:
        timeout_seconds = float(configured_timeout_seconds)
        if self._gate_hard_timeout_seconds is None:
            return timeout_seconds
        return min(timeout_seconds, self._gate_hard_timeout_seconds)

    def _build_gate_env(
        self,
        *,
        snapshot_id: str,
        gate_name: str,
        proposal_id: Optional[str],
        env_vars: Optional[dict[str, str]],
    ) -> dict[str, str]:
        """Build a scrubbed environment dictionary passed to a gate command."""
        inherit_full = os.getenv("AETHER_GATE_INHERIT_FULL_ENV", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if inherit_full:
            env = os.environ.copy()
        else:
            env = {
                key: os.environ[key]
                for key in SAFE_GATE_ENV_KEYS
                if key in os.environ
            }
        env["AETHER_SNAPSHOT_ID"] = snapshot_id
        env["AETHER_GATE_NAME"] = gate_name
        if proposal_id:
            env["AETHER_PROPOSAL_ID"] = proposal_id
        if env_vars:
            env.update(env_vars)
        return env

    async def _execute_gate_command(
        self,
        gate_config: GateConfig,
        working_dir: Path,
        env: dict[str, str],
    ) -> _GateExecutionPayload:
        """Execute one gate command and return normalized output payload."""
        started = time.perf_counter()
        timeout_seconds = self._resolve_gate_timeout_seconds(gate_config.timeout_seconds)

        # Fail closed check
        if gate_config.command.startswith("FAIL_CLOSED:"):
            return _GateExecutionPayload(
                status=GateStatus.FAIL,
                stdout="",
                stderr=gate_config.command[12:].strip(),
                exit_code=1,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        # Safe in-process evaluation for built-in simulation commands (no shell spawned)
        if is_safe_simulation_command(gate_config.command):
            stripped = gate_config.command.strip()
            if stripped in {"true", "exit 0"}:
                return _GateExecutionPayload(
                    status=GateStatus.PASS,
                    stdout="",
                    stderr="",
                    exit_code=0,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            if stripped in {"false", "exit 1"}:
                return _GateExecutionPayload(
                    status=GateStatus.FAIL,
                    stdout="",
                    stderr="",
                    exit_code=1,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            if stripped.startswith("SIMULATED:"):
                msg = stripped[len("SIMULATED:"):].strip()
                return _GateExecutionPayload(
                    status=GateStatus.PASS,
                    stdout=f"[SIMULATED] {msg}\n" if msg else "[SIMULATED]\n",
                    stderr="",
                    exit_code=0,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            if stripped.startswith("echo "):
                tokens = shlex.split(stripped)
                text_out = " ".join(tokens[1:]) + "\n"
                return _GateExecutionPayload(
                    status=GateStatus.PASS,
                    stdout=text_out,
                    stderr="",
                    exit_code=0,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

        if not self._is_custom_command_execution_allowed():
            logger.warning(
                "custom_gate_command_refused",
                gate_name=gate_config.name,
                command=gate_config.command,
            )
            return _GateExecutionPayload(
                status=GateStatus.ERROR,
                stdout="",
                stderr=(
                    "Custom gate command execution is disabled by default. "
                    "Set AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true in a trusted, isolated environment."
                ),
                exit_code=-1,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            process = await asyncio.create_subprocess_shell(
                gate_config.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(working_dir),
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                hard_cap_note = ""
                if self._gate_hard_timeout_seconds is not None and timeout_seconds == self._gate_hard_timeout_seconds:
                    hard_cap_note = f" (hard cap via AETHER_GATE_HARD_TIMEOUT_SECONDS={self._gate_hard_timeout_seconds:g})"
                return _GateExecutionPayload(
                    status=GateStatus.ERROR,
                    stdout="",
                    stderr=f"Gate timed out after {timeout_seconds:g}s{hard_cap_note}",
                    exit_code=-1,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            status = GateStatus.PASS if process.returncode == 0 else GateStatus.FAIL
            return _GateExecutionPayload(
                status=status,
                stdout=stdout.decode("utf-8", errors="replace")[:50000],
                stderr=stderr.decode("utf-8", errors="replace")[:50000],
                exit_code=process.returncode,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            logger.exception("gate_execution_error", gate_name=gate_config.name, error=str(exc))
            return _GateExecutionPayload(
                status=GateStatus.ERROR,
                stdout="",
                stderr=f"Gate execution error: {str(exc)}",
                exit_code=-1,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    async def _publish_gate_started(
        self,
        *,
        snapshot: Snapshot,
        snapshot_id: str,
        gate_config: GateConfig,
        proposal_id: Optional[str],
    ) -> None:
        await self.event_bus.publish(
            create_gate_event(
                EventType.GATE_STARTED,
                snapshot.repo_id,
                gate_config.name,
                snapshot_id,
                "RUNNING",
                proposal_id=proposal_id,
            )
        )
        logger.info(
            "gate_started",
            gate_name=gate_config.name,
            snapshot_id=snapshot_id,
            command=gate_config.command,
        )

    async def _publish_gate_completed(
        self,
        *,
        snapshot: Snapshot,
        snapshot_id: str,
        gate_config: GateConfig,
        gate_result: GateResult,
        proposal_id: Optional[str],
    ) -> None:
        event_type = (
            EventType.GATE_COMPLETED
            if gate_result.status in (GateStatus.PASS, GateStatus.SKIP)
            else EventType.GATE_FAILED
        )
        await self.event_bus.publish(
            create_gate_event(
                event_type,
                snapshot.repo_id,
                gate_config.name,
                snapshot_id,
                gate_result.status.value,
                proposal_id=proposal_id,
                duration_ms=gate_result.duration_ms,
                exit_code=gate_result.exit_code,
            )
        )
        logger.info(
            "gate_completed",
            gate_name=gate_config.name,
            snapshot_id=snapshot_id,
            status=gate_result.status.value,
            duration_ms=gate_result.duration_ms,
            exit_code=gate_result.exit_code,
        )

    def _new_running_result(
        self,
        *,
        snapshot: Snapshot,
        snapshot_id: str,
        gate_config: GateConfig,
        proposal_id: Optional[str],
    ) -> GateResult:
        return GateResult(
            repo_id=snapshot.repo_id,
            snapshot_id=snapshot_id,
            proposal_id=proposal_id,
            gate_name=gate_config.name,
            gate_command=gate_config.command,
            status=GateStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

    def _apply_execution_payload(
        self,
        gate_result: GateResult,
        payload: _GateExecutionPayload,
    ) -> GateResult:
        gate_result.status = payload.status
        gate_result.stdout = payload.stdout
        gate_result.stderr = payload.stderr
        gate_result.exit_code = payload.exit_code
        gate_result.duration_ms = payload.duration_ms
        gate_result.completed_at = datetime.now(UTC)
        if gate_result.status in (GateStatus.PASS, GateStatus.FAIL):
            gate_result = self._parse_test_output(gate_result)
        return gate_result

    async def run_gate(
        self,
        snapshot_id: str,
        gate_config: GateConfig,
        working_dir: Path,
        proposal_id: Optional[str] = None,
        env_vars: Optional[dict[str, str]] = None,
    ) -> GateResult:
        """
        Run a single gate on a snapshot.

        Args:
            snapshot_id: ID of the snapshot to test
            gate_config: Gate configuration
            working_dir: Directory containing the snapshot files
            proposal_id: Optional proposal ID for tracking
            env_vars: Optional environment variables

        Returns:
            GateResult with status and output
        """
        snapshot = self.session.get(Snapshot, snapshot_id)
        if not snapshot:
            raise ValueError(f"Snapshot not found: {snapshot_id}")

        gate_result = self._new_running_result(
            snapshot=snapshot,
            snapshot_id=snapshot_id,
            gate_config=gate_config,
            proposal_id=proposal_id,
        )
        self.session.add(gate_result)
        self.session.commit()

        await self._publish_gate_started(
            snapshot=snapshot,
            snapshot_id=snapshot_id,
            gate_config=gate_config,
            proposal_id=proposal_id,
        )

        env = self._build_gate_env(
            snapshot_id=snapshot_id,
            gate_name=gate_config.name,
            proposal_id=proposal_id,
            env_vars=env_vars,
        )
        payload = await self._execute_gate_command(gate_config, working_dir, env)
        gate_result = self._apply_execution_payload(gate_result, payload)

        self.session.add(gate_result)
        self.session.commit()

        await self._publish_gate_completed(
            snapshot=snapshot,
            snapshot_id=snapshot_id,
            gate_config=gate_config,
            gate_result=gate_result,
            proposal_id=proposal_id,
        )

        return gate_result

    def _parse_test_output(self, gate_result: GateResult) -> GateResult:
        """Try to parse test metrics from output."""
        if not gate_result.stdout:
            return gate_result

        output = gate_result.stdout

        # Try pytest format: "X passed, Y failed"
        pytest_match = re.search(r"(\d+)\s+passed", output)
        if pytest_match:
            gate_result.tests_passed = int(pytest_match.group(1))

        pytest_fail = re.search(r"(\d+)\s+failed", output)
        if pytest_fail:
            gate_result.tests_failed = int(pytest_fail.group(1))

        # Try jest format: "Tests: X passed, Y failed"
        jest_match = re.search(r"Tests:\s*(\d+)\s+passed", output)
        if jest_match:
            gate_result.tests_passed = int(jest_match.group(1))

        jest_fail = re.search(r"Tests:.*?(\d+)\s+failed", output)
        if jest_fail:
            gate_result.tests_failed = int(jest_fail.group(1))

        # Try to extract coverage
        cov_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:coverage|cov)", output, re.IGNORECASE)
        if cov_match:
            gate_result.coverage_pct = float(cov_match.group(1))

        return gate_result

    async def _run_gates_parallel(
        self,
        *,
        snapshot: Snapshot,
        snapshot_id: str,
        gates: list[GateConfig],
        working_dir: Path,
        proposal_id: Optional[str],
        env_vars: Optional[dict[str, str]],
        max_parallel: int,
    ) -> list[GateResult]:
        """Run a gate list in parallel while batching DB status commits."""
        if not gates:
            return []

        parallel_limit = max(1, max_parallel)
        semaphore = asyncio.Semaphore(parallel_limit)
        gate_results: list[GateResult] = []
        tasks: list[asyncio.Task[_GateExecutionPayload]] = []

        for gate_config in gates:
            gate_result = self._new_running_result(
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                gate_config=gate_config,
                proposal_id=proposal_id,
            )
            self.session.add(gate_result)
            gate_results.append(gate_result)

        self.session.commit()

        for gate_config, gate_result in zip(gates, gate_results):
            await self._publish_gate_started(
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                gate_config=gate_config,
                proposal_id=proposal_id,
            )

            env = self._build_gate_env(
                snapshot_id=snapshot_id,
                gate_name=gate_config.name,
                proposal_id=proposal_id,
                env_vars=env_vars,
            )

            async def _execute(
                *,
                config: GateConfig = gate_config,
                payload_env: dict[str, str] = env,
            ) -> _GateExecutionPayload:
                async with semaphore:
                    return await self._execute_gate_command(config, working_dir, payload_env)

            tasks.append(asyncio.create_task(_execute()))

        payloads = await asyncio.gather(*tasks, return_exceptions=False)

        for gate_result, payload in zip(gate_results, payloads):
            gate_result = self._apply_execution_payload(gate_result, payload)
            self.session.add(gate_result)

        self.session.commit()

        for gate_config, gate_result in zip(gates, gate_results):
            await self._publish_gate_completed(
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                gate_config=gate_config,
                gate_result=gate_result,
                proposal_id=proposal_id,
            )

        return gate_results

    async def run_all_gates(
        self,
        snapshot_id: str,
        gates: list[GateConfig],
        working_dir: Path,
        proposal_id: Optional[str] = None,
        stop_on_failure: bool = True,
        env_vars: Optional[dict[str, str]] = None,
        execution_mode: str = "sequential",
        max_parallel: int = 4,
    ) -> list[GateResult]:
        """
        Run all configured gates on a snapshot.

        Args:
            snapshot_id: ID of the snapshot
            gates: List of gate configurations
            working_dir: Directory with snapshot files
            proposal_id: Optional proposal ID
            stop_on_failure: Stop running gates if a required gate fails
            env_vars: Optional environment variables for gate commands
            execution_mode: `sequential`, `fast_fail`, or `parallel`
            max_parallel: Max parallel gate commands when mode allows parallelism

        Returns:
            List of gate results
        """
        mode = execution_mode.strip().lower()
        if mode not in {"sequential", "fast_fail", "parallel"}:
            raise ValueError(f"Unsupported gate execution mode: {execution_mode}")

        sorted_gates = sorted(gates, key=lambda g: g.order)
        if not sorted_gates:
            return []

        snapshot = self.session.get(Snapshot, snapshot_id)
        if not snapshot:
            raise ValueError(f"Snapshot not found: {snapshot_id}")

        if mode == "parallel":
            return await self._run_gates_parallel(
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                gates=sorted_gates,
                working_dir=working_dir,
                proposal_id=proposal_id,
                env_vars=env_vars,
                max_parallel=max_parallel,
            )

        if mode == "fast_fail":
            results: list[GateResult] = []
            required = [gate for gate in sorted_gates if gate.required]
            optional = [gate for gate in sorted_gates if not gate.required]

            for gate_config in required:
                result = await self.run_gate(
                    snapshot_id,
                    gate_config,
                    working_dir,
                    proposal_id=proposal_id,
                    env_vars=env_vars,
                )
                results.append(result)
                if stop_on_failure and result.status not in (GateStatus.PASS, GateStatus.SKIP):
                    logger.info(
                        "gate_sequence_stopped",
                        reason="required_gate_failed",
                        failed_gate=gate_config.name,
                    )
                    return results

            optional_results = await self._run_gates_parallel(
                snapshot=snapshot,
                snapshot_id=snapshot_id,
                gates=optional,
                working_dir=working_dir,
                proposal_id=proposal_id,
                env_vars=env_vars,
                max_parallel=max_parallel,
            )
            results.extend(optional_results)
            return results

        results = []
        for gate_config in sorted_gates:
            result = await self.run_gate(
                snapshot_id,
                gate_config,
                working_dir,
                proposal_id=proposal_id,
                env_vars=env_vars,
            )
            results.append(result)
            if stop_on_failure and gate_config.required:
                if result.status not in (GateStatus.PASS, GateStatus.SKIP):
                    logger.info(
                        "gate_sequence_stopped",
                        reason="required_gate_failed",
                        failed_gate=gate_config.name,
                    )
                    break

        return results

    def get_gate_result(self, gate_result_id: str) -> Optional[GateResult]:
        """Get a gate result by ID."""
        return self.session.get(GateResult, gate_result_id)

    def get_results_for_snapshot(self, snapshot_id: str) -> list[GateResult]:
        """Get all gate results for a snapshot."""
        statement = (
            select(GateResult)
            .where(GateResult.snapshot_id == snapshot_id)
            .order_by(GateResult.started_at)
        )
        return list(self.session.exec(statement))

    def get_latest_results(
        self,
        repo_id: str,
        gate_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[GateResult]:
        """Get latest gate results for a repository."""
        statement = select(GateResult).where(GateResult.repo_id == repo_id)

        if gate_name:
            statement = statement.where(GateResult.gate_name == gate_name)

        statement = statement.order_by(GateResult.completed_at.desc()).limit(limit)
        return list(self.session.exec(statement))

    def compute_evidence(self, results: list[GateResult]) -> dict[str, str]:
        """
        Compute evidence summary from gate results.

        Returns dict of {gate_name: status}, plus `_verification_mode` ("SIMULATED"
        or "EXECUTED") when gate commands are recorded on the results.
        """
        evidence: dict[str, str] = {}
        commands_present = [r.gate_command for r in results if getattr(r, "gate_command", "")]
        for result in results:
            evidence[result.gate_name] = result.status.value
        if commands_present:
            all_simulated = all(is_safe_simulation_command(cmd) for cmd in commands_present)
            evidence["_verification_mode"] = "SIMULATED" if all_simulated else "EXECUTED"
        return evidence

    def all_required_gates_pass(
        self,
        results: list[GateResult],
        gates: list[GateConfig],
    ) -> bool:
        """Check if all required gates passed."""
        required_gates = {g.name for g in gates if g.required}
        result_map = {r.gate_name: r.status for r in results}

        for gate_name in required_gates:
            if gate_name not in result_map:
                return False
            if result_map[gate_name] not in (GateStatus.PASS, GateStatus.SKIP):
                return False

        return True

    def build_semantic_merge_gates(
        self,
        changed_paths: list[str],
        repo_root: Path,
    ) -> tuple[list[GateConfig], ManifestLoadResult]:
        """
        Resolve test/lint/typecheck targets from manifest, build tripartite gate list.
        Returns (gates, manifest_result).
        """
        manifest_result = load_gate_manifest(repo_root, changed_paths)

        if manifest_result.selection_source == "fail_closed":
            # Guardrail 2 & Rule 3: Fail closed directly
            gate = GateConfig(
                name="fail_closed",
                command=f"FAIL_CLOSED: {manifest_result.error or 'unknown_error'}",
                required=True,
                timeout_seconds=5,
                order=0,
            )
            return [gate], manifest_result

        # Parser gate: Guardrail 1 - repo-controlled python helper using ast.parse (no global tree-sitter CLI binary)
        # We quote the python snippet and execute it via sys.executable
        python_snippet = (
            "import ast\n"
            "import sys\n"
            f"files = {changed_paths!r}\n"
            "has_err = False\n"
            "for f in files:\n"
            "    if f.endswith('.py'):\n"
            "        try:\n"
            "            ast.parse(open(f, 'rb').read())\n"
            "        except Exception as e:\n"
            "            print(f'Parser fail: {f} - {e}', file=sys.stderr)\n"
            "            has_err = True\n"
            "if has_err:\n"
            "    sys.exit(1)\n"
        )
        # Escape the snippet for command line execution
        parser_cmd = f"{sys.executable} -c {shlex.quote(python_snippet)}"

        # Static gate: ruff check + mypy --no-error-summary on manifest-selected targets
        # Quoted targets
        lint_args = " ".join(shlex.quote(t) for t in manifest_result.lint_targets)
        typecheck_args = " ".join(shlex.quote(t) for t in manifest_result.typecheck_targets)
        static_cmd = f"{sys.executable} -m ruff check {lint_args} && {sys.executable} -m mypy --no-error-summary {typecheck_args}"

        # Impacted baseline gate: pytest on manifest-selected test targets
        test_args = " ".join(shlex.quote(t) for t in manifest_result.test_targets)
        test_cmd = f"{sys.executable} -m pytest {test_args} -x --tb=short"

        gates = [
            GateConfig(
                name=TripartiteGateLayer.PARSER.value,
                command=parser_cmd,
                required=True,
                timeout_seconds=30,
                order=0,
            ),
            GateConfig(
                name=TripartiteGateLayer.STATIC.value,
                command=static_cmd,
                required=True,
                timeout_seconds=60,
                order=1,
            ),
            GateConfig(
                name=TripartiteGateLayer.IMPACTED_BASELINE.value,
                command=test_cmd,
                required=True,
                timeout_seconds=300,
                order=2,
            ),
        ]
        return gates, manifest_result


class TripartiteGateLayer(str, Enum):
    PARSER = "parser"
    STATIC = "static"
    IMPACTED_BASELINE = "impacted_baseline"

