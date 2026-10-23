# OtterBot Threat Model (v1)

## Security Invariants

1. Untrusted content never directly authorizes privileged actions.
2. Deterministic policy gates execute before tool execution.
3. Tools run through a broker with quota and isolation controls.
4. Session memory cannot be read across session boundaries.
5. Every action emits an auditable event.
6. Skills must pass signed admission controls.

## Must-Never-Happen Events

- Cross-session memory read succeeds.
- External egress to non-allowlisted destinations succeeds.
- A replayed token executes a second action.
- Unsigned or tampered skill is admitted.
- Path traversal or container escape payload reaches execution.

## Abuse Cases Addressed in Tests

- Prompt injection causing privileged integration write
- Data exfiltration through network destinations
- Token replay and signature forgery
- Filesystem traversal escape payloads
- Skill supply-chain tampering
