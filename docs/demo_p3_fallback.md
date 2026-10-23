# Demo P3 Fallback Readiness

Objective: prepare deterministic fallback assets for demo continuity if live flow is noisy.

## Command

- `./scripts/run_demo_p3_gate.sh`

## What the gate produces

- Stable known-good P1 summary:
  - `.pytest-artifacts/demo/p3-known-good.json`
- Dashboard state snapshot (JSON + HTML):
  - `.pytest-artifacts/demo/p3-dashboard-state-<timestamp>.json`
  - `.pytest-artifacts/demo/p3-dashboard-state-<timestamp>.html`
- Stable dashboard state pointer:
  - `.pytest-artifacts/demo/p3-dashboard-state.json`
- Gate report:
  - `.pytest-artifacts/demo/p3-fallback-<timestamp>.json`

## Required pass checks

- stack healthy
- latest passing P1 artifact exists
- known-good JSON pinned
- backend health and physics endpoint reachable
- UI reachable
- dashboard fallback state captured

## Demo usage pattern

If live demo degrades:

1. Show `p3-known-good.json` (last verified flow evidence).
2. Show `p3-dashboard-state.json` for stable status/physics snapshot.
3. Continue narrative with architecture/runbook rather than live mutation.
