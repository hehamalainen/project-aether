"""Secondary risk scoring and explanation layer for OtterBot actions."""

from __future__ import annotations

from .contracts import ActionRequest, OtterGuardAssessment, ToolManifest


class OtterGuard:
    """Heuristic intent/action scorer used as secondary signal."""

    _INJECTION_MARKERS = (
        "ignore previous",
        "system prompt",
        "override policy",
        "create telegram",
        "install backdoor",
        "exfiltrate",
    )

    def assess(self, request: ActionRequest, tool: ToolManifest) -> OtterGuardAssessment:
        score = max(0.0, min(1.0, request.risk_hint))
        reasons: list[str] = []

        capability = request.requested_capability.lower()
        read_like = capability.startswith("summar") or capability.startswith("read")
        write_perm = any(perm.endswith(":write") for perm in tool.required_permissions)

        if read_like and write_perm:
            score += 0.6
            reasons.append("INTENT_ACTION_MISMATCH")

        joined_args = " ".join(str(value).lower() for value in request.args.values())
        if any(marker in joined_args for marker in self._INJECTION_MARKERS):
            score += 0.4
            reasons.append("PROMPT_INJECTION_MARKER")

        if "http://" in request.target.lower() or "https://" in request.target.lower():
            if write_perm:
                score += 0.2
                reasons.append("UNTRUSTED_REMOTE_CONTENT")

        score = max(0.0, min(1.0, score))

        explanation = "Low risk"
        if score >= 0.7:
            explanation = "High-risk action pattern detected by OtterGuard"
        elif score >= 0.4:
            explanation = "Moderate risk; review recommended"

        return OtterGuardAssessment(
            risk_score=score,
            reason_codes=reasons,
            explanation=explanation,
        )
