# Sprint 4A: Head CRDT Algebra Findings & Walkthrough

## What was built
We successfully isolated and empirically explored / structurally hardened the CRDT-native algebra for repository heads, strictly adhering to the boundary that **this is a research spike, not a control plane integration**.

### 1. The Specification
Created `docs/aether_head_crdt_algebra_spec.md`.
- Documented the exact distinction between **Arbitration** (a deterministic read-projection of the active fork) and **Convergence** (an explicit causal merge pushed by an agent).
- Bounded the non-goal: This does not prove that CRDT replaces CAS, only that the math is compact and deterministic.

### 2. Causal Metadata (Dotted Version Vectors)
Implemented `aether-rs/crates/crdt/src/causality.rs`.
- `Dot`: A tuple `(agent_id, sequence)` uniquely identifying a promotion event.
- `VersionVector`: A compact map `{agent_id: max_sequence}` representing the full causal history observed by an agent prior to promoting. This prevents the memory cost of storing full causal DAGs.

### 3. The Algebra (HeadFrontier)
Implemented `aether-rs/crates/crdt/src/head_crdt.rs`.
- `HeadFrontier::join`: Merges two sets of concurrent heads. It performs **Dominance Pruning**—if `Event B`'s vector clock includes `Event A`'s dot, `Event A` is pruned from the frontier because it is causally superseded.
- `HeadFrontier::arbitrate_view`: Our read-time projection. It sorts the active frontier by immutable `frozen_trust` (then lexicographically by `agent_id` to break ties) to yield a single deterministic `snapshot_id`. **Crucially, it does not mutate the frontier or fake a semantic merge.**

## Validation & Results

### Property-Based Proofs
> [!NOTE]
> All tests ran natively via `cargo test -p crdt` without needing the Postgres harness.

The tests support the expected CRDT invariants under bounded `proptest` coverage:
1. **Idempotence**: `join(A, A) == A`
2. **Commutativity**: `join(A, B) == join(B, A)`
3. **Associativity**: `join(A, join(B, C)) == join(join(A, B), C)`
4. **Dominance Pruning**: The vector clock inclusion properly trims stale heads.
5. **Projection Determinism**: The `arbitrate_view` function correctly projects the same snapshot regardless of the order in which concurrent heads are received.

> [!WARNING]
> **Caveat on Proptest Generation**: Our test generator builds short (0..5 events) frontiers by safely filtering out invalid causality. This is an effective synthetic test, but it is not a true "reachable-history" generation. It does not represent massive valid frontiers. Furthermore, the invalid-state suite proves `try_append` and `try_join` correctly return `Err(...)` upon encountering corruption, rather than panicking.

### Deterministic Simulation
We ran simulated topologies of N=50 agents to test **Frontier Growth** and **Compaction Efficiency**.

> [!TIP]
> **Key Finding**: With a 20% merge frequency (an agent observes the shared frontier 20% of the time before promoting), a 50-agent swarm running 1,000 steps maintains a highly compact frontier. In the seeded simulation run, compaction remained strong because newly arriving `VersionVectors` naturally dominate and prune large swaths of the active frontier.

> [!WARNING]
> **Caveat on Simulation**: The simulation is reproducible because it is driven by a seeded PRNG (`StdRng::seed_from_u64`). However, it remains a single stochastic trajectory. It is an exploration, not an exhaustive bound proof.

## Next Steps
This spike (Sprint 4A) provides substantial hardening with remaining validation caveats. With the properties empirically explored via seeded simulation and synthetic testing, we have a structurally sound (though not exhaustively proven) causal frontier. We can pause work on the CRDT path here, completely unpolluted, and shift our focus to **Sprint 1 (T-Optimization)** and **Sprint 2 (Arrival Telemetry)**.
