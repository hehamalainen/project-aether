# OtterBot Build Roadmap (Realistic)

## Delivery Frame

- Secure core target: 6-10 weeks for one engineer.
- Enterprise hardening and compliance: additional phase after secure core.

## Phases

1. Week 1: Threat model and security invariants
2. Weeks 2-4: Auth, policy gate, tool broker, immutable config model, audit schema
3. Weeks 5-6: OtterGuard secondary scoring and approval gates
4. Weeks 7-8: Teams + Slack surface, core tools, isolated memory, trust dashboard baseline
5. Weeks 9-10: Hardening gate (red-team scenarios and incident playbooks)

## No-Go Gate

If deterministic policy tests fail to block high-risk tool calls in red-team scenarios, feature expansion pauses until controls are corrected.
