# Phase 1 Baseline Freeze - Snapshot Fix

Date: 2026-03-11

## Executive summary

Phase 1 is now in a materially different state than a few days ago.

The core result:
- the long-run promotion-capacity degradation was traced to recursive delta-chain resolution in the snapshot path;
- that recursion was removed and replaced with iterative resolution;
- the dedicated reproducer now survives beyond the old failure point;
- the locked 1000-agent strict profile now passes 25m, 60m, the full `15m + 2x60m` night suite, a 4-hour soak, and a 10-hour soak.

This document freezes the current working baseline so it can be shared, reviewed, and repeated.

Important reproducibility note:
- passing runtime artifacts were produced on historical pre-release git commit `e5643a9ff418` (not a reachable object in the single-root public release repository);
- harness metadata reported `git_dirty=true`;
- so this is a **historical worktree baseline** from pre-release development.

## Root cause summary

Observed failure mode before the fix:
- late-window promotion throughput collapse during 25m/60m swarm runs;
- recurring `create_snapshot` quarantines;
- exact exception shape:
  - `RecursionError: maximum recursion depth exceeded`
  - source tag:
    - `annotation.py:_gen_annotations_cache_key:97`

The decisive finding came from the dedicated snapshot reproducer:
- pre-fix reproducer failed at iteration `960`;
- post-fix reproducer passed through `1200` iterations.

The true root cause was not admission control and not shared-engine topology. The dominant issue was recursive snapshot-chain resolution in the snapshot path under sustained delta-snapshot growth.

## Baseline artifacts

### Snapshot reproducer

Pre-fix failing artifact:
- `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v4.json`

Post-fix passing artifact:
- `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v5.json`

### 25m strict diagnostic

Artifact:
- `.pytest-artifacts/phase1-repro/m4-25m-diagnostic-route0-v20-snapshot-fix-20260310-2205.json`

Result:
- `passed=true`
- `config_fingerprint=7b23891d939e7dc5151873471b2207846ecf392e1ea4cbd6cfb0b577393ddbe0`

Key metrics:
- `submit_tps = 0.8567`
- `completion_tps_submit_phase = 0.8567`
- `promotion_tps = 0.7133`
- `submit_p95_ms = 30.19`
- `rho = 0.9961`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

### 60m strict

Artifact:
- `.pytest-artifacts/phase1-repro/m4-60m-strict-route0-v21-snapshot-fix-20260311-1225.json`

Result:
- `passed=true`
- `config_fingerprint=a5c79e3001ca3d2576739b5bb598bb33034363c9c838d3642eef9cb5f86e4fdc`

Key metrics:
- `submit_tps = 0.8556`
- `completion_tps_submit_phase = 0.8556`
- `promotion_tps = 0.7330`
- `submit_p95_ms = 135.09`
- `rho = 0.9961`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

### Night suite

Suite summary:
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-summary.txt`

Suite log:
- `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602.log`

Per-run artifacts:
- 15m:
  - `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass15.json`
- 60m A:
  - `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass60a.json`
- 60m B:
  - `.pytest-artifacts/phase1-suite/night-suite-snapshot-fix-20260311-115602-pass60b.json`

Night-suite result summary:

| Run | Passed | Submit TPS | Completion TPS | Promotion TPS | Submit P95 ms | Rho | Backlog End | Snapshot Quarantines |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 15m | yes | 0.8578 | 0.8578 | 0.6423 | 50.24 | 1.0000 | 0 | 0 |
| 60m A | yes | 0.8556 | 0.8556 | 0.7330 | 120.16 | 0.9961 | 0 | 0 |
| 60m B | yes | 0.8556 | 0.8556 | 0.7330 | 134.22 | 0.9961 | 0 | 0 |

### 4h soak

Artifact:
- `.pytest-artifacts/phase1-suite/soak-4h-snapshot-fix-20260311.json`

Result:
- `passed=true`
- `config_fingerprint=20bdf8c419f4402cbf1f7806e9564ae1d0453102c2ae5a12ea42647bbcdb1212`

Key metrics:
- `submit_tps = 0.8532`
- `completion_tps_submit_phase = 0.8532`
- `promotion_tps = 0.7866`
- `submit_p95_ms = 1086.01`
- `rho = 0.9961`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

### 10h soak

Artifact:
- `.pytest-artifacts/phase1-suite/soak-10h-snapshot-fix-20260312.json`

Result:
- `passed=true`
- `config_fingerprint=77d14f73255c42d9c5083010dcfd35a5f10166e93d9faaef9c5a2dfd05d40303`

Key metrics:
- `submit_tps = 0.8500`
- `completion_tps_submit_phase = 0.8500`
- `promotion_tps = 0.8079`
- `submit_p95_ms = 1816.83`
- `rho = 0.9844`
- `backlog_end = 0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`

## Locked baseline configuration

Environment:
- `AETHER_MAX_COHERENCE_BACKLOG=260`
- `AETHER_BACKLOG_RESUME_LEVEL=180`
- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.43`
- `AETHER_ROUTE_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0`
- `AETHER_RATIO_CONTROL_MIN_MU_TPS=0.05`
- `AETHER_RATIO_CONTROL_MIN_MU_EVENTS=1`
- `AETHER_COHERENCE_WINDOW_SECONDS=60`
- `AETHER_RETRY_AFTER_MAX_SECONDS=300`
- `AETHER_GATE_HARD_TIMEOUT_SECONDS=15`

Harness arguments:
- `--agents 1000`
- `--files 500`
- `--contention low`
- `--min-interval-ms 350`
- `--max-interval-ms 1400`
- `--inflight 200`
- `--promotion-batch 30`
- `--promotion-non-conflicting-batches 12`
- `--promotion-max-conflict-sets 1`
- `--gate-execution-mode parallel`
- `--gate-max-parallel 4`
- `--promotion-interval-ms 40`
- `--promotion-iteration-timeout-seconds 30`
- `--materialization-batch-size 2000`
- `--materialization-interval-ms 10`
- `--monitor-interval-ms 500`
- `--submit-via-api-route`
- `--adaptive-intake-throttle`
- `--intake-backlog-high-watermark 165`
- `--intake-backlog-low-watermark 125`
- `--intake-throttle-check-interval-ms 40`
- `--physics-window-seconds 300`
- `--min-submit-throughput 0.8`
- `--min-promotion-throughput 0.8`
- `--max-submit-p95-ms 5000`
- `--max-conflict-rate 0.35`
- `--max-backlog-end 500`
- `--max-submit-failure-rate 0.05`
- `--max-window-rho 1.0`
- `--seed 2026`
- `--strict`

## Implementation delta summary

Primary fix surface:
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/repository_service.py`
- `packages/aether/services/snapshot_reproducer.py`

Supporting diagnostics/runtime:
- `packages/aether/services/swarm_harness.py`
- `apps/aether_cli/main.py`
- `tests/aether/test_services.py`
- `tests/aether/test_swarm_harness.py`

Functional delta:
- added a dedicated snapshot recursion reproducer;
- replaced recursive delta-chain resolution with iterative chain replay in snapshot resolution;
- replaced recursive repository snapshot-file resolution with iterative replay;
- reduced snapshot hot-path ORM/compiler exposure with scalar lookups and direct row insertion;
- preserved and extended harness diagnostics with quarantine-phase counters.

## Code review summary of snapshot fix

Blocking findings:
- none found in the snapshot-fix path after reviewing the delta and cross-checking against the passing runtime artifacts.

Review notes:
- the reproducer is the strongest addition in this set because it converts a 25m/60m diagnosis loop into a short targeted regression loop;
- the iterative chain-replay change is the decisive correctness fix, not just a performance optimization;
- the scalar-query snapshot path is appropriate for the promotion hot path because it removes avoidable ORM/compiler surface.

Residual risks to keep in mind:
- the current passing baseline is still tied to a dirty worktree, so the next step should be a clean commit baseline;
- the reproducer currently leaves its temp storage on disk by design; acceptable for debugging, but worth revisiting before CI hardening;
- direct SQL helpers in the snapshot path are now part of the critical path, so regression coverage there should stay explicit.

## Exact next-run matrix for repeatability

Goal:
- prove that the new baseline is repeatable across seeds before moving to new architecture work.

### Matrix A - strict repeatability

1. 25m strict, seed `2027`
- same locked baseline config
- pass criteria:
  - all strict checks pass
  - `create_snapshot_quarantines = 0`
  - `materialize_gate_workspace_quarantines = 0`

2. 60m strict, seed `2027`
- same locked baseline config
- pass criteria:
  - all strict checks pass
  - no late quarantine wave

3. 60m strict, seed `2028`
- same locked baseline config
- same pass criteria

4. 60m strict, seed `2029`
- same locked baseline config
- same pass criteria

### Matrix B - controlled perturbation

5. 60m strict, seed `2026`, contention `medium`
- keep all other knobs unchanged
- objective:
  - measure headroom, not redefine baseline
- pass criteria:
  - strict checks pass, or
  - if it fails, failure mode must be classified cleanly without changing the baseline

Recommended sequencing:
1. Matrix A first
2. only if Matrix A is clean, run Matrix B
3. 4h soak is now already complete on the locked baseline

## Decision rule after repeatability

If Matrix A passes:
- freeze this configuration as the current Phase 1 operational baseline;
- package the snapshot fix as a focused reliability PR;
- move next work to throughput headroom, operator surfaces, and higher-contention scenarios.

If Matrix A fails:
- do not retune admission control first;
- compare the failure against reproducer and quarantine counters;
- treat any reappearance of snapshot-path quarantines as a direct regression.
