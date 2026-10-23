# Aether Documentation Index

## Start here

> **Note on Single-Root Release History:** This public release repository is initialized as a clean single-root snapshot on `main`. Commit SHAs and branch names cited inside historical engineering memos, qualification filenames, and benchmark reports (e.g., `024163b8...`, `ab03e58a...`, `52814ac0...`, `e5643a9f...`, `f8787e2b...`, `soak/sprint-a-20260616`) record the pre-release development revisions at which those runs were executed; they are preserved as historical provenance and are not reachable Git objects in this repository.

- `docs/aether_artifact_index_2026_04_13.md`
  - **canonical artifact index**: distinguishes committed, directly inspectable evidence files in this release (`qualification/semantic_suite/*.json`, `peer-review-artifacts/`, `docs/evidence/`, `examples/reproducible_gate_verification_demo.py`) from historical local soak reports (`.pytest-artifacts/*`) excluded from git
- `docs/aether_readiness_package_2026_04_11.md`
  - technical readiness, architecture summary, and historical soak metrics
- `docs/aether_claims_matrix_2026_04_13.md`
  - safe claims now, not-yet-proven claims, and future claims
- `docs/aether_strategy_roadmap_2026_04_13.md`
  - current strategic product and architecture sequencing

## Supporting strategy and research docs

- `docs/aether_future_vcs_alignment_memo_2026_04_11.md`
- `docs/aether_deep_research_operationalization_2026_04_11.md`
- `docs/aether_research_iteration_delta_memo_2026_04_11.md`
- `docs/aether_native_vcs_roadmap.md`
- `docs/aether_rs_refactor_recovery_2026_05_04.md`
  - recovered implementation memo for the local Python-to-Rust refactor sprint
- `docs/aether_scaling_recovery_2026_05_06.md`
  - scaling failure memo covering the GIL/SQLite limits and the shift to Rust/Postgres contention analysis
- `docs/aether_agent_loop_and_soak_plan_review_2026_05_04.md`
  - operating workflow and adversarial review of the current Rust soak remediation plan
- `docs/aether_rs_soak_results_2026_05_04.md`
  - first Rust soak results after the async I/O patch, showing improvement but confirming transaction-scope as the next bottleneck
- `docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`
  - verified metrics and structural stability pass for the Stage 3 Rust Postgres Native execution
- `docs/aether_stage6_200agent_topology_design.md`
  - topology and integrity contract used for the 200-agent Postgres canary and follow-on density analysis
- `docs/aether_semantic_merge_bakeoff_2026_05_17.md`
  - Phase 1 serialized semantic-merge design note derived from the A1 density sweep

## Historical support docs

These remain useful context, but they are not the front-door truth set:

- `docs/phase1_baseline_freeze_2026_03_11.md`
- `docs/phase1_executive_update_2026_03_12.md`
- `docs/peer_review_package_phase1_backpressure_2026_03_02.md`
- `docs/mx_aether_proof_ladder_and_slice_matrix_2026_03_15.md`
## Recent research checkpoints

- `sprint4d_topology_baseline.md` — directional short-run topology baseline evidence; useful for comparative context, but not redesign authorization.
- `sprint4e_design_spike.md` — hypothesis-driven causal frontiers design spike covering schema candidates and microbenchmark tradeoffs; exploratory only, not implementation approval.
