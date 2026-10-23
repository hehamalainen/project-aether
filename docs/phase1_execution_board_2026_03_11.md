# Phase 1 Execution Board - 2026-03-11

## Objective

Convert the current passing Phase 1 swarm baseline into a clean, repeatable, reviewable operating baseline.

This board assumes:
- MX Swarm and Aether stay separate products for now;
- Aether remains the repository convergence and control plane;
- the current 1000-agent strict baseline is real, but still needs clean-baseline and repeatability confirmation.

## Current state

Proven now:
- snapshot reproducer passes beyond the old recursion failure zone;
- 25m strict passes;
- 60m strict passes;
- `15m + 2x60m` night suite passes.

Primary baseline reference:
- `docs/phase1_baseline_freeze_2026_03_11.md`

Primary peer-review package:
- `docs/peer_review_package_phase1_session_diagnostics_2026_03_08.md`

## Frozen baseline

Controller / intake:
- ratio `0.43`
- intake high `165`
- intake low `125`

Strict profile:
- `1000` agents
- `500` files
- `contention=low`
- `inflight=200`
- `promotion-batch=30`
- `promotion-non-conflicting-batches=12`
- `gate-max-parallel=4`

Passing runtime artifact:
- `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

Passing night suite summary:
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-summary.txt`

## Workstream 1 - Clean baseline packaging

### Goal
Move from dirty-worktree success to clean reproducible success.

### Scope
- review and package the snapshot-fix delta;
- keep the change set focused on:
  - `packages/aether/services/snapshot_service.py`
  - `packages/aether/services/repository_service.py`
  - `packages/aether/services/snapshot_reproducer.py`
  - `packages/aether/services/swarm_harness.py`
  - `apps/aether_cli/main.py`
  - `tests/aether/test_services.py`
  - `tests/aether/test_swarm_harness.py`

### Gates
- focused tests pass;
- reproducer passes;
- 25m strict passes on clean commit;
- 60m strict passes on clean commit.

### Stop conditions
- `create_snapshot_quarantines > 0`
- reproducer regresses
- clean-commit baseline fails while current worktree baseline passes

## Workstream 2 - Repeatability matrix

### Goal
Prove the new baseline is stable across seeds.

### Runs
1. 25m strict, seed `2027`
2. 60m strict, seed `2027`
3. 60m strict, seed `2028`
4. 60m strict, seed `2029`

### Gates
For every run:
- all strict checks pass
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`
- no late-window degradation wave

### Stop conditions
- any quarantine reappears
- reproducer stays green but runtime fails in the same snapshot path
- submit p95 or rho regress materially without any code change

## Workstream 3 - Controlled stress

### Goal
Measure headroom without changing the baseline design.

### Runs
5. 60m strict, seed `2026`, `contention=medium`
6. 4h soak on the locked baseline

### Gates
- baseline behavior remains bounded
- no snapshot quarantine wave
- backlog remains bounded
- service rate does not collapse late

### Stop conditions
- snapshot-path regressions
- new long-window degradation not explained by contention increase

## Workstream 4 - Operator surface

### Goal
Turn the current internal diagnostics into operator-grade controls.

### Deliverables
- quarantine breakdown surfaced cleanly in dashboard/control plane
- snapshot reproducer documented as a formal release gate
- phase-1 runbook for:
  - reproducer
  - 25m strict
  - 60m strict
  - 15m + 2x60m suite

## Workstream 5 - Post-baseline throughput headroom

### Goal
Only after repeatability is proven, move from "passes" to "has margin."

### Candidate targets
- promotion throughput ceiling
- gate execution overhead
- route / DB write contention
- higher-contention swarm profiles

### Rule
Do not reopen admission-control redesign unless new evidence proves it is the bottleneck again.

## Artifact index

### Reproducer
- `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v4.json`
- `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v5.json`

### Passing diagnostics
- `.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v20-snapshot-fix-20260310-2205.json`
- `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

### Passing suite
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-summary.txt`
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass15.json`
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass60a.json`
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass60b.json`

## Decision rule

If Workstreams 1 and 2 pass:
- freeze the baseline as the current Phase 1 operating standard;
- package the snapshot fix as a focused reliability milestone;
- move to controlled-stress and headroom work.

If Workstream 1 or 2 fails:
- do not redesign the system broadly;
- classify the failure against reproducer + quarantine counters first;
- treat snapshot-path reappearance as a direct regression.
