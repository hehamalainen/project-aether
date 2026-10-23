# Clean Baseline Execution Checklist - 2026-03-12

## Purpose

Convert the current dirty-worktree passing baseline into a clean reproducible baseline.

This checklist is intentionally narrow. Do not mix new tuning or architecture work into it.

## Scope

Primary code paths:
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/repository_service.py`
- `packages/aether/services/snapshot_reproducer.py`

Supporting runtime/tests:
- `packages/aether/services/swarm_harness.py`
- `apps/aether_cli/main.py`
- `tests/aether/test_services.py`
- `tests/aether/test_swarm_harness.py`

## Step 1 - package the snapshot fix cleanly

- isolate the snapshot-fix delta from unrelated worktree changes
- review for accidental scope growth
- ensure docs point to the frozen baseline and 4h soak result

Go gate:
- only snapshot/reproducer/harness/test/docs changes are in scope

## Step 2 - focused validation

Run:
- `python -m pytest tests/aether/test_services.py tests/aether/test_swarm_harness.py -q`

Go gate:
- all focused tests pass

## Step 3 - reproducer validation

Run the reproducer on the packaged code.

Go gates:
- reproducer passes
- no recursion failure
- survives beyond old failure zone

Reference artifact:
- `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v5.json`

## Step 4 - clean 25m strict

Run the locked baseline profile with a fresh artifact path.

Go gates:
- strict pass
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`
- `backlog_end = 0`

Reference artifact:
- `.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v20-snapshot-fix-20260310-2205.json`

## Step 5 - clean 60m strict

Run the same locked baseline profile unchanged.

Go gates:
- strict pass
- no late degradation wave
- no snapshot/materialization quarantines

Reference artifact:
- `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

## Step 6 - repeatability

Run:
- 25m strict seed `2027`
- 60m strict seed `2027`
- 60m strict seed `2028`
- 60m strict seed `2029`

Go gates:
- all pass
- no quarantine reappearance

## Step 7 - controlled stress

Run after repeatability only:
- 60m strict with `contention=medium`

Already complete:
- 4h soak on locked baseline passed:
  - `.pytest-artifacts/phase1-suite/soak-4h-snapshot-fix-20260311.json`

## Stop conditions

Stop and classify before any more tuning if:
- reproducer regresses
- `create_snapshot_quarantines > 0`
- `materialize_gate_workspace_quarantines > 0`
- clean commit fails while dirty baseline passed

## Rule

Do not change:
- ratio
- watermarks
- inflight
- promotion batch settings
- gate mode

until the clean baseline and repeatability gates are complete.
