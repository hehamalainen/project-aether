# Swarm Chunk Status

Single source of truth for chunk completion status and gate outcomes.

## Chunk Board

| Chunk | Scope | Owner | Status | Required Gate State | Notes |
|---|---|---|---|---|---|
| 1 | Capacity telemetry attribution | Codex + team | COMPLETED (GO) | Passed | Two consecutive strict burst passes with >95% attribution |
| 2 | Copy-on-write document clone | Codex + team | COMPLETED (GO) | Passed | Two consecutive strict burst passes after COW detachment optimization |
| 3 | Delta snapshot materialization | Codex + team | IN PROGRESS | Sustained 15m+ throughput gate pending | True delta snapshots + gate workspace materialization implemented; submit throughput gate still fails |
| 4 | Split evaluator/committer pipeline | Pending | BLOCKED | Wait for chunk 3 GO | Do not start early |
| 5 | Capacity-based intake control | Pending | BLOCKED | Wait for chunk 4 GO | Do not start early |

## Gate Decision Log

| Date (EET) | Profile | Artifact | Result | Decision | Notes |
|---|---|---|---|---|---|
| 2026-02-10 | Burst Stage B (500 agents) | `.pytest-artifacts/swarm-stage-b-500-fast10.json` | PASS | GO | Backlog under threshold and all strict checks passed |
| 2026-02-10 | Burst Stage C (1000 agents) | `.pytest-artifacts/swarm-stage-c-1000-fast9.json` | PASS | GO | All strict checks passed |
| 2026-02-10 | Stability Soak (4h, 1000 agents) | `.pytest-artifacts/soak/swarm-soak-4h-1000-20260209-220610.json` | FAIL (`submit_throughput_ok`) | NO-GO (for burst SLO) | System stayed stable; throughput target and backlog cap are contradictory for long soak with current promotion capacity |
| 2026-02-10 | Chunk 1 Burst Gate Pass 1 | `.pytest-artifacts/chunk1/chunk1-burst-pass1.json` | PASS | HOLD | First consecutive pass, attribution ratio 95.95% |
| 2026-02-10 | Chunk 1 Burst Gate Pass 2 | `.pytest-artifacts/chunk1/chunk1-burst-pass2.json` | PASS | GO | Second consecutive pass, attribution ratio 97.49%; chunk 1 exit criteria met |
| 2026-02-10 | Chunk 2 Burst Gate Attempt 1 | `.pytest-artifacts/chunk2/chunk2-burst-pass1.json` | FAIL (`submit_throughput_ok`) | HOLD | Initial COW version regressed under strict burst; no advancement |
| 2026-02-10 | Chunk 2 Burst Gate Attempt 2 | `.pytest-artifacts/chunk2/chunk2-burst-pass2.json` | FAIL (`submit_throughput_ok`, `submit_p95_ok`) | NO-GO | Regression reproduced; required corrective optimization |
| 2026-02-10 | Chunk 2 Burst Gate Rerun 1 | `.pytest-artifacts/chunk2/chunk2-burst-rerun1.json` | PASS | HOLD | Strict pass after optimizing COW detachment path |
| 2026-02-10 | Chunk 2 Burst Gate Rerun 2 | `.pytest-artifacts/chunk2/chunk2-burst-rerun2.json` | PASS | GO | Second consecutive strict pass; chunk 2 exit criteria met |
| 2026-02-10 | Chunk 3 Burst Check 1 | `.pytest-artifacts/chunk3/chunk3-burst-check1.json` | PASS | HOLD | Delta materialization path enabled; short strict burst passes |
| 2026-02-10 | Chunk 3 Burst Check 2 | `.pytest-artifacts/chunk3/chunk3-burst-check2.json` | PASS | HOLD | Snapshot materialization improved vs chunk 2 baseline |
| 2026-02-10 | Chunk 3 Burst Check 3 | `.pytest-artifacts/chunk3/chunk3-burst-check3.json` | PASS | HOLD | 60-min gate pair still required before GO |
| 2026-02-11 | Chunk 3 Stability Gate Pass 1 (60m) | `.pytest-artifacts/chunk3/chunk3-60m-pass1.json` | PASS | HOLD | Stability profile pass (submit throughput non-blocking); snapshot target still unmet |
| 2026-02-11 | Chunk 3 Stability Gate Pass 2 (60m) | `.pytest-artifacts/chunk3/chunk3-60m-pass2.json` | PASS | NO-GO (exit) | Required 60m pair achieved, but snapshot reduction target not met |
| 2026-02-11 | Chunk 3 Stage A (100 agents, 15m) | `.pytest-artifacts/chunk3/chunk3-15m-stage-a.json` | FAIL (`submit_throughput_ok`) | HOLD | Long-duration gate with throttle enabled; submit latency healthy but intake capped by backlog control |
| 2026-02-11 | Chunk 3 Stage B (500 agents, 15m) | `.pytest-artifacts/chunk3/chunk3-15m-stage-b.json` | FAIL (`submit_throughput_ok`) | NO-GO (iteration) | Promotion path stable; sustained submit target not achievable under current watermark policy and strict threshold pairing |

## Current Working Baseline

- Promotion gate mode: `parallel`
- Gate parallelism: `4`
- Adaptive intake throttle: enabled
- High/low watermarks: auto-derived from backlog threshold

## Demo-Day Gate Log

| Date (UTC) | Gate | Command | Artifact | Result | Notes |
|---|---|---|---|---|---|
| 2026-02-22 | P0 Uptime | `./scripts/run_demo_p0_gate.sh` | N/A (live health + forced recovery) | PASS | Backend/UI recovered after forced stop; keepalive restored stack within timeout |
| 2026-02-22 | P1 Flow | `./scripts/run_demo_p1_gate.sh` | `.pytest-artifacts/demo/p1-flow-20260222-231525.json` | PASS | Repo create -> proposal submit -> promotion -> physics all checks passed |
| 2026-02-22 | P2 Guardrails | `./scripts/run_demo_p2_gate.sh` | `.pytest-artifacts/demo/p2-guardrails-20260222-235140.json` | PASS | Risk-freeze baseline initialized; stack healthy; keepalive active; latest P1 artifact passed |
| 2026-02-24 | P2 Guardrails (Research-Wired) | `./scripts/run_demo_p2_gate.sh` | `.pytest-artifacts/demo/p2-guardrails-20260224-154756.json` | PASS | Added ADR/checklist guard (`research_alignment_ready=true`) to existing P-gate flow |
| 2026-02-26 | P3 Fallback | `./scripts/run_demo_p3_gate.sh` | `.pytest-artifacts/demo/p3-fallback-20260226-081654.json` | PASS | Pinned stable fallback assets (`p3-known-good.json`, `p3-dashboard-state.json`) |
| 2026-02-26 | P2 Guardrails (Post-P3 Regression) | `./scripts/run_demo_p2_gate.sh` | `.pytest-artifacts/demo/p2-guardrails-20260226-082621.json` | PASS | Research alignment guard remains green after P3 checklist updates |
| 2026-02-26 | P4 Semantic/AIP | `./scripts/run_demo_p4_gate.sh` | `.pytest-artifacts/demo/p4-semantic-aip-20260226-082040.json` | PASS | AIP schema/parser tests green + dual strict harness pass (`p4-strict-pass1-*`, `p4-strict-pass2-*`), fallback window rate `0.0` |

## Architecture Bottleneck Execution Log

| Date (UTC) | Workstream | Change | Validation | Result | Notes |
|---|---|---|---|---|---|
| 2026-02-26 | A (Natural intake shaping) | Added per-path active-proposal hotspot throttle (`AETHER_MAX_PATH_ACTIVE_PROPOSALS`) with `429 path_hotspot_exceeded` | `python -m pytest -q tests/aether/test_backpressure_api.py tests/aether/test_services.py::TestSystemPhysicsTelemetry::test_active_path_counts_only_include_active_statuses tests/aether/test_system_physics_api.py` | PASS | Starts 2-week architecture board execution; reduces same-file stampede pressure |

## Chunk 1 Evidence Summary

- Tests passed:
  - `python -m pytest tests/aether/test_services.py tests/aether/test_swarm_harness.py -q`
- Consecutive strict burst passes:
  - `.pytest-artifacts/chunk1/chunk1-burst-pass1.json`
  - `.pytest-artifacts/chunk1/chunk1-burst-pass2.json`
- Telemetry fields now present:
  - `clone_ms`
  - `snapshot_hash_ms`
  - `snapshot_materialize_ms`
  - `snapshot_db_commit_ms`
  - `snapshot_publish_ms`

## Chunk 2 Evidence Summary

- Tests passed:
  - `python -m pytest tests/aether/test_crdt.py -q`
  - `python -m pytest tests/aether/test_services.py tests/aether/test_swarm_harness.py -q`
- New clone isolation tests:
  - `test_clone_copy_on_write_detaches_only_modified_file`
  - `test_clone_get_document_write_isolated`
- Consecutive strict burst passes:
  - `.pytest-artifacts/chunk2/chunk2-burst-rerun1.json`
  - `.pytest-artifacts/chunk2/chunk2-burst-rerun2.json`
- Exit criteria check:
  - `clone_ms` materially reduced (from ~4.2s in chunk 1 to ~0.7ms in chunk 2 reruns)

## Chunk 3 Interim Evidence Summary

- Delta snapshot materialization implemented in `SnapshotService` (base snapshot reuse + changed-path rewrite).
- True delta snapshot manifests are now persisted (changed files only + `base_snapshot_id` pointer).
- Promotion now materializes a temporary full gate workspace per snapshot, keeping full-tree expansion off the snapshot creation hot path.
- Tests passed:
  - `python -m pytest tests/aether/test_services.py -q`
  - `python -m pytest tests/aether/test_swarm_harness.py tests/aether/test_promotion_worker.py -q`
  - `python -m pytest tests/aether/test_swarm_scale.py -q`
- Short strict burst checks all passed:
  - `.pytest-artifacts/chunk3/chunk3-burst-check1.json`
  - `.pytest-artifacts/chunk3/chunk3-burst-check2.json`
  - `.pytest-artifacts/chunk3/chunk3-burst-check3.json`
- 60-minute stability gate pair:
  - `.pytest-artifacts/chunk3/chunk3-60m-pass1.json` (`snapshot_ms=3339.1`, `snapshot_materialize_ms=2611.3`)
  - `.pytest-artifacts/chunk3/chunk3-60m-pass2.json` (`snapshot_ms=4040.6`, `snapshot_materialize_ms=3222.4`)
- 15-minute A/B rerun after true-delta + gate-workspace change:
  - `.pytest-artifacts/chunk3/chunk3-15m-stage-a.json` (`submit_tps=11.32`, `snapshot_ms=3886.3`, `snapshot_materialize_ms=1955.1`, `gates_ms=1994.6`)
  - `.pytest-artifacts/chunk3/chunk3-15m-stage-b.json` (`submit_tps=11.24`, `snapshot_ms=3004.2`, `snapshot_materialize_ms=2066.7`, `gates_ms=1944.9`)
- Performance trend vs chunk 2 burst baseline:
  - `snapshot_materialize_ms` improved from ~2253-2501 ms to ~1931-2083 ms
  - `snapshot_ms` improved from ~3047-3331 ms to ~2725-2869 ms
- Exit-gate decision:
  - 60m pair requirement: met
  - 15m A/B sustained submit gate: failed (`submit_throughput_ok`)
  - `>=3x snapshot_ms` reduction target: not met
  - Chunk 3 remains `IN PROGRESS` (no advancement to chunk 4 yet).
