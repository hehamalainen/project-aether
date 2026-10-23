# Aether Research Execution Checklist

Last updated: 2026-02-26 (UTC)
ADR reference: `docs/adr_2026_02_agentic_semantic_control_plane.md`
Execution board reference: `docs/execution_board_2week_architecture_bottlenecks_2026_02_26.md`

## P-Gate Mapping

| Gate | Objective | Required Evidence |
|---|---|---|
| P0 | Demo uptime recoverability | `run_demo_p0_gate.sh` pass |
| P1 | End-to-end flow reliability | `run_demo_p1_gate.sh` pass artifact |
| P2 | Risk freeze and pre-demo guardrails | `run_demo_p2_gate.sh` pass artifact |
| P3 | Fallback readiness | known-good JSON + known-good dashboard state documented |
| P4 | Semantic/AIP advancement | parser adapter + mutator schema tests + strict harness pass |
| P5 | Throughput and queue stability | strict soak showing bounded queue and target latency |

## Current Completion State

- [x] P0 uptime gate passed
  - Evidence: live pass recorded in `docs/swarm_chunk_status.md`
- [x] P1 flow gate passed
  - Evidence: `.pytest-artifacts/demo/p1-flow-20260222-231525.json`
- [x] P2 guardrail gate passed
  - Evidence: `.pytest-artifacts/demo/p2-guardrails-20260222-235140.json`
- [x] P3 fallback package locked
  - Evidence: `.pytest-artifacts/demo/p3-fallback-20260226-081654.json`
  - Stable assets: `.pytest-artifacts/demo/p3-known-good.json`, `.pytest-artifacts/demo/p3-dashboard-state.json`
- [x] P4 semantic/AIP milestone completed
- [ ] P5 strict soak target profile completed

## Phase Tasks

### P3: Demo fallback readiness

- [x] Select and pin one known-good P1 artifact JSON path.
- [x] Capture one known-good dashboard screenshot/state and path.
- [x] Write 2-minute fallback narration (if live run is noisy).

Exit gate:

- [ ] `run_demo_p3_gate.sh` (or equivalent) passes and logs artifact.
- [x] `run_demo_p3_gate.sh` passes and logs artifact.

### P4: Semantic + AIP MVP

- [x] Define AIP mutator schema version (`v0`) and validation rules.
- [x] Add parser adapter coverage for target non-Python file types.
- [x] Add semantic fallback telemetry split by language/path.
- [x] Add regression tests for parser failure and deterministic fallback.
- [x] Run `./scripts/run_demo_p4_gate.sh` and archive artifact.

Exit gate:

- [x] Two consecutive strict runs pass with reduced fallback rate.

### P5: Scalability evidence

- [ ] Run strict soak with agreed thresholds.
- [ ] Confirm queue boundedness and latency target compliance.
- [ ] Confirm deterministic replay consistency under concurrent load.

Exit gate:

- [ ] Artifact-backed pass logged in `docs/swarm_chunk_status.md`.

## Hard No-Go Conditions

- [ ] `lambda_accepted > mu_promoted` sustained.
- [ ] Ack->promote tail latency grows without bound.
- [ ] Semantic fallback rate remains high (>5%) on target mix.
- [ ] Replay hash divergence for same input/event stream.
