# Aether Head CRDT Algebra Specification (Sprint 4A)

## 1. Context and Non-Goals
This document formalizes the mathematical design for a CRDT-native repository head in Aether. The current Aether architecture uses a Compare-And-Swap (CAS) `UPDATE` against the `aether_repositories.head_id` row to strictly serialize promotions. This serializes concurrent writers and yields `409 Conflict` errors when causal boundaries are crossed.

A CRDT-native head replaces the scalar `head_id` with a monotonic, append-only **Frontier** of concurrent heads. It eliminates head-row CAS contention, replacing it with frontier append plus read-time arbitration.

> [!WARNING]  
> **Non-Goal**: This specification does **not** assert that CRDT-native heads should replace CAS in the production architecture. It is a research spike to prove the mathematical soundness, compactness, and determinism of the algebra in isolation. The decision to integrate this into the control plane will follow later empirical bake-offs.

---

## 2. Causal Metadata: Dotted Version Vectors (DVV)
To represent causality without retaining the entire graph of history, we utilize Dotted Version Vectors. 

### 2.1 Primitives
- **Node ID**: A globally unique identifier (e.g., `agent_id`).
- **Dot**: A tuple `(NodeID, SequenceNumber)` uniquely identifying a specific event (a promotion).
- **Version Vector (Causal Context)**: A mapping `{NodeID: SequenceNumber}` summarizing all events observed by a node. If Node A's vector contains `{B: 5}`, it means A has observed all events from B up to sequence 5.

### 2.2 Event Structure
When an agent successfully promotes a proposal, it generates a `HeadEvent`:
```rust
struct HeadEvent {
    snapshot_id: String,       // The resulting filesystem state
    dot: Dot,                  // (agent_id, sequence) uniquely identifying this promotion
    context: VersionVector,    // The causal history the agent observed before promoting
    frozen_trust: f64,         // Immutable trust score at the exact moment of promotion
}
```

---

## 3. The Algebra: `HeadFrontier`
The repository head is modeled as a `HeadFrontier` — a set of `HeadEvent`s that have not been causally superseded by any other known event.

### 3.1 Dominance
A `HeadEvent` **A** is *causally dominated* by **B** if **B**'s causal context encompasses **A**'s dot. 
Mathematically: `B.context >= A.dot`.

### 3.2 Join (Merge) Operation
The fundamental CRDT merge operation `join(F1, F2)` takes two frontiers and produces a new frontier.
1. **Union**: Compute `U = F1 ∪ F2`.
2. **Dominance Pruning**: For every event `E` in `U`, if there exists another event `D` in `U` such that `D` causally dominates `E`, discard `E`.
3. **Result**: The remaining un-dominated events form the new `HeadFrontier`.

#### Property Invariants
The `join` operation strictly satisfies:
- **Commutativity**: `join(A, B) == join(B, A)`
- **Associativity**: `join(A, join(B, C)) == join(join(A, B), C)`
- **Idempotence**: `join(A, A) == A`

---

## 4. Semantic Distinction: Arbitration vs. Convergence
When a `HeadFrontier` contains more than 1 event, the repository is logically forked. Agents promoted concurrently without observing each other.

### 4.1 Arbitration (Read-Time Projection)
To present a singular "current state" to a user or a naive reader (e.g., `GET /v1/repos/{id}/head`), we perform a deterministic projection: `arbitrate_view(Frontier) -> snapshot_id`.

**Rules of Arbitration**:
1. Sort the frontier by `frozen_trust` descending.
2. Break ties by sorting lexicographically on `agent_id`.
3. Return the `snapshot_id` of the first event.

**Crucial Semantic**: Arbitration is a *lossy read projection*. It does not mutate the frontier. It does not resolve the fork. It merely picks a consistent winner so that distributed observers all see the same default state.

### 4.2 Convergence (Write-Time Resolution)
True semantic convergence requires a subsequent agent to execute a merge.
1. Agent observes the forked frontier `(Event A, Event B)`.
2. Agent merges the underlying text/CRDT files locally.
3. Agent promotes a new `Event C`.
4. `Event C`'s causal context includes both `A.dot` and `B.dot`.
5. When `Event C` is appended and `join` is evaluated, `C` dominates both `A` and `B`. The frontier shrinks to a width of 1.
