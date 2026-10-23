# RFC: Checkpointed Branch Reuse Layer

- Status: Draft (for review)
- Date: 2026-03-04
- Owner: Aether Core

## 1) Problem

In agent swarms, many proposals repeat the same expensive prefix work (tool calls, prompt chains, intermediate analyses) before diverging on one decision. Today this creates avoidable recomputation and token/runtime waste before proposals even reach Aether promotion.

Important constraint: this RFC improves upstream efficiency only. It does **not** replace promotion capacity work (`mu_promoted`) and does not claim to solve queueing bottlenecks by itself.

## 2) Goal

Add a reusable checkpoint/branch layer for pre-submit exploration so agents can:

1. Save deterministic checkpoints for intermediate workflow state.
2. Branch from any checkpoint to test alternatives in parallel.
3. Reuse unchanged prefixes and submit only winning branches to canonical Aether proposal flow.

## 3) Scope

### In scope

- Checkpoint persistence for agent workflow state (planner/tool context + artifacts).
- Branch creation from checkpoints.
- Resume/replay from checkpoint IDs.
- Reuse accounting (cache hits, avoided recompute time/tokens).
- CLI + API surfaces for checkpoint lifecycle.

### Out of scope

- Changing promotion semantics, gate policy, or operation-graph commit logic.
- Replacing repository conflict detection.
- Cross-repo global scheduler redesign.

## 4) Proposed API (v0)

### HTTP

- `POST /v1/repos/{repo_id}/experiments/checkpoints`
  - Input: `workflow_id`, `step_id`, `state_payload`, `artifact_refs`, `parent_checkpoint_id?`
  - Output: `checkpoint_id`, `state_hash`, `created_at`
- `POST /v1/repos/{repo_id}/experiments/branches`
  - Input: `checkpoint_id`, `branch_label`, `strategy_metadata`
  - Output: `branch_id`, `base_checkpoint_id`
- `POST /v1/repos/{repo_id}/experiments/resume`
  - Input: `checkpoint_id`, `branch_id?`
  - Output: `restored_state_ref`, `restored_artifacts`
- `GET /v1/repos/{repo_id}/experiments/checkpoints/{id}`
  - Output: checkpoint metadata, lineage, reuse stats

### CLI

- `aether checkpoint save`
- `aether checkpoint branch`
- `aether checkpoint resume`
- `aether checkpoint show`

## 5) Data model (minimal)

- `experiment_checkpoints`
  - `id`, `repo_id`, `workflow_id`, `step_id`, `state_hash`, `parent_checkpoint_id`, `artifact_manifest`, `created_at`
- `experiment_branches`
  - `id`, `repo_id`, `base_checkpoint_id`, `branch_label`, `strategy_metadata`, `created_at`
- `checkpoint_reuse_events`
  - `id`, `repo_id`, `checkpoint_id`, `branch_id`, `reuse_type`, `saved_runtime_ms`, `saved_tokens`, `created_at`

All records are immutable append-only events plus metadata references.

## 6) Execution semantics

- Checkpoint identity is content-addressed by deterministic `state_hash`.
- Resume is read-only until the branch mutates state.
- Branches are isolated work lanes; only explicit proposal submission enters canonical Aether promotion.
- Failed branch experiments do not touch canonical head.

## 7) Metrics

Track in `/v1/system/physics` extension or sibling endpoint:

- `checkpoint_hit_rate` = reused checkpoints / checkpoint lookups
- `branch_reuse_saved_runtime_ms_total`
- `branch_reuse_saved_tokens_total`
- `mean_resume_latency_ms`
- `duplicate_prefix_execution_rate`
- `winner_branch_submit_ratio` (winning branch submits / total branch evaluations)

## 8) Acceptance gates

Phase A (correctness):

- Deterministic resume reproduces identical `state_hash` for same checkpoint.
- Branch from same checkpoint is isolated (no cross-branch state bleed).
- Failed branch cannot mutate canonical head.

Phase B (efficiency):

- `checkpoint_hit_rate >= 0.30` on calibrated synthetic workloads.
- `branch_reuse_saved_runtime_ms_total > 0` and `saved_tokens_total > 0`.
- `mean_resume_latency_ms <= 150ms` (local baseline target).

Phase C (system impact guard):

- No regression to existing promotion path SLOs in 15m strict harness.
- Queue physics remains bounded under same backpressure config (`rho_window <= 1`, bounded backlog).

## 9) Risks

- Stale checkpoint reuse if state-hash inputs are incomplete.
- Artifact lifecycle drift (missing files on resume).
- Over-investing in pre-submit optimization while promotion service rate remains primary bottleneck.

## 10) Rollout plan

1. Read-only v0: checkpoint save/show + metrics only.
2. Enable branch/resume for harness-driven experiments.
3. Add submit-winner helper path.
4. Evaluate against nightly trend table before broad enablement.
