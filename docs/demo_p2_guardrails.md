# Demo P2 Guardrails (Risk Freeze)

Objective: prevent late, high-risk architecture changes before demo while preserving uptime and observability fixes.

## Command

- `./scripts/run_demo_p2_gate.sh`

This gate validates:

1. Stack health (`./scripts/demo_stack.sh status`)
2. Risk freeze policy (`./scripts/check_demo_risk_freeze.sh`)
3. Research alignment check (`./scripts/check_research_alignment.sh`)
4. Latest P1 rehearsal artifact exists and is passing
5. Keepalive process is running (`pgrep -af scripts/demo_keepalive.sh`)

On first run, the gate initializes a freeze baseline at:

- `.run/demo_risk_freeze_baseline.txt`

Subsequent runs enforce the freeze against *new* changes beyond that baseline.

## Risk Freeze Policy

Blocked by default (unless explicitly overridden via `AETHER_DEMO_FREEZE_ALLOW_PATTERNS`):

- `packages/aether/services/promotion_engine.py`
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/proposal_service.py`
- `packages/aether/models/entities.py`
- `packages/aether/crdt/*`

Rationale: these are high-blast-radius paths for promotion correctness, snapshot determinism, and schema behavior.

## Optional Overrides

- Allow specific blocked paths temporarily:
  - `AETHER_DEMO_FREEZE_ALLOW_PATTERNS="packages/aether/services/proposal_service.py"`
- Cap changed file count (default 30):
  - `AETHER_DEMO_MAX_CHANGED_FILES=20`

Use overrides only for urgent demo reliability fixes and remove after demo.
