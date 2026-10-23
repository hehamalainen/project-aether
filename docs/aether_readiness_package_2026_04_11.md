# Aether Readiness Package (Historical Internal Planning Snapshot — April 11, 2026)

Date: 2026-04-11
Audience: technical peer review, internal planning
Scope: historical snapshot of Python control-plane soak testing, failure history, and engineering milestones as of April 11, 2026

> [!IMPORTANT]
> **Public Release Evidence & Research-Prototype Qualification:**
> - **Research Prototype Status:** Project Aether is released as a **research prototype** (`0.1.0-alpha`), not a production-hardened or customer-ready system. For the authoritative public claims-to-evidence boundary, see [`README.md`](../README.md) and [`docs/aether_claims_matrix_2026_04_13.md`](aether_claims_matrix_2026_04_13.md).
> - **Unbundled Historical Soak Summaries (`.pytest-artifacts/*`) vs. Committed Artifacts:** The `25m`, `60m`, `4h`, and `8h` soak runs referenced below were recorded in local `.pytest-artifacts/*` files and `<MX_SWARM_ROOT>/reports/*` files that are **excluded from git and are not independently verifiable from this release repository**. By contrast, the committed `1,000`-agent `60m` preflight artifact included in this repository ([`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json)) **FAILED** its strict gate (`passed=false`, `submit_p95_ok=false`, P95 submit latency `5,759.84 ms` ~`5.76s` vs `5,000 ms` threshold, `99.55%` of `1,334,063` client attempts throttled at intake, `5,637` accepted, `1.15` proposals promoted/s across `684` promotion batches [`0.16` promotion batches/s]).
> - **Subsequent Long-Run Soak Outcomes (April 11–12, 2026):** After this memo was drafted on April 11, the in-flight `12h` soak (`.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260411-day.json`) exposed a late-window `mark_evaluating` event-loop quarantine wave (`198` quarantines), and the follow-up `24h` soak (`.pytest-artifacts/phase1-suite/soak-24h-mx-intake-r0420-20260412.json`) completed without a quarantine wave but **failed** submit and promotion throughput thresholds.

---

## 1. Executive Summary (Historical Snapshot as of 2026-04-11)

Aether is an experimental agent-native repository control-plane prototype with local synthetic harness and unit/integration test evidence.

What was observed in local development runs up to 2026-04-11:

- In local synthetic swarm-harness runs (`1,000` simulated agents with `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, where intake backpressure throttles `>99%` of client loop attempts so accepted throughput stays near `~0.85` proposals/s), local `25m`, `60m`, `4h`, and `8h` runs were recorded in `.pytest-artifacts/` (raw JSON files not bundled in this release).
- The earlier long-run snapshot recursion failure was isolated, reproduced via `packages/aether/services/snapshot_reproducer.py`, and replaced with iterative chain resolution.
- Separate local MX Swarm adapter experiments exercised proposal submission into an Aether instance (`<MX_SWARM_ROOT>/reports/*`, external to this repository and not bundled here; see `tests/aether/test_mx_swarm_adapter.py` for the in-repo deterministic adapter unit tests).

What remained open / unverified:

- no clean `10h` (`window_rho_ok = 1.011` failure), `12h` (`mark_evaluating` quarantine wave), or `24h` (throughput threshold failure) soak run passed
- raw `.pytest-artifacts/*` soak dumps are not bundled in the public git repository
- broader MX slice coverage and mixed-source stress were not established

Practical conclusion (updated for public research release):

- Aether is an **experimental research prototype** demonstrating CRDT-backed text/AST merging (Python), probabilistic conflict scoring, and OCC/advisory-locked PostgreSQL promotion mechanics (Rust).
- Long-window high-agent scaling and multi-repo/single-repo contention remain active research challenges (see `docs/sprint4d_topology_baseline.md` and `docs/sprint4e_mitigation_experiments.md`).

---

## 2. Current Architecture

### 2.1 Product role

Aether is the canonical control plane for machine-speed software integration. It is responsible for:

- proposal intake
- provenance persistence
- semantic conflict handling
- queue/backpressure control
- snapshot creation and replay
- verification gate execution
- deterministic canonical promotion
- observability and auditability

It is not another coding agent. It is the repository substrate that agent systems need when many changes are generated concurrently.

### 2.2 Core implementation surfaces

Primary code paths:

- `packages/aether/api/routes.py`
- `packages/aether/models/entities.py`
- `packages/aether/services/proposal_service.py`
- `packages/aether/services/promotion_engine.py`
- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/swarm_harness.py`
- `apps/aether_server/main.py`
- `scripts/run_swarm_harness_safe.sh`

### 2.3 Current control model

The current stabilized baseline relies on:

- adaptive intake throttling
- bounded backlog watermarks
- accepted-to-promotion ratio control
- deterministic snapshot/replay
- parallel gate execution with bounded fan-out
- strict runtime telemetry over throughput, latency, rho, backlog, and quarantines

Current proven ratio setting:

- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425` for clean `8h`

Current test candidate for longer windows:

- `AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.420`

---

## 3. Historical Local Soak Summaries vs. Committed Release Evidence

### 3.1 Aether synthetic baseline (historical local `.pytest-artifacts/*` summaries — raw JSON not bundled)

> **Note on Public Release Artifact Availability:** Raw `.pytest-artifacts/` directories are git-ignored and excluded from the public git tree, so the runs in the table below are **historical summaries only and cannot be independently verified from this release**. By contrast, the committed `1,000`-agent `60m` preflight artifact included in this repository ([`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`](../peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json)) **FAILED** (`passed=false`, `submit_p95_ok=false`, `p95_submit_latency_ms = 5759.84`, `99.55%` throttled at intake, `1.15` proposals promoted/s across `684` promotion batches [`0.16` promotion batches/s]). See [`docs/aether_artifact_index_2026_04_13.md`](aether_artifact_index_2026_04_13.md) and [`docs/aether_claims_matrix_2026_04_13.md`](aether_claims_matrix_2026_04_13.md) for the full breakdown.

Recorded historical soak summaries on the Python synthetic harness (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, `1000` simulated agents, `500` files):

| Window | Recorded Historical Status | Historical Local Artifact Path (`.pytest-artifacts/` excluded from git — not verifiable from this release) |
|---|---|---|
| `25m` strict | recorded pass (unbundled) | `.pytest-artifacts/phase1-repro/mx-intake-cleanup-25m-r0425-20260325.json` |
| `60m` strict | recorded pass (unbundled) | `.pytest-artifacts/phase1-repro/mx-intake-cleanup-60m-r0425-20260325.json` |
| `4h` strict diagnostic | recorded pass (unbundled) | `.pytest-artifacts/phase1-suite/diag-4h-mark-evaluating-r0425-20260329c.json` |
| `8h` strict soak | recorded pass (unbundled) | `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json` |
| `10h` strict soak | **failed** (`window_rho_ok = 1.011 > 1.0`) | `.pytest-artifacts/phase1-suite/soak-10h-mx-intake-r0425-20260408-retry.json` |
| `12h` strict soak (`0.420`) | **bug exposed** (`198` `mark_evaluating` quarantines) | `.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260411-day.json` |
| `24h` strict soak (`0.420`) | **failed** (submit & promotion throughput miss) | `.pytest-artifacts/phase1-suite/soak-24h-mx-intake-r0420-20260412.json` |

Recorded summary metrics from the historical `8h` soak notes (under aggressive intake throttling where accepted throughput is `< 1` proposal/s):

- `submit_throughput = 0.8550` accepted proposals/s
- `promotion_throughput = 0.8037` proposals promoted/s (metric counts promoted proposals/s, not promotion batches/s)
- `p95_submit_latency_ms = 1527.22`
- `rho_window = 0.9961`
- `backlog_end = 0`
- `submit_failure_rate = 0.0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`
- `quarantine_reasons_by_phase = {}`

### 3.2 MX Swarm integration boundary (historical external workspace notes vs. in-repo adapter unit tests)

In-repo verifiable scope:
- [`packages/aether/adapters/mx_swarm.py`](../packages/aether/adapters/mx_swarm.py) and [`tests/aether/test_mx_swarm_adapter.py`](../tests/aether/test_mx_swarm_adapter.py) verify deterministic schema translation from MX Swarm artifacts into Aether proposals on `HEAD`.

Historical external workspace summaries (not bundled in this repository):
- Live model-backed MX Swarm runs referenced in `docs/aether_mx_swarm_state_and_gap_analysis_2026_03_15.md` and `docs/mx_aether_proof_ladder_and_slice_matrix_2026_03_15.md` wrote their output reports to an external `<MX_SWARM_ROOT>/reports/` checkout that is **not** included in this repository and is therefore classified as an unbundled historical summary.

### 3.3 What is not yet proven

These claims should not be made yet:

- clean committed `12h` pass
- clean committed `24h` pass
- multi-day soak stability
- broad MX slice coverage as baseline
- mixed-source stress as baseline
- customer-ready production operations

---

## 4. Validated Artifact Index

> **Qualification Note:** Paths under `.pytest-artifacts/` below refer to historical local soak outputs that are excluded from the git release tree; see [`docs/aether_artifact_index_2026_04_13.md`](aether_artifact_index_2026_04_13.md) for the committed evidence files included directly in the repository.

### 4.1 Baseline freeze and root-cause fix

- Baseline freeze document:
  - `docs/phase1_baseline_freeze_2026_03_11.md`
- Snapshot reproducer passing artifact (historical local output):
  - `.pytest-artifacts/phase1-repro/snapshot-repro-1200-v5.json`
- Original long passing `10h` snapshot-fix artifact (historical local output):
  - `.pytest-artifacts/phase1-suite/soak-10h-snapshot-fix-20260312.json`

### 4.2 Integrated MX-intake baseline (historical local soak outputs)

- `25m` pass:
  - `.pytest-artifacts/phase1-repro/mx-intake-cleanup-25m-r0425-20260325.json`
- `60m` pass:
  - `.pytest-artifacts/phase1-repro/mx-intake-cleanup-60m-r0425-20260325.json`
- `4h` pass:
  - `.pytest-artifacts/phase1-suite/diag-4h-mark-evaluating-r0425-20260329c.json`
- `8h` pass:
  - `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json`

### 4.3 Important near-pass / interrupted artifacts (historical local soak outputs)

- `10h` near-pass, single strict miss (`window_rho_ok` only):
  - `.pytest-artifacts/phase1-suite/soak-10h-mx-intake-r0425-20260408-retry.json`
- interrupted `8h` / `12h` artifacts should not be used as baseline proof:
  - `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260329-night.json`
  - `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260330-night.json`
  - `.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260409.json` (interrupted / no final JSON)

---

## 5. Known Failure History and Fixes

### 5.1 Failure: snapshot recursion collapse

Observed behavior:

- late-window promotion degradation
- recurring `create_snapshot` quarantines
- recursion failure in snapshot chain resolution

Fix:

- replaced recursive snapshot-chain traversal with iterative resolution
- added dedicated reproducer
- reduced snapshot hot-path ORM/compiler overhead

Primary fix surfaces:

- `packages/aether/services/snapshot_service.py`
- `packages/aether/services/repository_service.py`
- `packages/aether/services/snapshot_reproducer.py`

Status:

- fixed
- removed from current hot-path failures

### 5.2 Failure: MX-intake integrated baseline long-run collapse

Observed behavior after intake/provenance work:

- early catastrophic `8h` failure
- later `mark_evaluating` quarantine waves in some long runs
- late-window throughput degradation in other long runs

Fixes / mitigations applied:

- provenance-aware intake contract
- stronger soak policy and run classification
- exact quarantine reason instrumentation
- tighter accepted-to-promotion ratio control

Primary relevant surfaces:

- `packages/aether/services/proposal_service.py`
- `packages/aether/services/swarm_harness.py`
- `packages/aether/models/entities.py`
- `scripts/run_swarm_harness_safe.sh`
- `packages/aether/soak_policy.py`

Status:

- materially improved
- clean through `8h`
- `10h` is a near-pass at `0.425`
- `12h @ 0.420` is the current confirmation target

### 5.3 Failure: wrapper timeout bug

Observed behavior:

- an “8h soak” was cut at roughly `2h`
- root cause was the safe wrapper default timeout still fixed at `7200s`

Fix:

- wrapper timeout now derives from `run_seconds + drain_seconds + buffer`
- added explicit buffer and richer recorded metadata

File:

- `scripts/run_swarm_harness_safe.sh`

Status:

- fixed

### 5.4 Failure: operator-window issues

Observed behavior:

- lid-close / sleep
- battery drain / power loss
- daytime policy blocks when override not set

Meaning:

- these are not architecture failures
- they do invalidate artifacts

Status:

- mitigated by:
  - soak policy
  - safer wrapper
  - explicit daytime override
  - better operator discipline

---

## 6. Maturity Assessment (Updated for Public Research Release)

### 6.1 Research prototype positioning

For public release, Aether is explicitly positioned as a **research prototype (`0.1.0-alpha`)**:

- The Python reference backend and the Rust workspace (`aether-rs`) have asymmetric capabilities (see [`README.md`](../README.md)).
- Committed 1,000-agent preflight evidence (`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`) shows strict SLO failure (`passed=false`, P95 submit latency `5.76s` vs `5.0s` limit, `99.55%` intake throttling), and longer historical local runs (`10h`, `12h`, `24h`) exposed `window_rho_ok` misses, `mark_evaluating` quarantine waves, and throughput degradation.
- Subsequent Rust/PostgreSQL topology sweeps (`docs/sprint4d_topology_baseline.md`) demonstrated `90.1%–95.3%` HTTP `409` contention and `15 MB` to `340 MB` table bloat at 50–100 concurrent agents on a single repository.

---

## 7. Current Risks

Most important active risks:

1. **Long-window control margin**
   - `10h` at `0.425` completed but missed `window_rho_ok` narrowly (`1.011`)
   - indicates late-window service-rate headroom is still tight

2. **Repository hygiene / backup**
   - current worktree is dirty
   - code is not yet safely pushed and backed up

3. **Single-machine operational fragility**
   - sleep, power, and lid state still directly affect benchmark validity

4. **Evidence packaging gap**
   - much of the evidence exists, but it is spread across docs and artifacts
   - this readiness package is meant to reduce that gap

---

## 8. Next Three Milestones

### Milestone 1: freeze the longer-run baseline

Goal:

- pass clean `12h` at `0.420`
- then pass clean `24h` on the same config

Exit condition:

- `12h` clean pass
- `24h` clean pass
- no snapshot or `mark_evaluating` quarantine wave
- no operator interruption

### Milestone 2: secure the repository state

Goal:

- clean the worktree
- split focused commits
- push to GitHub
- create a backup branch

Exit condition:

- important code no longer exists only on one laptop
- long-run fixes and supporting docs are safely versioned remotely

### Milestone 3: package for external scrutiny

Goal:

- produce a crisp technical and executive package that can survive peer review

Contents:

- architecture summary
- validated artifact list
- failure history and fixes
- maturity statement
- roadmap and risks

Exit condition:

- ready for:
  - investor discussion
  - technical peer review
  - GitHub / platform partner conversations

---

## 9. External Communication Boundary (Research Prototype Scope)

Safe statement (grounded in committed release artifacts):

- Aether is an experimental agent-native version-control **research prototype** (`0.1.0-alpha`) with a Python reference backend and an experimental Rust/SQLite/PostgreSQL workspace (`aether-rs`).
- On `HEAD`, readers can directly verify Python RGA + LibCST AST semantic merges, deterministic simulated synthesis-fallback merges (`model_version="1.0-fake"`), gate verification demos (`SIMULATED:` vs. `EXECUTED`), and Rust CRDT + PostgreSQL OCC concurrency tests.
- Committed 1,000-agent preflight evidence (`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`) documents intake backpressure (`99.55%` throttled, `1.15` proposals promoted/s across `684` promotion batches [`0.16` promotion batches/s]) and a P95 submit-latency SLO failure (`5.76s` vs `5.0s` limit, `passed=false`), while historical multi-hour soak summaries (`25m`–`24h`) are preserved as unbundled engineering notes in `docs/aether_claims_matrix_2026_04_13.md`.

Unsafe statement (do not claim):

- do not claim production readiness or customer-ready operations
- do not claim that the committed 1,000-agent preflight JSON passed (`passed=false`)
- do not claim unbundled `.pytest-artifacts/*` or `<MX_SWARM_ROOT>` runs as independently verifiable release proofs
- do not claim live LLM synthesis or full Python feature parity on the Rust server

---

## 10. Current In-Flight Work

Current soak in progress at the time of writing (historical local path):

- `.pytest-artifacts/phase1-suite/soak-12h-mx-intake-r0420-20260411-day.json`

Important rule:

- this readiness package does **not** count that in-flight run as validated evidence until the final artifact exists and passes.

---

## 11. Repository State Note

As of 2026-04-11, representative modified areas included:

- `AGENTS.md`
- `packages/aether/api/routes.py`
- `packages/aether/models/entities.py`
- `packages/aether/services/proposal_service.py`
- `packages/aether/services/swarm_harness.py`
- `packages/aether/soak_policy.py`
- `scripts/run_swarm_harness_safe.sh`
- `tests/aether/test_services.py`
- `tests/aether/test_swarm_harness.py`
- `tests/aether/test_system_physics_api.py`
- `tests/aether/test_soak_policy.py`

This is acceptable for ongoing engineering, but not for durable safety. Remote backup is now a priority milestone.

---

## 12. Bottom Line

Aether is real enough to review seriously.

The core thesis is no longer hypothetical:

- high-agent synthetic control-plane evidence exists
- real MX artifact landing into canonical Aether exists
- major failure modes have been found, reproduced, and fixed

The remaining work is concentrated and legible:

- longer-run stability (`12h`, `24h`, then multi-day)
- repository hygiene and remote backup
- external packaging and review

That is the right place to be. It means the project has moved from theory validation into systems maturity work.
