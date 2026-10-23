"""Tool broker with execution quotas and worker pool metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .contracts import ActionRequest, ExecutionEnvelope, ToolExecutionResult, ToolManifest
from .exceptions import QuotaExceededError, ToolExecutionError


ToolHandler = Callable[[ExecutionEnvelope], dict[str, object]]


@dataclass
class ToolBrokerConfig:
    """Runtime controls for pool and quota behavior."""

    warm_workers_per_tool: int = 1
    max_executions_per_minute: int = 30


@dataclass
class ToolBrokerMetrics:
    """Operational counters for cold starts and quota pressure."""

    warm_hits: int = 0
    cold_starts: int = 0
    quota_denials: int = 0


class ToolBroker:
    """Dispatches approved tool actions into isolated execution handlers."""

    def __init__(self, config: ToolBrokerConfig | None = None):
        self._config = config or ToolBrokerConfig()
        self._manifests: dict[str, ToolManifest] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._warm_pool: dict[str, int] = {}
        self._session_window: dict[tuple[str, int], int] = {}
        self.metrics = ToolBrokerMetrics()

    def register_tool(self, manifest: ToolManifest, handler: ToolHandler | None = None) -> None:
        self._manifests[manifest.tool_id] = manifest
        self._handlers[manifest.tool_id] = handler or self._default_handler
        self._warm_pool[manifest.tool_id] = self._config.warm_workers_per_tool

    def get_manifest(self, tool_id: str) -> ToolManifest | None:
        return self._manifests.get(tool_id)

    def build_execution_envelope(
        self,
        request: ActionRequest,
        tool_id: str,
        capability_token: str,
        timeout_ms: int = 5000,
    ) -> ExecutionEnvelope:
        return ExecutionEnvelope(
            tool_id=tool_id,
            input_ref=request.target,
            capability_token=capability_token,
            timeout_ms=timeout_ms,
            resource_limits={"cpu": "0.5", "memory": "512Mi"},
            session_id=request.session_id,
        )

    def execute(self, envelope: ExecutionEnvelope) -> ToolExecutionResult:
        self._enforce_quota(envelope.session_id)
        self._acquire_worker(envelope.tool_id)

        handler = self._handlers.get(envelope.tool_id)
        if handler is None:
            raise ToolExecutionError(f"No handler registered for tool: {envelope.tool_id}")

        output = handler(envelope)
        return ToolExecutionResult(
            tool_id=envelope.tool_id,
            session_id=envelope.session_id,
            output=output,
        )

    def _enforce_quota(self, session_id: str) -> None:
        epoch_minute = int(datetime.now(timezone.utc).timestamp()) // 60
        key = (session_id, epoch_minute)
        count = self._session_window.get(key, 0)
        if count >= self._config.max_executions_per_minute:
            self.metrics.quota_denials += 1
            raise QuotaExceededError("Per-session execution quota exceeded")
        self._session_window[key] = count + 1

    def _acquire_worker(self, tool_id: str) -> None:
        warm_count = self._warm_pool.get(tool_id, 0)
        if warm_count > 0:
            self.metrics.warm_hits += 1
            self._warm_pool[tool_id] = warm_count - 1
            self._warm_pool[tool_id] += 1
            return

        self.metrics.cold_starts += 1
        self._warm_pool[tool_id] = 1

    def _default_handler(self, envelope: ExecutionEnvelope) -> dict[str, object]:
        return {
            "status": "simulated",
            "tool_id": envelope.tool_id,
            "input_ref": envelope.input_ref,
        }
