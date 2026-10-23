"""Deterministic policy enforcement for OtterBot actions."""

from __future__ import annotations

from .contracts import (
    ActionRequest,
    Decision,
    OtterGuardAssessment,
    PolicyDecision,
    SessionPolicy,
    ToolManifest,
)
from .auth import AuthContext


class DeterministicPolicyEngine:
    """Primary authorization boundary. OtterGuard is secondary only."""

    _SUPPORTED_CHANNELS = {"teams", "slack"}
    _SUSPICIOUS_COMMAND_MARKERS = ("curl ", "wget ", "bash -c", "nc ", "python -c")

    def evaluate(
        self,
        request: ActionRequest,
        tool: ToolManifest,
        session_policy: SessionPolicy,
        auth_context: AuthContext,
        guard_assessment: OtterGuardAssessment | None = None,
    ) -> PolicyDecision:
        if request.channel.lower() not in self._SUPPORTED_CHANNELS:
            return self._deny("UNSUPPORTED_CHANNEL", session_policy)

        if tool.kill_switch:
            return self._deny("TOOL_KILL_SWITCH_ACTIVE", session_policy)

        if request.user_id != auth_context.user_id or request.session_id != auth_context.session_id:
            return self._deny("TOKEN_SUBJECT_MISMATCH", session_policy)

        if tool.tool_id not in session_policy.allowed_tools:
            return self._deny("TOOL_NOT_ALLOWED", session_policy)

        if not set(tool.required_permissions).issubset(set(session_policy.allowed_scopes)):
            return self._deny("PERMISSION_SCOPE_MISMATCH", session_policy)

        if not set(tool.required_permissions).issubset(set(auth_context.scopes)):
            return self._deny("TOKEN_SCOPE_MISMATCH", session_policy)

        if self._has_path_traversal(request.target) or self._args_have_path_traversal(request.args):
            return self._deny("PATH_TRAVERSAL", session_policy)

        if not self._within_data_boundaries(request.target, session_policy.data_boundaries):
            return self._deny("DATA_BOUNDARY_VIOLATION", session_policy)

        if self._contains_suspicious_command(request):
            return self._deny("SUSPICIOUS_COMMAND", session_policy)

        if self._is_egress_denied(tool, session_policy):
            return self._deny("EGRESS_DENIED", session_policy)

        if self._capability_mismatch(request, tool):
            return self._deny("CAPABILITY_SCOPE_MISMATCH", session_policy)

        if tool.requires_approval or tool.data_classification in session_policy.require_human_approval_for:
            return PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reason_codes=["HUMAN_APPROVAL_REQUIRED"],
                required_approval=True,
                allowed_scopes=list(session_policy.allowed_scopes),
                egress_policy=session_policy.egress_policy,
            )

        if (
            guard_assessment is not None
            and guard_assessment.risk_score >= session_policy.approval_threshold
        ):
            return PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reason_codes=["OTTERGUARD_HIGH_RISK", *guard_assessment.reason_codes],
                required_approval=True,
                allowed_scopes=list(session_policy.allowed_scopes),
                egress_policy=session_policy.egress_policy,
            )

        return PolicyDecision(
            decision=Decision.ALLOW,
            reason_codes=[],
            required_approval=False,
            allowed_scopes=list(session_policy.allowed_scopes),
            egress_policy=session_policy.egress_policy,
        )

    def _deny(self, code: str, session_policy: SessionPolicy) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.DENY,
            reason_codes=[code],
            required_approval=False,
            allowed_scopes=list(session_policy.allowed_scopes),
            egress_policy=session_policy.egress_policy,
        )

    def _has_path_traversal(self, value: str) -> bool:
        target = value.replace("\\", "/")
        if "../" in target:
            return True
        if target.startswith("/"):
            return True
        return False

    def _within_data_boundaries(self, target: str, boundaries: list[str]) -> bool:
        lowered = target.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return True
        return any(target.startswith(boundary) for boundary in boundaries)

    def _args_have_path_traversal(self, args: dict[str, object]) -> bool:
        for value in args.values():
            if isinstance(value, str) and self._has_path_traversal(value):
                return True
        return False

    def _contains_suspicious_command(self, request: ActionRequest) -> bool:
        joined = " ".join([request.target, *[str(value) for value in request.args.values()]]).lower()
        return any(marker in joined for marker in self._SUSPICIOUS_COMMAND_MARKERS)

    def _is_egress_denied(self, tool: ToolManifest, session_policy: SessionPolicy) -> bool:
        allowed = set(session_policy.egress_policy.allowed_domains)
        for destination in tool.network_destinations:
            normalized = destination.strip().lower()
            if not normalized or normalized == "none":
                continue
            if normalized not in allowed:
                return True
        return False

    def _capability_mismatch(self, request: ActionRequest, tool: ToolManifest) -> bool:
        capability = request.requested_capability.lower()
        read_like = capability.startswith("summar") or capability.startswith("read")
        write_perm = any(perm.endswith(":write") for perm in tool.required_permissions)
        if read_like and write_perm:
            return True
        return False
