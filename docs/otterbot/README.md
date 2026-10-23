# OtterBot v1 Secure Core

This folder captures the implemented clean-room OtterBot foundation:

- Deterministic policy engine is the primary enforcement boundary.
- OtterGuard provides secondary risk scoring and explanations.
- Tool broker implements per-session quotas and warm-worker strategy.
- Session memory is isolated by session boundary.
- Skill admission enforces signature, provenance, runtime isolation, and kill switch.

## Scope Implemented

- Channels: Teams, Slack (minimal adapter stubs)
- Tools: 7 built-in manifests
- Runtime contracts:
  - `ActionRequest`
  - `PolicyDecision`
  - `ToolManifest`
  - `ExecutionEnvelope`
  - `AuditEvent`
  - `SessionPolicy`

## Test Coverage

`tests/otterbot/` covers:

- Indirect prompt injection blocking
- Egress exfiltration blocking
- High-risk action approval gate
- Token replay and forgery blocking
- Path traversal blocking
- Session memory isolation
- Skill admission hard-gates
- Tool execution quotas and worker metrics
