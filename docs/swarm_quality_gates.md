# Swarm Quality Gates

This document defines the hard gate policy for all scalability work chunks.
No chunk advances unless the current chunk passes all required gates.

## Non-Negotiable Rules

- Hard stop: if any required gate fails, do not move to next chunk.
- Two-pass rule: a required performance gate must pass in two consecutive runs.
- Artifact-first: every gate run writes JSON artifacts under `.pytest-artifacts/`.
- Deterministic review: decisions are recorded in `docs/swarm_chunk_status.md`.

## Demo-Day Gate Track (P0-P4)

Use this track when demo reliability is the top priority.

### P0: Demo Uptime (must pass before P1)

- Command: `./scripts/run_demo_p0_gate.sh`
- Required pass criteria:
  - backend and UI become healthy
  - 3 consecutive status checks pass
  - if `AETHER_P0_RECOVERY_TEST=true`, keepalive recovers from forced stop within timeout
- Failure policy:
  - fix only uptime/recoverability issues (`scripts/demo_stack.sh`, `scripts/demo_keepalive.sh`)
  - no architectural refactors

### P1: Demo Flow Rehearsal

- Command: `./scripts/run_demo_p1_gate.sh`
- Required pass criteria:
  - one full dry-run succeeds: repo create -> proposal submit -> promotion -> `/v1/system/physics`
  - runbook timing fits 5-7 minutes
- Failure policy:
  - patch only demo-path reliability/observability

### P2: Risk Freeze

- Command: `./scripts/run_demo_p2_gate.sh`
- Required pass criteria:
  - risk-freeze baseline is initialized (`.run/demo_risk_freeze_baseline.txt`)
  - no deep promotion-engine or snapshot-architecture changes merged pre-demo
  - only targeted fixes for demo-path errors
  - research ADR + execution checklist checks pass (`./scripts/check_research_alignment.sh`)
  - latest P1 artifact exists and `passed=true`
  - keepalive process is active

### P3: Fallback Readiness

- Command: `./scripts/run_demo_p3_gate.sh`
- Required pass criteria:
  - one known-good JSON artifact is preselected
  - one known-good dashboard state is available for backup display
  - fallback assets are pinned to stable paths under `.pytest-artifacts/demo/`

### P4: Semantic/AIP Milestone

- Command: `./scripts/run_demo_p4_gate.sh`
- Required pass criteria:
  - AIP schema + parser adapter regression tests pass
  - two consecutive strict harness runs pass
  - semantic fallback rate passes configured target (`--max-fallback-rate`, default `0.05`)
  - fallback telemetry is visible by language/path via `/v1/system/physics`

## Gate Profiles

Use different SLO contracts for different goals.

### Burst Profile (short horizon throughput)

Purpose: verify high intake and promotion under short pressure windows.

- Typical run: 90s run + 20s drain
- Expected checks:
  - `submit_throughput_ok` must pass
  - `promotion_throughput_ok` must pass
  - `submit_p95_ok` must pass
  - `conflict_rate_ok` must pass
  - `backlog_end_ok` must pass
  - `submit_failure_rate_ok` must pass

### Stability Profile (long soak reliability)

Purpose: verify bounded backlog and stable service over long durations.

- Typical run: 4h run + 20m drain
- Required checks:
  - `promotion_throughput_ok`
  - `submit_p95_ok`
  - `conflict_rate_ok`
  - `backlog_end_ok`
  - `submit_failure_rate_ok`
- Optional/non-blocking check:
  - `submit_throughput_ok` (tracked, but not a blocker for stability profile)

## Chunk Exit Gates

Each chunk has explicit gate criteria in addition to tests.

### Chunk 1: Capacity Telemetry

- Tests:
  - `python -m pytest tests/aether/test_services.py -q`
  - `python -m pytest tests/aether/test_swarm_harness.py -q`
- Perf gate:
  - 2 consecutive burst runs with telemetry fields present in JSON.
- Exit criteria:
  - >90% of `evaluate_ms` attributable to subphases (`clone`, `snapshot`, `gates`, `score`).

### Chunk 2: Copy-On-Write Clone

- Tests:
  - chunk 1 tests plus clone behavior tests.
- Perf gate:
  - 2 consecutive burst runs.
- Exit criteria:
  - `clone_ms` decreases materially and does not grow with run time.

### Chunk 3: Delta Snapshot

- Tests:
  - chunk 2 tests plus snapshot correctness tests.
- Perf gate:
  - 2 consecutive 60-minute runs.
- Exit criteria:
  - `snapshot_ms` reduced by >=3x vs pre-delta baseline.

### Chunk 4: Promotion Pipeline Split

- Tests:
  - chunk 3 tests plus promotion ordering/replay tests.
- Perf gate:
  - 2 consecutive 60-minute runs.
- Exit criteria:
  - stable `promotion_throughput >= 1.0/s` with bounded backlog.

### Chunk 5: Capacity-Based Intake Control

- Tests:
  - chunk 4 tests plus throttle policy tests.
- Perf gate:
  - 2 consecutive burst runs + 1 stability run.
- Exit criteria:
  - no contradictory SLO set between burst and stability profiles.

## Artifact Naming Convention

- Burst:
  - `.pytest-artifacts/swarm-stage-<stage>-<variant>.json`
- Soak:
  - `.pytest-artifacts/soak/swarm-soak-<profile>-<timestamp>.json`
- Always include a paired `.log` file when available.

## Decision Logging

After each gate run, add an entry in `docs/swarm_chunk_status.md` with:

- date/time
- profile (`burst`, `stability`, `soak`)
- artifact path
- pass/fail per check
- go/no-go decision
