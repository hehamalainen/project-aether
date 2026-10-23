"""Secure OtterBot runtime orchestration."""

from __future__ import annotations

import uuid

from .audit import AuditLogger
from .auth import TokenValidator
from .contracts import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    Decision,
    EgressPolicy,
    Outcome,
    SessionPolicy,
)
from .exceptions import AuthError, QuotaExceededError, ToolExecutionError
from .otterguard import OtterGuard
from .policy_engine import DeterministicPolicyEngine
from .tool_broker import ToolBroker, ToolBrokerConfig
from .tools import default_tool_manifests
from .contracts import utc_now


class OtterBotRuntime:
    """Coordinates auth, deterministic policy, guard scoring, and tool execution."""

    def __init__(
        self,
        token_validator: TokenValidator,
        policy_engine: DeterministicPolicyEngine,
        otterguard: OtterGuard,
        tool_broker: ToolBroker,
        audit_logger: AuditLogger,
    ):
        self.token_validator = token_validator
        self.policy_engine = policy_engine
        self.otterguard = otterguard
        self.tool_broker = tool_broker
        self.audit_logger = audit_logger
        self.session_policies: dict[str, SessionPolicy] = {}

    @classmethod
    def with_defaults(cls, signing_key: str = "otterbot-dev") -> "OtterBotRuntime":
        validator = TokenValidator(signing_key=signing_key)
        policy_engine = DeterministicPolicyEngine()
        guard = OtterGuard()
        broker = ToolBroker(ToolBrokerConfig(warm_workers_per_tool=1, max_executions_per_minute=30))
        for manifest in default_tool_manifests():
            broker.register_tool(manifest)
        audit = AuditLogger()
        return cls(validator, policy_engine, guard, broker, audit)

    def register_session_policy(self, session_id: str, policy: SessionPolicy) -> None:
        self.session_policies[session_id] = policy

    def default_session_policy(self) -> SessionPolicy:
        return SessionPolicy(
            allowed_tools=[manifest.tool_id for manifest in default_tool_manifests()],
            data_boundaries=["workspace/"],
            retention_days=30,
            approval_threshold=0.75,
            allowed_scopes=[
                "docs:read",
                "docs:write",
                "code:read",
                "network:read",
                "integrations:write",
                "memory:write",
                "memory:read",
            ],
            egress_policy=EgressPolicy(allowed_domains=["api.mycorp.com"], allow_private_networks=False),
            require_human_approval_for=["sensitive"],
        )

    def handle_action(
        self,
        request: ActionRequest,
        token: str,
        tool_id: str,
        human_approval: bool = False,
    ) -> ActionResult:
        action_id = str(uuid.uuid4())
        policy = self.session_policies.get(request.session_id, self.default_session_policy())

        try:
            auth_context = self.token_validator.validate(token)
        except AuthError as exc:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.BLOCKED,
                reason_codes=[exc.code],
            )
            self._audit(action_id, request, tool_id, result)
            return result

        manifest = self.tool_broker.get_manifest(tool_id)
        if manifest is None:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.BLOCKED,
                reason_codes=["TOOL_NOT_FOUND"],
            )
            self._audit(action_id, request, tool_id, result)
            return result

        guard_assessment = self.otterguard.assess(request, manifest)
        decision = self.policy_engine.evaluate(
            request=request,
            tool=manifest,
            session_policy=policy,
            auth_context=auth_context,
            guard_assessment=guard_assessment,
        )

        if decision.decision == Decision.DENY:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.BLOCKED,
                reason_codes=decision.reason_codes,
                payload={"guard": guard_assessment.explanation},
            )
            self._audit(action_id, request, tool_id, result)
            return result

        if decision.decision == Decision.REQUIRE_APPROVAL and not human_approval:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.BLOCKED,
                reason_codes=decision.reason_codes,
                required_approval=True,
                payload={"guard": guard_assessment.explanation},
            )
            self._audit(action_id, request, tool_id, result)
            return result

        envelope = self.tool_broker.build_execution_envelope(
            request=request,
            tool_id=tool_id,
            capability_token=token,
        )

        try:
            execution_result = self.tool_broker.execute(envelope)
        except QuotaExceededError:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.BLOCKED,
                reason_codes=["SESSION_EXECUTION_QUOTA_EXCEEDED"],
            )
            self._audit(action_id, request, tool_id, result)
            return result
        except ToolExecutionError:
            result = ActionResult(
                action_id=action_id,
                decision=Decision.DENY,
                outcome=Outcome.FAILED,
                reason_codes=["TOOL_EXECUTION_FAILED"],
            )
            self._audit(action_id, request, tool_id, result)
            return result

        result = ActionResult(
            action_id=action_id,
            decision=Decision.ALLOW,
            outcome=Outcome.SUCCESS,
            reason_codes=[],
            payload=execution_result.output,
        )
        self._audit(action_id, request, tool_id, result)
        return result

    def _audit(self, action_id: str, request: ActionRequest, tool_id: str, result: ActionResult) -> None:
        self.audit_logger.log(
            AuditEvent(
                ts=utc_now(),
                session_id=request.session_id,
                action_id=action_id,
                intent=request.requested_capability,
                tool=tool_id,
                policy_decision=result.decision.value,
                outcome=result.outcome.value,
                reason_codes=result.reason_codes,
            )
        )
