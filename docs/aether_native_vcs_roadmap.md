# Aether Native VCS Roadmap

This roadmap turns the "beyond Git" theory into implementable engineering milestones.

## Phase 1: Autonomous Promotion Stability (in progress)

Goal:
- Ensure continuous promotion can run safely under sustained swarm load.

Implemented:
- Separate promotion worker process (`apps/aether_server/promotion_worker.py`).
- Worker lifecycle integrated into server startup/shutdown (`apps/aether_server/main.py`).
- Env controls:
  - `AETHER_AUTOPROMOTE_ENABLED`
  - `AETHER_AUTOPROMOTE_INTERVAL_SECONDS`
  - `AETHER_AUTOPROMOTE_REPO_TIMEOUT_SECONDS`
  - `AETHER_AUTOPROMOTE_BATCH_SIZE`
  - `AETHER_AUTOPROMOTE_SHUTDOWN_GRACE_SECONDS`

Acceptance:
- Server remains responsive while autonomous promotion is enabled.
- Worker exits cleanly on shutdown and never leaves orphan process.

## Phase 2: Deterministic Replay and Provenance (in progress)

Goal:
- Every promotion is replayable and verifiable from canonical metadata.

Implemented:
- Promotion evidence now includes deterministic operation-graph and replay fields:
  - `_op_graph_parent`
  - `_op_graph_node`
  - `_op_graph_payload_sha256`
  - `_op_graph_kind`
  - `_replay_snapshot_id`
  - `_replay_from_hash`
  - `_replay_to_hash`
  - `_replay_gate_vector`
- Verification service for stored promotions:
  - `PromotionEngine.verify_promotion_provenance(...)`
  - API endpoint: `GET /v1/repos/{repo_id}/promotions/{promotion_id}/verify`

Acceptance:
- Recomputing payload hash from persisted proposal/gate metadata reproduces `_op_graph_node`.
- Replay from `from_hash` to `to_hash` is auditable via snapshot and gate vectors.

## Phase 3: Semantic Conflict Units

Goal:
- Move from file-level conflicts toward symbol and API-region conflicts.

Planned work:
- Extend Python semantic IDs from coarse `CLASS:*`/`FUNC:*` to method-level and block-level IDs.
- Add parser adapters for non-Python files (Tree-sitter based) with uniform symbol graph format.
- Persist symbol touch sets for deterministic conflict detection across restarts.

Implemented now:
- Class nodes are decomposed into per-method semantic nodes (`CLASS:<name>::FUNC:<method>`),
  which enables disjoint method edits in the same class to avoid false conflicts and co-promote.
- Non-Python symbol extraction pipeline:
  - `packages/aether/services/symbol_adapters.py`
  - tree-sitter adapters for JS/TS/Go/Java/Rust when bindings are available
  - regex fallback for JS/TS to keep symbol-level conflict detection active without native bindings

Acceptance:
- Disjoint edits inside same class frequently co-promote without false conflicts.
- Non-Python diffs no longer always collapse to file-level conflict fallback.

## Phase 4: Operation Graph as First-Class History

Goal:
- Represent promotions as causal graph nodes instead of linear commit-only semantics.

Planned work:
- Introduce explicit operation node storage and parent pointers.
- Support multi-parent merge promotions and explicit conflict-resolution lineage.
- Add APIs to query graph ancestry and alternative branches.

Implemented now:
- Explicit operation graph persistence table:
  - `aether_operation_graph_nodes` (`OperationGraphNode` model)
- Promotion path now writes operation nodes at promote-time.
- Operation graph APIs:
  - `GET /v1/repos/{repo_id}/operation-graph`
  - `GET /v1/repos/{repo_id}/operation-graph/{operation_node_id}`

Acceptance:
- Promotion history can be queried as DAG.
- Causal ancestry and branch resolution are deterministic from stored nodes.

## Phase 5: Swarm Scheduler and Coordination Scale

Goal:
- Improve high-tempo throughput with risk/trust/cost-aware scheduling.

Planned work:
- Queue-based proposal ingestion and promotion scheduling.
- Batch tuning policies based on conflict pressure and trust distribution.
- Shard repositories/workers and replace single-process bottlenecks.

Implemented now:
- Optional 500-agent soak profile test:
  - `tests/aether/test_swarm_scale.py::TestSwarmSoakProfile::test_500_agents_low_contention_soak`
  - Enabled with `AETHER_ENABLE_SOAK_TESTS=1`
- Dedicated CI soak job with artifacted output and parsed summary metrics:
  - `.github/workflows/aether-ci.yml` job `aether-soak`

Acceptance:
- Sustained 500+ agent simulation with bounded p95 latency and promotion lag SLOs.
- No single process required for global progress.

## Phase 6: Git Bridge and Migration

Goal:
- Keep interoperability while native operation graph becomes default.

Planned work:
- Preserve bidirectional import/export guarantees.
- Map operation graph lineage into Git-compatible views for external tooling.
- Introduce migration tooling for Git-first repos into native mode.

Acceptance:
- Native mode can round-trip through Git bridge without losing promoted state.
- Teams can transition incrementally by repository.
