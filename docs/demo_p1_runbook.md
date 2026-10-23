# Demo P1 Runbook (5-7 min)

Objective: demonstrate one complete Aether loop from ingest to canonical head update with live telemetry.

## Preflight (60-90s)

1. Start or recover stack:
   - `./scripts/demo_stack.sh start`
2. Ensure auto-recovery stays active in a separate terminal:
   - `./scripts/demo_keepalive.sh`
3. Confirm health:
   - `./scripts/demo_stack.sh status`

Pass gate: backend and UI both `health: ok`.

## Live P1 Gate (60-120s)

Run:

- `./scripts/run_demo_p1_gate.sh`

Pass gate:

- `repo_created=true`
- `proposal_submitted=true`
- `promotion_success=true`
- `head_advanced=true`
- `physics_visible=true`

Artifacts:

- JSON: `.pytest-artifacts/demo/p1-flow-<timestamp>.json`
- Log: `.pytest-artifacts/demo/p1-flow-<timestamp>.log`

## Demo Talk Track (3-4 min)

1. **Repo Create**: "Aether creates a fresh repo context with gate policy."
2. **Proposal Intake**: "An agent submits a code change (`full_file`) and receives an ID immediately."
3. **Promotion**: "Promotion evaluates and advances canonical head when evidence is sufficient."
4. **Head Check**: "Head sequence increments, proving canonical state progression."
5. **Physics View**: "We inspect `lambda`, `mu`, backlog, and fallback-rate from `/v1/system/physics`."

Keep one terminal showing the latest P1 log and one browser tab on dashboard.

## Backup Plan (if live run is noisy)

Use last known-good P1 artifact JSON and explain that it is from the same gate command:

- `.pytest-artifacts/demo/p1-flow-<timestamp>.json`

Then continue the conceptual walkthrough in dashboard without rerunning heavy actions.
