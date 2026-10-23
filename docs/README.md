# Aether Documentation Index

## Start here

> **Note on Single-Root Release History & Privacy Redaction:** This public release repository is initialized as a clean single-root snapshot on `main`. Commit SHAs and branch names cited inside historical engineering memos, qualification filenames, and benchmark reports (e.g., `024163b8...`, `ab03e58a...`, `52814ac0...`, `e5643a9f...`, `f8787e2b...`, `soak/sprint-a-20260616`) record the pre-release development revisions at which those runs were executed; they are preserved as historical provenance and are not reachable Git objects in this repository. Local filesystem paths and usernames inside committed logs and reports were redacted to `<AETHER_WORKSPACE>`, `<AETHER_RUNTIME_ARTIFACTS>`, `<MX_SWARM_ROOT>`, and `<REDACTED_USER>`.

- [`README.md`](../README.md)
  - **primary release overview**: research prototype positioning, verified backend capability matrix (Python vs. Rust/SQLite vs. Rust/PostgreSQL), security and promotion boundaries (`commit_promotion` vs. evaluated promotion, `AETHER_ALLOW_CUSTOM_GATE_COMMANDS` Python-only scope), and the 9-row claims-to-evidence table
- [`docs/aether_claims_matrix_2026_04_13.md`](aether_claims_matrix_2026_04_13.md)
  - **authoritative claims matrix**: classifies every major claim into (a) behavior verified on `HEAD`, (b) committed historical reports (including the failed 1,000-agent preflight JSON and simulated synthesis qualification reports), (c) unbundled historical summaries (`.pytest-artifacts/*`, `<MX_SWARM_ROOT>/reports/*`), and (d) unproven hypotheses / future work
- [`docs/aether_artifact_index_2026_04_13.md`](aether_artifact_index_2026_04_13.md)
  - **canonical artifact index**: distinguishes committed, directly inspectable evidence files in this release (`qualification/semantic_suite/*.json`, `peer-review-artifacts/`, `docs/evidence/`, `examples/reproducible_gate_verification_demo.py`) from unbundled historical local soak summaries (`.pytest-artifacts/*`)
- [`docs/aether_readiness_package_2026_04_11.md`](aether_readiness_package_2026_04_11.md)
  - historical April 11, 2026 internal readiness snapshot, updated with explicit research-prototype and evidence-availability qualifications
- [`docs/aether_strategy_roadmap_2026_04_13.md`](aether_strategy_roadmap_2026_04_13.md)
  - strategic research and architecture sequencing

## Supporting architecture and research docs

- [`docs/architecture/aether_cas_correctness_proof.md`](architecture/aether_cas_correctness_proof.md)
  - conditional OCC and PostgreSQL advisory-locking safety argument (design reasoning and explicit assumptions for `aether-rs` PostgreSQL write paths)
- `docs/aether_future_vcs_alignment_memo_2026_04_11.md`
- `docs/aether_deep_research_operationalization_2026_04_11.md`
- `docs/aether_research_iteration_delta_memo_2026_04_11.md`
- `docs/aether_native_vcs_roadmap.md`
- `docs/aether_rs_refactor_recovery_2026_05_04.md`
  - recovered implementation memo for the local Python-to-Rust refactor sprint
- `docs/aether_scaling_recovery_2026_05_06.md`
  - scaling failure memo covering the GIL/SQLite limits and the shift to Rust/Postgres contention analysis
- `docs/aether_agent_loop_and_soak_plan_review_2026_05_04.md`
  - operating workflow and adversarial review of the Rust soak remediation plan
- `docs/aether_rs_soak_results_2026_05_04.md`
  - first Rust soak results after the async I/O patch, showing improvement but confirming transaction-scope as the next bottleneck
- [`docs/aether_stage3_pg_baseline_closeout_2026_05_10.md`](aether_stage3_pg_baseline_closeout_2026_05_10.md)
  - historical Stage 3 synthetic `full_file` harness summary (`50` agent loops across `10` repos with `echo SOAK_GATE_OK` gates, `53.96%` HTTP `409` contention, and `175ms` fallback Poisson threshold)
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

- [`docs/sprint4d_topology_baseline.md`](sprint4d_topology_baseline.md) — directional `120s` topology baseline evidence across 8 `(N × R)` topologies, documenting PostgreSQL table bloat (`15 MB` to `340 MB`) and `90.1%–95.3%` HTTP `409` contention at `N/R >= 50`.
- [`docs/sprint4e_mitigation_experiments.md`](sprint4e_mitigation_experiments.md) — follow-up `120s` mitigation experiments (`VACUUM` tuning and append-only prototype measurements) analyzing application-level lock and subprocess serialization bottlenecks.
- [`docs/sprint4e_design_spike.md`](sprint4e_design_spike.md) — hypothesis-driven causal frontiers design spike covering schema candidates and microbenchmark tradeoffs; exploratory only, not implementation approval.

## Retained Historical Predecessor-Project & Early Session Documents (Root Directory)

The following root-level markdown files are retained strictly as historical development artifacts and **do not describe current Project Aether capabilities**:

- [`HUMAN.md`](../HUMAN.md) — informal early Phase 1–2 session note (January 27, 2026) summarizing initial Python prototype milestones.
- [`EXECUTIVE_SUMMARY.md`](../EXECUTIVE_SUMMARY.md) — January 2026 vision document for a separate predecessor concept (`Evolution Coding` / `IDASDLC`, a proposed 5-layer Recursive Self-Improvement agent framework). Not implemented in Project Aether.
- [`PRD_REVIEW.md`](../PRD_REVIEW.md) — January 3, 2026 architectural review of the predecessor `Evolution Coding` / `IDASDLC` PRD. Not a description of Project Aether.
- [`RELEASE_NOTES.md`](../RELEASE_NOTES.md) — early regression test note from the predecessor `Evolution-Coding` / `IDASDLC` workspace (`14 passed`). Not Project Aether release notes.
