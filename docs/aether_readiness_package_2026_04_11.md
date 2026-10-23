# Aether Readiness Package

Date: 2026-04-11
Audience: technical peer review, executive/investor review, internal planning
Scope: current architecture, validated evidence, failure history, maturity assessment, and next milestones

---

## 1. Executive Summary

Aether is no longer just a research thesis. It is a working agent-native repository control plane with real runtime evidence.

What is already true:

- Aether has passed strict synthetic swarm validation at `1000` concurrent agents in controlled runs.
- The earlier long-run snapshot recursion failure was isolated, reproduced, and removed from the hot path.
- MX Swarm has already landed real model-generated artifacts into Aether, and Aether persisted and promoted them canonically.
- The current integrated baseline has now passed clean `25m`, `60m`, `4h`, and `8h` strict validation windows.

What is not yet closed:

- a clean committed `12h` artifact is not frozen yet
- a clean committed `24h` artifact is not frozen yet
- the repository is still dirty and not yet pushed/safeguarded to GitHub
- broader MX slice coverage and mixed-source stress are not yet the baseline story

Practical conclusion:

- Aether is past research-only beta.
- It is in validated-system / pre-production-hardening territory.
- The remaining gap is long-window reliability proof, repository hygiene, and packaging for external scrutiny.

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

## 3. What Is Proven

### 3.1 Aether synthetic baseline

> **Note on Public Release Artifact Availability:** Raw `.pytest-artifacts/` directories are git-ignored and excluded from the public git tree. The table below records historical local soak runs (`AETHER_ACCEPTED_TO_PROMOTION_RATIO_LIMIT=0.425`, `1000` agents, `500` files) and their preserved summary metrics. For committed, directly inspectable JSON/log artifacts in this release tree (`peer-review-artifacts/20260304/phase1/phase1-60m-preflight-20260304-121522.json`, `qualification/semantic_suite/*.json`, `docs/evidence/`), see [`docs/aether_artifact_index_2026_04_13.md`](aether_artifact_index_2026_04_13.md).

Validated historical soak reports on the integrated baseline:

| Window | Status | Historical Local Artifact Path (`.pytest-artifacts/` excluded from git) |
|---|---|---|
| `25m` strict | pass | `.pytest-artifacts/phase1-repro/mx-intake-cleanup-25m-r0425-20260325.json` |
| `60m` strict | pass | `.pytest-artifacts/phase1-repro/mx-intake-cleanup-60m-r0425-20260325.json` |
| `4h` strict diagnostic | pass | `.pytest-artifacts/phase1-suite/diag-4h-mark-evaluating-r0425-20260329c.json` |
| `8h` strict soak | pass | `.pytest-artifacts/phase1-suite/soak-8h-mx-intake-r0425-20260331-stable.json` |

Key confirmed properties from the clean `8h` soak:

- `submit_throughput = 0.8550`
- `promotion_throughput = 0.8037`
- `p95_submit_latency_ms = 1527.22`
- `rho_window = 0.9961`
- `backlog_end = 0`
- `submit_failure_rate = 0.0`
- `create_snapshot_quarantines = 0`
- `materialize_gate_workspace_quarantines = 0`
- `quarantine_reasons_by_phase = {}`

That is the current canonical long-run evidence.

### 3.2 MX Swarm integration boundary

The MX Swarm integration claim is also proven, though it belongs to the combined Aether + MX story rather than Aether-alone soak proof.

What is proven:

- deterministic adapter proof: green
- canned worker proof: green
- local model-backed live proof: green
- real model-generated artifact landed in Aether and was promoted canonically

Supporting Aether-side and combined-system docs:

- `docs/aether_mx_swarm_state_and_gap_analysis_2026_03_15.md`
- `docs/mx_aether_proof_ladder_and_slice_matrix_2026_03_15.md`

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

## 6. Current Maturity Level

### 6.1 Maturity assessment

Aether is currently at:

- **Level 3 moving into Level 4**

Working definition:

- Level 3 = validated system prototype
- Level 4 = pre-production reliability hardening

### 6.2 Why it is not just research beta anymore

Because the following are already true:

- the core control-plane architecture works in runtime
- major failure modes have been isolated and fixed, not hand-waved
- the system has real strict runtime evidence at `1000` agents
- the MX integration boundary is proven with real artifact landing and promotion

### 6.3 Why it is not yet production mature

Because the following are still missing:

- clean committed `12h` and `24h` long-run proof
- repository cleanup and secure remote backup
- cleaner packaging of claims vs evidence for outside reviewers
- broader workload coverage beyond the current validated baseline story

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

## 9. What To Say Externally Right Now

Safe statement:

- Aether is a working agent-native repository control plane.
- The system has passed strict synthetic validation at `1000` concurrent agents through a clean `8h` soak on the current integrated baseline.
- Aether has already proven real end-to-end integration with MX Swarm for model-generated artifact landing and promotion.
- The system is no longer in idea-stage research. It is in runtime hardening and long-window validation.

Unsafe statement:

- do not claim production readiness
- do not claim multi-day stability
- do not claim broad workload coverage
- do not claim the current dirty worktree as a final clean baseline freeze

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
